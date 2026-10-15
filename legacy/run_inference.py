import os
from tqdm import tqdm
import torch
import onnxruntime
import numpy as np
import time
import torchvision 
import cv2
import tabulate as tb
import tensorrt as trt
import logging
import threading
import weakref
import atexit
import signal

# Thread-local and instance tracking are used for cleanup/recovery paths.
_thread_local = threading.local()
_inference_instances = weakref.WeakSet()

# Try to import PyCUDA for CUDA memory management (works even if PyTorch CUDA doesn't)
try:
    import pycuda.driver as cuda
    import pycuda.autoinit
    PYCUDA_AVAILABLE = True
except ImportError:
    PYCUDA_AVAILABLE = False
    # Logger not yet defined, use print
    print("WARNING: PyCUDA not available - using torch CUDA fallback for TensorRT if available")

# Logging is configured in logger.py - don't override it here
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s - %(levelname)s - %(message)s',
#     datefmt='%Y-%m-%d %H:%M:%S'
# )
logger = logging.getLogger(__name__)
TORCH_CUDA_AVAILABLE = torch.cuda.is_available()

# CUDA context cleanup functions (only if PyCUDA is available)
if PYCUDA_AVAILABLE:
    def _cleanup_cuda_context():
        """Cleanup CUDA context on exit to avoid context stack warnings"""
        # First, try to explicitly cleanup all instances
        # This should have been done already when threads stopped, but do it again as safety
        try:
            for instance in list(_inference_instances):
                try:
                    instance.cleanup()
                except:
                    pass
        except:
            pass
        
        # Then aggressively pop all remaining contexts from the current thread's stack
        # Note: This only works for the current thread - contexts in other threads
        # should have been cleaned up when those threads stopped
        try:
            max_pops = 20  # Increased limit for multiple threads
            pop_count = 0
            for _ in range(max_pops):
                try:
                    ctx = cuda.Context.get_current()
                    if ctx is None:
                        break
                    ctx.pop()
                    pop_count += 1
                except (cuda.Error, AttributeError, RuntimeError):
                    # Stack might be empty or error occurred
                    break
            
            # Also try detach as fallback
            try:
                ctx = cuda.Context.get_current()
                if ctx is not None:
                    ctx.detach()
            except (cuda.Error, AttributeError, RuntimeError):
                pass
                
            if pop_count > 0:
                logger.info(f"Cleaned up {pop_count} CUDA context(s) from main thread on exit")
        except:
            # Ignore any errors during cleanup
            pass

    def _signal_handler(signum, frame):
        """Handle signals to clean up CUDA context before exit"""
        _cleanup_cuda_context()
        # Use os._exit to bypass PyCUDA's cleanup that causes abort
        os._exit(0)

    # Register cleanup function with high priority (lower number = higher priority)
    # This ensures it runs before PyCUDA's own cleanup
    atexit.register(_cleanup_cuda_context)
    # Also register with atexit.unregister to ensure it's first
    try:
        # Try to register with priority by unregistering and re-registering
        atexit.unregister(_cleanup_cuda_context)
    except:
        pass
    atexit.register(_cleanup_cuda_context)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


def _trt_dtype_to_torch(dtype: trt.DataType) -> torch.dtype:
    if dtype == trt.DataType.FLOAT:
        return torch.float32
    if dtype == trt.DataType.HALF:
        return torch.float16
    if dtype == trt.DataType.INT8:
        return torch.int8
    if dtype == trt.DataType.INT32:
        return torch.int32
    if dtype == trt.DataType.BOOL:
        return torch.bool
    # Conservative fallback
    return torch.float32


class TensorRTInference:
    def __init__(self, engine_path, img_size=(640, 640)):
        """
        Initialize TensorRT inference class
        
        Args:
            engine_path (str): Path to the TensorRT engine file
            img_size (tuple): Input image size (height, width)
        """
        self.engine_path = engine_path
        self.img_size = img_size
        
        # Select CUDA backend for TensorRT device memory pointers.
        if PYCUDA_AVAILABLE:
            self.cuda_backend = "pycuda"
        elif TORCH_CUDA_AVAILABLE:
            self.cuda_backend = "torch"
        else:
            raise RuntimeError(
                "No CUDA backend available for TensorRT inference. "
                "Install pycuda or use a CUDA-enabled torch build."
            )
        
        logger.info(f"Initializing TensorRT inference with engine: {os.path.basename(engine_path)}")
        logger.info(f"Using device: CUDA (via {self.cuda_backend})")
        logger.info(f"Input image size: {img_size}")
        
        # Track this instance for cleanup
        if PYCUDA_AVAILABLE:
            _inference_instances.add(self)
        
        self.engine = self._load_engine()
        if self.engine is None:
            raise RuntimeError(f"Failed to load TensorRT engine from {engine_path}")
        
        self.context = self.engine.create_execution_context()
        if self.context is None:
            raise RuntimeError(f"Failed to create execution context for engine {os.path.basename(engine_path)}")
        
        # Get input/output binding names and shapes
        self.input_binding = self.engine.get_tensor_name(0)
        self.output_binding = self.engine.get_tensor_name(1)
        self.input_shape = tuple(self.engine.get_tensor_shape(self.input_binding))
        self.output_shape = tuple(self.engine.get_tensor_shape(self.output_binding))
        self.input_dtype = self.engine.get_tensor_dtype(self.input_binding)
        self.output_dtype = self.engine.get_tensor_dtype(self.output_binding)

        logger.info(f"TensorRT input binding: {self.input_binding} shape={self.input_shape} dtype={self.input_dtype}")
        logger.info(f"TensorRT output binding: {self.output_binding} shape={self.output_shape} dtype={self.output_dtype}")
        
        # Initialize backend-specific device buffers.
        self._pushed_context = False
        self.d_input = None
        self.d_output = None
        self.torch_input = None
        self.torch_output = None

        if self.cuda_backend == "pycuda":
            # Ensure CUDA context is active before allocating memory.
            # This is critical when running in threads (pycuda.autoinit only works in main thread).
            try:
                ctx = cuda.Context.get_current()
                if ctx is None:
                    raise cuda.Error("No current context")
            except (cuda.Error, AttributeError, RuntimeError):
                if not hasattr(_thread_local, 'context_ref_count'):
                    _thread_local.context_ref_count = 0

                if _thread_local.context_ref_count == 0:
                    try:
                        cuda.init()
                        device = cuda.Device(0)
                        primary_ctx = device.retain_primary_context()
                        primary_ctx.push()
                        _thread_local.context_ref_count = 1
                        self._pushed_context = True
                        logger.info("Pushed primary CUDA context for thread")
                    except Exception as e:
                        logger.error(f"Failed to initialize CUDA context: {e}")
                        raise RuntimeError(f"Could not initialize CUDA context: {e}")
                else:
                    _thread_local.context_ref_count += 1
                    logger.debug(f"Incremented context ref count to {_thread_local.context_ref_count}")

            try:
                input_size = int(np.prod(self.input_shape) * np.dtype(np.float32).itemsize)
                output_size = int(np.prod(self.output_shape) * np.dtype(np.float32).itemsize)
                self.d_input = cuda.mem_alloc(input_size)
                self.d_output = cuda.mem_alloc(output_size)
                logger.info(f"Allocated GPU memory: input={input_size/(1024**2):.2f}MB, output={output_size/(1024**2):.2f}MB")
            except Exception as e:
                logger.error(f"Failed to allocate GPU memory: {e}")
                raise RuntimeError(f"Cannot allocate GPU memory for TensorRT inference: {e}")
        else:
            # Torch CUDA fallback: use tensor storage pointers directly for TRT bindings.
            try:
                self.torch_input = torch.empty(
                    self.input_shape,
                    dtype=_trt_dtype_to_torch(self.input_dtype),
                    device='cuda',
                )
                # Output can be dynamic; allocate eagerly only when shape is static.
                if all(int(d) > 0 for d in self.output_shape):
                    self.torch_output = torch.empty(
                        self.output_shape,
                        dtype=_trt_dtype_to_torch(self.output_dtype),
                        device='cuda',
                    )
                logger.info("Allocated TensorRT buffers using torch CUDA tensors")
            except Exception as e:
                logger.error(f"Failed to allocate torch CUDA buffers: {e}")
                raise RuntimeError(f"Cannot allocate CUDA buffers with torch fallback: {e}")

    def cleanup(self):
        """Explicitly cleanup CUDA context - call this before deleting the instance"""
        if self.cuda_backend != "pycuda" or not PYCUDA_AVAILABLE:
            self.torch_input = None
            self.torch_output = None
            return
        
        try:
            # Decrement reference count for this thread
            if hasattr(_thread_local, 'context_ref_count'):
                _thread_local.context_ref_count -= 1
                
                # If this was the last instance in the thread, pop the context
                if _thread_local.context_ref_count <= 0:
                    try:
                        # Try to get current context and pop it
                        ctx = cuda.Context.get_current()
                        if ctx is not None:
                            ctx.pop()
                            _thread_local.context_ref_count = 0
                            logger.debug("Popped CUDA context for thread")
                        else:
                            # No current context, but ref count says we should have one
                            # This might mean we're in a different thread - try to clean up anyway
                            _thread_local.context_ref_count = 0
                    except (cuda.Error, AttributeError, RuntimeError) as e:
                        # If we can't pop, at least reset the ref count
                        _thread_local.context_ref_count = 0
                        logger.debug(f"Could not pop CUDA context: {e}")
        except Exception as e:
            # Ignore errors during cleanup, but log them
            logger.debug(f"Error during cleanup: {e}")
            pass
    
    def __del__(self):
        """Cleanup CUDA context when instance is destroyed (fallback)"""
        # Try to cleanup, but don't rely on this being called
        try:
            self.cleanup()
        except:
            pass

    def _load_engine(self):
        """Load TensorRT engine from file"""
        try:
            TRT_LOGGER = trt.Logger(trt.Logger.ERROR)
            with open(self.engine_path, "rb") as f:
                engine_data = f.read()
            
            if len(engine_data) == 0:
                logger.error(f"Engine file is empty: {self.engine_path}")
                return None
            
            with trt.Runtime(TRT_LOGGER) as runtime:
                engine = runtime.deserialize_cuda_engine(engine_data)
                if engine is None:
                    logger.error(f"deserialize_cuda_engine returned None for: {os.path.basename(self.engine_path)}")
                return engine
        except Exception as e:
            logger.error(f"Exception loading engine from {self.engine_path}: {e}", exc_info=True)
            return None

    def preprocess_image(self, img):
        """
        Preprocess image for inference
        
        Args:
            img (np.ndarray): Input image in BGR format
            
        Returns:
            tuple: (processed image numpy array, original image shape, ratio, padding)
        """
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img, ratio, dwh = letterbox(img, new_shape=self.img_size, stride=32, auto=False)
        img = img.transpose(2, 0, 1)
        img = np.ascontiguousarray(img, dtype=np.float32)
        
        # Normalize to [0, 1]
        img = img / 255.0
        
        # Add batch dimension if needed
        if img.ndim == 3:
            img = np.expand_dims(img, axis=0)
            
        return img, ratio, dwh

    def postprocess_detections(self, output, ratio, dwh, conf_thres=0.3, iou_thres=0.6):
        """
        Postprocess model output to get detections
        
        Args:
            output (torch.Tensor): Model output
            ratio (tuple): Image scaling ratio
            dwh (tuple): Padding dimensions
            conf_thres (float): Confidence threshold for NMS
            iou_thres (float): IoU threshold for NMS
            
        Returns:
            list: List of detections [x1, y1, x2, y2, conf, cls]
        """
        # Convert numpy to torch for NMS (CPU is fine for post-processing)
        try:
            # Check for NaN/Inf in numpy array
            if not np.isfinite(output).all():
                logger.error("Model output contains NaN/Inf. Skipping postprocess for this frame.")
                return []
        except Exception:
            # If finiteness check fails for any reason, continue to NMS and let it decide.
            pass

        # Convert numpy array to torch tensor for NMS (CPU tensor is fine)
        output_torch_tensor = torch.from_numpy(output)
        output_torch = non_max_suppression(output_torch_tensor, conf_thres=conf_thres, iou_thres=iou_thres)
        detections = []
        
        for det in output_torch:
            if len(det):
                for *xyxy, conf, cls in reversed(det):
                    xyxy = [float(i) for i in xyxy]
                    
                    # Convert to original image coordinates
                    xc = (xyxy[0] + xyxy[2])/2
                    yc = (xyxy[1] + xyxy[3])/2
                    w = xyxy[2] - xyxy[0]
                    h = xyxy[3] - xyxy[1]

                    # Robustness: skip invalid boxes
                    if not np.isfinite([xc, yc, w, h]).all():
                        continue
                    if w <= 0 or h <= 0:
                        continue
                    
                    xc -= dwh[0]
                    yc -= dwh[1]
                    xc /= ratio[0]
                    yc /= ratio[1]
                    w /= ratio[0]
                    h /= ratio[1]

                    try:
                        xmin = int(xc - w/2)
                        xmax = int(xc + w/2)
                        ymin = int(yc - h/2)
                        ymax = int(yc + h/2)
                    except Exception:
                        continue
                    
                    detections.append([xmin, ymin, xmax, ymax, conf.item(), int(cls.item())])
        
        return detections

    def infer(self, image, conf_thres=0.3, iou_thres=0.6):
        """
        Run inference on a single image
        
        Args:
            image (np.ndarray): Input image in BGR format
            conf_thres (float): Confidence threshold for NMS
            iou_thres (float): IoU threshold for NMS
            
        Returns:
            list: List of detections [x1, y1, x2, y2, conf, cls]
        """
        # Preprocess image (returns numpy array)
        img, ratio, dwh = self.preprocess_image(image)
        
        # Ensure img is float32 and contiguous
        if img.dtype != np.float32:
            img = img.astype(np.float32)
        if not img.flags['C_CONTIGUOUS']:
            img = np.ascontiguousarray(img)

        # Check if context is valid, recreate if needed
        if self.context is None:
            logger.warning(f"TensorRT context is None, attempting to recreate (engine={os.path.basename(self.engine_path)})")
            try:
                # Check if engine is still valid
                if self.engine is None:
                    logger.error(f"Engine is None, attempting to reload (engine={os.path.basename(self.engine_path)})")
                    self.engine = self._load_engine()
                    if self.engine is None:
                        logger.error(f"Failed to reload engine (engine={os.path.basename(self.engine_path)})")
                        return []
                
                # Try to create context
                self.context = self.engine.create_execution_context()
                if self.context is None:
                    logger.error(f"create_execution_context() returned None (engine={os.path.basename(self.engine_path)})")
                    # Try reloading engine and context
                    logger.info(f"Attempting to reload engine and recreate context (engine={os.path.basename(self.engine_path)})")
                    try:
                        self.engine = self._load_engine()
                        if self.engine is not None:
                            self.context = self.engine.create_execution_context()
                            if self.context is None:
                                logger.error(f"Still failed to create context after engine reload (engine={os.path.basename(self.engine_path)})")
                                return []
                            else:
                                logger.info(f"Successfully recreated context after engine reload (engine={os.path.basename(self.engine_path)})")
                        else:
                            logger.error(f"Failed to reload engine (engine={os.path.basename(self.engine_path)})")
                            return []
                    except Exception as reload_error:
                        logger.error(f"Exception during engine reload: {reload_error}", exc_info=True)
                        return []
                else:
                    logger.info(f"Successfully recreated TensorRT context (engine={os.path.basename(self.engine_path)})")
            except Exception as e:
                logger.error(f"Exception while recreating TensorRT context (engine={os.path.basename(self.engine_path)}): {e}", exc_info=True)
                return []

        # Handle potential dynamic output shapes (-1). Query after setting input shape if needed.
        out_shape = self.output_shape
        if any(int(d) < 0 for d in out_shape):
            try:
                self.context.set_input_shape(self.input_binding, tuple(img.shape))
                out_shape = tuple(self.context.get_tensor_shape(self.output_binding))
            except Exception:
                out_shape = self.output_shape

        # Prepare output buffer (numpy array)
        output = np.empty(out_shape, dtype=np.float32)
        
        # Run inference using selected CUDA backend.
        try:
            if self.cuda_backend == "pycuda":
                # Ensure CUDA context is active before memory operations.
                try:
                    ctx = cuda.Context.get_current()
                    if ctx is None:
                        if not hasattr(_thread_local, 'context_ref_count') or _thread_local.context_ref_count == 0:
                            try:
                                cuda.init()
                                device = cuda.Device(0)
                                primary_ctx = device.retain_primary_context()
                                primary_ctx.push()
                                if not hasattr(_thread_local, 'context_ref_count'):
                                    _thread_local.context_ref_count = 0
                                _thread_local.context_ref_count += 1
                                logger.debug("Pushed CUDA context for inference (context was lost)")
                            except Exception as e:
                                logger.warning(f"Could not push CUDA context: {e}")
                                raise RuntimeError(f"Cannot perform inference without CUDA context: {e}")
                except (cuda.Error, AttributeError, RuntimeError):
                    try:
                        cuda.init()
                        device = cuda.Device(0)
                        primary_ctx = device.retain_primary_context()
                        primary_ctx.push()
                        if not hasattr(_thread_local, 'context_ref_count'):
                            _thread_local.context_ref_count = 0
                        _thread_local.context_ref_count += 1
                        logger.debug("Pushed CUDA context for inference (fallback)")
                    except Exception as push_error:
                        logger.error(f"Could not push CUDA context (fallback): {push_error}")
                        raise RuntimeError(f"Cannot perform inference without CUDA context: {push_error}")

                cuda.memcpy_htod(self.d_input, img)
                bindings = [int(self.d_input), int(self.d_output)]
                if bindings[0] == 0 or bindings[1] == 0:
                    raise RuntimeError(f"Invalid memory pointers: input={bindings[0]}, output={bindings[1]}")

                ok = self.context.execute_v2(bindings)
                if ok is False:
                    error_msg = "TensorRT execute_v2 returned False (CUDA runtime error)"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

                cuda.memcpy_dtoh(output, self.d_output)
            else:
                # Torch CUDA fallback path when pycuda is unavailable.
                input_tensor = torch.from_numpy(img).to(
                    device='cuda',
                    dtype=_trt_dtype_to_torch(self.input_dtype),
                    non_blocking=False,
                )
                if not input_tensor.is_contiguous():
                    input_tensor = input_tensor.contiguous()

                if self.torch_output is None or tuple(self.torch_output.shape) != tuple(out_shape):
                    self.torch_output = torch.empty(
                        out_shape,
                        dtype=_trt_dtype_to_torch(self.output_dtype),
                        device='cuda',
                    )

                bindings = [int(input_tensor.data_ptr()), int(self.torch_output.data_ptr())]
                if bindings[0] == 0 or bindings[1] == 0:
                    raise RuntimeError(f"Invalid memory pointers: input={bindings[0]}, output={bindings[1]}")

                ok = self.context.execute_v2(bindings)
                if ok is False:
                    error_msg = "TensorRT execute_v2 returned False (CUDA runtime error)"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

                output = self.torch_output.detach().float().cpu().numpy()
        except Exception as e:
            logger.exception(f"TensorRT inference failed (engine={os.path.basename(self.engine_path)}): {e}")
            # Best-effort recovery: recreate execution context
            try:
                logger.info(f"Attempting to recreate TensorRT context after error (engine={os.path.basename(self.engine_path)})")
                # First try to recreate context from existing engine
                self.context = self.engine.create_execution_context()
                if self.context is None:
                    logger.warning(f"Context recreation returned None, attempting engine reload (engine={os.path.basename(self.engine_path)})")
                    # If context creation fails, try reloading the engine
                    try:
                        self.engine = self._load_engine()
                        if self.engine is not None:
                            self.context = self.engine.create_execution_context()
                            if self.context is None:
                                logger.error(f"Failed to create context even after engine reload (engine={os.path.basename(self.engine_path)})")
                            else:
                                logger.info(f"Successfully recreated context after engine reload (engine={os.path.basename(self.engine_path)})")
                        else:
                            logger.error(f"Failed to reload engine (engine={os.path.basename(self.engine_path)})")
                            self.context = None
                    except Exception as reload_error:
                        logger.error(f"Exception during engine reload: {reload_error}", exc_info=True)
                        self.context = None
                else:
                    logger.info(f"Successfully recreated TensorRT context (engine={os.path.basename(self.engine_path)})")
            except Exception as recovery_error:
                logger.error(f"Exception while recreating TensorRT context (engine={os.path.basename(self.engine_path)}): {recovery_error}", exc_info=True)
                self.context = None
            return []
        
        # Postprocess detections (output is numpy array)
        detections = self.postprocess_detections(output, ratio, dwh, conf_thres, iou_thres)
        return detections

    def visualize_detections(self, image, detections, save_path=None):
        """
        Visualize detections on image
        
        Args:
            image (np.ndarray): Original image
            detections (list): List of detections
            save_path (str, optional): Path to save visualization
        """
        vis_img = image.copy()
        for det in detections:
            x1, y1, x2, y2, conf, cls = det
            cv2.rectangle(vis_img, (x1, y1), (x2, y2), (0, 255, 0), 2)
            
        if save_path:
            cv2.imwrite(save_path, vis_img)
            logger.info(f"Visualization saved to {save_path}")
        return vis_img

    def predict(self, image, conf_threshold=0.1, iou_threshold=0.2):
        """
        Predict method for compatibility with GrabImage.py
        Returns both detections and the original image.
        
        Args:
            image (np.ndarray): Input image in BGR format
            conf_threshold (float): Confidence threshold for NMS
            iou_threshold (float): IoU threshold for NMS
            
        Returns:
            tuple: (detections, image_copy) where detections is a list of [x1, y1, x2, y2, conf, cls]
        """
        detections = self.infer(image, conf_thres=conf_threshold, iou_thres=iou_threshold)
        return detections, image.copy()

# Backwards-compatible alias for ModelInfer
ModelInfer = TensorRTInference

def box_iou(box1, box2):
    def box_area(box):
        # box = 4xn
        return (box[2] - box[0]) * (box[3] - box[1])

    area1 = box_area(box1.T)
    area2 = box_area(box2.T)

    # inter(N,M) = (rb(N,M,2) - lt(N,M,2)).clamp(0).prod(2)
    inter = (torch.min(box1[:, None, 2:], box2[:, 2:]) - torch.max(box1[:, None, :2], box2[:, :2])).clamp(0).prod(2)
    return inter / (area1[:, None] + area2 - inter)  # iou = inter / (area1 + area2 - inter)

def xywh2xyxy(x):
    # Convert nx4 boxes from [x, y, w, h] to [x1, y1, x2, y2] where xy1=top-left, xy2=bottom-right
    y = x.clone() if isinstance(x, torch.Tensor) else np.copy(x)
    y[:, 0] = x[:, 0] - x[:, 2] / 2  # top left x
    y[:, 1] = x[:, 1] - x[:, 3] / 2  # top left y
    y[:, 2] = x[:, 0] + x[:, 2] / 2  # bottom right x
    y[:, 3] = x[:, 1] + x[:, 3] / 2  # bottom right y
    return y

def letterbox(img, new_shape=(640, 640), color=(114, 114, 114), auto=True, scaleFill=False, scaleup=True, stride=32):
    # Resize and pad image while meeting stride-multiple constraints
    shape = img.shape[:2]  # current shape [height, width]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)

    # Scale ratio (new / old)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    if not scaleup:  # only scale down, do not scale up (for better test mAP)
        r = min(r, 1.0)

    # Compute padding
    ratio = r, r  # width, height ratios
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]  # wh padding
    if auto:  # minimum rectangle
        dw, dh = np.mod(dw, stride), np.mod(dh, stride)  # wh padding
    elif scaleFill:  # stretch
        dw, dh = 0.0, 0.0
        new_unpad = (new_shape[1], new_shape[0])
        ratio = new_shape[1] / shape[1], new_shape[0] / shape[0]  # width, height ratios

    dw /= 2  # divide padding into 2 sides
    dh /= 2

    if shape[::-1] != new_unpad:  # resize
        img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)  # add border
    return img, ratio, (dw, dh)

def non_max_suppression(prediction, conf_thres=0.25, iou_thres=0.45, classes=None, agnostic=False, multi_label=False,
                        labels=()):
    """Runs Non-Maximum Suppression (NMS) on inference results

    Returns:
         list of detections, on (n,6) tensor per image [xyxy, conf, cls]
    """

    nc = prediction.shape[2] - 5  # number of classes
    xc = prediction[..., 4] > conf_thres  # candidates

    # Settings
    min_wh, max_wh = 2, 4096  # (pixels) minimum and maximum box width and height
    max_det = 300  # maximum number of detections per image
    max_nms = 30000  # maximum number of boxes into torchvision.ops.nms()
    time_limit = 10.0  # seconds to quit after
    redundant = True  # require redundant detections
    multi_label &= nc > 1  # multiple labels per box (adds 0.5ms/img)
    merge = False  # use merge-NMS

    t = time.time()
    output = [torch.zeros((0, 6), device=prediction.device)] * prediction.shape[0]
    for xi, x in enumerate(prediction):  # image index, image inference
        # Apply constraints
        x = x[xc[xi]]  # confidence

        # Cat apriori labels if autolabelling
        if labels and len(labels[xi]):
            l = labels[xi]
            v = torch.zeros((len(l), nc + 5), device=x.device)
            v[:, :4] = l[:, 1:5]  # box
            v[:, 4] = 1.0  # conf
            v[range(len(l)), l[:, 0].long() + 5] = 1.0  # cls
            x = torch.cat((x, v), 0)

        # If none remain process next image
        if not x.shape[0]:
            continue

        # Compute conf
        if nc == 1:
            x[:, 5:] = x[:, 4:5] # for models with one class, cls_loss is 0 and cls_conf is always 0.5,
                                 # so there is no need to multiplicate.
        else:
            x[:, 5:] *= x[:, 4:5]  # conf = obj_conf * cls_conf

        # Box (center x, center y, width, height) to (x1, y1, x2, y2)
        box = xywh2xyxy(x[:, :4])

        # Detections matrix nx6 (xyxy, conf, cls)
        if multi_label:
            i, j = (x[:, 5:] > conf_thres).nonzero(as_tuple=False).T
            x = torch.cat((box[i], x[i, j + 5, None], j[:, None].float()), 1)
        else:  # best class only
            conf, j = x[:, 5:].max(1, keepdim=True)
            x = torch.cat((box, conf, j.float()), 1)[conf.view(-1) > conf_thres]

        # Filter by class
        if classes is not None:
            x = x[(x[:, 5:6] == torch.tensor(classes, device=x.device)).any(1)]

        # Check shape
        n = x.shape[0]  # number of boxes
        if not n:  # no boxes
            continue
        elif n > max_nms:  # excess boxes
            x = x[x[:, 4].argsort(descending=True)[:max_nms]]  # sort by confidence

        # Batched NMS
        c = x[:, 5:6] * (0 if agnostic else max_wh)  # classes
        boxes, scores = x[:, :4] + c, x[:, 4]  # boxes (offset by class), scores
        i = torchvision.ops.nms(boxes, scores, iou_thres)  # NMS
        if i.shape[0] > max_det:  # limit detections
            i = i[:max_det]
        if merge and (1 < n < 3E3):  # Merge NMS (boxes merged using weighted mean)
            # update boxes as boxes(i,4) = weights(i,n) * boxes(n,4)
            iou = box_iou(boxes[i], boxes) > iou_thres  # iou matrix
            weights = iou * scores[None]  # box weights
            x[i, :4] = torch.mm(weights, x[:, :4]).float() / weights.sum(1, keepdim=True)  # merged boxes
            if redundant:
                i = i[iou.sum(1) > 1]  # require redundancy

        output[xi] = x[i]
        if (time.time() - t) > time_limit:
            print(f'WARNING: NMS time limit {time_limit}s exceeded')
            break  # time limit exceeded

    return output

# Example usage
# if __name__ == "__main__":
#     # Initialize the TensorRT inference
#     inferencer = TensorRTInference(
#         engine_path="/home/nvidia/Documents/eye_test/yolov7/Toor_Syn_v1_5_1.optimized",  # Replace with your engine file path
#         img_size=(640, 640)  # Optional, defaults to (640, 640)
#     )

#     # Load and process an image
#     image_path = "/home/nvidia/Documents/eye_test/yolov7/toordaal_rice_56.png"  # Replace with your image path
#     image = cv2.imread(image_path)
#     if image is None:
#         logger.error(f"Could not read image {image_path}")
#         exit(1)

#     # Run inference with custom confidence and IoU thresholds
#     detections = inferencer.infer(
#         image=image,
#         conf_thres=0.05,  # Confidence threshold
#         iou_thres=0.45    # IoU threshold
#     )

#     # Visualize results
#     vis_img = inferencer.visualize_detections(
#         image=image,
#         detections=detections,
#         save_path="output.jpg"  # Optional: save the visualization
#     )

#     # Print detections
#     logger.info("\nDetections:")
#     print(tb.tabulate(detections, headers=['x1', 'y1', 'x2', 'y2', 'conf', 'cls'], tablefmt='grid'))
