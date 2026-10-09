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

# Logging is configured in logger.py - don't override it here
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s - %(levelname)s - %(message)s',
#     datefmt='%Y-%m-%d %H:%M:%S'
# )
logger = logging.getLogger(__name__)

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
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info(f"Initializing TensorRT inference with engine: {os.path.basename(engine_path)}")
        logger.info(f"Using device: {self.device}")
        logger.info(f"Input image size: {img_size}")
        
        self.engine = self._load_engine()
        self.context = self.engine.create_execution_context()
        
        # Get input/output binding names and shapes
        self.input_binding = self.engine.get_tensor_name(0)
        self.output_binding = self.engine.get_tensor_name(1)
        self.input_shape = tuple(self.engine.get_tensor_shape(self.input_binding))
        self.output_shape = tuple(self.engine.get_tensor_shape(self.output_binding))

    def _load_engine(self):
        """Load TensorRT engine from file"""
        TRT_LOGGER = trt.Logger(trt.Logger.ERROR)
        with open(self.engine_path, "rb") as f, trt.Runtime(TRT_LOGGER) as runtime:
            return runtime.deserialize_cuda_engine(f.read())

    def preprocess_image(self, img):
        """
        Preprocess image for inference
        
        Args:
            img (np.ndarray): Input image in BGR format
            
        Returns:
            tuple: (processed image tensor, original image shape, ratio, padding)
        """
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img, ratio, dwh = letterbox(img, new_shape=self.img_size, stride=32, auto=False)
        img = img.transpose(2, 0, 1)
        img = np.ascontiguousarray(img)
        
        img = torch.from_numpy(img).to(self.device).float()
        img /= 255.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
            
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
        # logger.debug(f"Postprocessing detections with conf_thres={conf_thres}, iou_thres={iou_thres}")
        output_torch = non_max_suppression(output, conf_thres=conf_thres, iou_thres=iou_thres)
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
                    
                    xc -= dwh[0]
                    yc -= dwh[1]
                    xc /= ratio[0]
                    yc /= ratio[1]
                    w /= ratio[0]
                    h /= ratio[1]

                    xmin = int(xc - w/2)
                    xmax = int(xc + w/2)
                    ymin = int(yc - h/2)
                    ymax = int(yc + h/2)
                    
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
        # Preprocess image
        img, ratio, dwh = self.preprocess_image(image)
        
        # Allocate GPU memory for input and output
        d_input = img.to(self.device)
        d_output = torch.empty(self.output_shape, dtype=torch.float32, device=self.device)
        
        # Run inference
        bindings = [int(d_input.data_ptr()), int(d_output.data_ptr())]
        self.context.execute_v2(bindings)
        
        # Postprocess detections
        detections = self.postprocess_detections(d_output.cpu(), ratio, dwh, conf_thres, iou_thres)
        print(conf_thres,iou_thres)
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
