"""
Camera Service — abstracts the Hikrobot MVS SDK behind a clean interface.

When USE_MOCK_CAMERA=true (set in .env), a MockCameraService is used that
generates synthetic frames so the full WebSocket pipeline can be tested on
any machine without hardware.

When USE_MOCK_CAMERA is NOT set (the default on the real Jetson device),
the real SDK is imported. If that import fails, the server crashes loudly.
"""

import logging
import time
import numpy as np
from abc import ABC, abstractmethod

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class BaseCameraService(ABC):
    """Interface that both the real and mock implementations share."""

    @abstractmethod
    def initialize(self) -> bool:
        """Connect to the camera. Returns True on success."""
        ...

    @abstractmethod
    def start_grabbing(self) -> bool:
        """Begin frame acquisition."""
        ...

    @abstractmethod
    def grab_frame(self) -> np.ndarray | None:
        """Return the next frame as a BGR numpy array, or None on failure."""
        ...

    @abstractmethod
    def stop_grabbing(self) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


# ---------------------------------------------------------------------------
# Mock implementation (for local Windows development)
# ---------------------------------------------------------------------------

class MockCameraService(BaseCameraService):
    """
    Generates synthetic colour-bar frames with a moving timestamp overlay.
    Useful for testing the WebSocket pipeline end-to-end without hardware.
    """

    def __init__(self):
        self._running = False
        self._frame_count = 0
        self._width = 640
        self._height = 480

    def initialize(self) -> bool:
        logger.info("[MockCamera] Initialized (no real hardware)")
        return True

    def start_grabbing(self) -> bool:
        self._running = True
        self._frame_count = 0
        logger.info("[MockCamera] Started grabbing mock frames")
        return True

    def grab_frame(self) -> np.ndarray | None:
        if not self._running:
            return None

        # Create a frame with shifting colour gradient
        frame = np.zeros((self._height, self._width, 3), dtype=np.uint8)

        # Shifting colour bars
        offset = (self._frame_count * 3) % self._width
        for i in range(self._width):
            col_idx = (i + offset) % self._width
            frame[:, i, 0] = int(255 * col_idx / self._width)          # Blue channel
            frame[:, i, 1] = int(255 * (1 - col_idx / self._width))    # Green channel
            frame[:, i, 2] = 128                                        # Red channel

        # Stamp frame number onto the image
        try:
            import cv2
            text = f"MOCK FRAME {self._frame_count}"
            cv2.putText(
                frame, text, (20, self._height // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2,
            )
            timestamp = time.strftime("%H:%M:%S")
            cv2.putText(
                frame, timestamp, (20, self._height // 2 + 40),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1,
            )
        except ImportError:
            pass  # cv2 not available — plain colour bars are fine

        self._frame_count += 1
        # No sleep here: the stream loop in api/camera.py already paces to
        # STREAM_FPS, and throttling in both places halves the effective rate.
        return frame

    def stop_grabbing(self) -> None:
        self._running = False
        logger.info("[MockCamera] Stopped grabbing")

    def close(self) -> None:
        self._running = False
        logger.info("[MockCamera] Closed")


# ---------------------------------------------------------------------------
# Real implementation (Hikrobot MVS SDK — Jetson only)
# ---------------------------------------------------------------------------

class RealCameraService(BaseCameraService):
    """
    Wraps the Hikrobot MvCameraControl SDK.
    Only imported when USE_MOCK_CAMERA is False.
    """

    def __init__(self):
        import sys
        import os
        from ctypes import c_ubyte, byref, sizeof, memset, cast, POINTER

        # Ensure MVS runtime path is available
        if not os.environ.get("MVCAM_COMMON_RUNENV"):
            for candidate in ["/home/nvidia/MVS/lib", "/opt/MVS/lib"]:
                if os.path.isdir(candidate):
                    os.environ["MVCAM_COMMON_RUNENV"] = candidate
                    break

        sys.path.append(settings.MVS_SDK_PATH)
        from MvCameraControl_class import (  # type: ignore
            MvCamera,
            MV_CC_DEVICE_INFO_LIST,
            MV_CC_DEVICE_INFO,
            MV_FRAME_OUT,
            MV_GIGE_DEVICE,
            MV_USB_DEVICE,
            MV_ACCESS_Exclusive,
            MV_TRIGGER_MODE_OFF,
            MV_BALANCEWHITE_AUTO_OFF,
            MV_EXPOSURE_AUTO_MODE_OFF,
            MV_GAIN_MODE_OFF,
            MVCC_INTVALUE,
            MVCC_FLOATVALUE,
            MV_OK,
        )

        self._MvCamera = MvCamera
        self._MV_CC_DEVICE_INFO_LIST = MV_CC_DEVICE_INFO_LIST
        self._MV_FRAME_OUT = MV_FRAME_OUT
        self._MV_GIGE_DEVICE = MV_GIGE_DEVICE
        self._MV_USB_DEVICE = MV_USB_DEVICE
        self._MV_ACCESS_Exclusive = MV_ACCESS_Exclusive
        self._MV_TRIGGER_MODE_OFF = MV_TRIGGER_MODE_OFF
        self._MV_BALANCEWHITE_AUTO_OFF = MV_BALANCEWHITE_AUTO_OFF
        self._MV_EXPOSURE_AUTO_MODE_OFF = MV_EXPOSURE_AUTO_MODE_OFF
        self._MV_GAIN_MODE_OFF = MV_GAIN_MODE_OFF
        self._MVCC_INTVALUE = MVCC_INTVALUE
        self._MVCC_FLOATVALUE = MVCC_FLOATVALUE
        self._MV_OK = MV_OK

        self._cast = cast
        self._POINTER = POINTER
        self._c_ubyte = c_ubyte
        self._byref = byref
        self._sizeof = sizeof
        self._memset = memset
        self._MV_CC_DEVICE_INFO = MV_CC_DEVICE_INFO

        self.cam = None
        self.nPayloadSize = 0
        self._is_gige = False
        self._consecutive_errors = 0

    # ---- helpers ----
    # BayerRG8. Legacy guards on this exact value (GrabImage.py:50) and raises
    # for anything else rather than demosaicing a format it does not understand.
    PIXEL_TYPE_BAYER_RG8 = 0x1080009

    @classmethod
    def _convert_frame(cls, frame) -> np.ndarray:
        """Convert a raw Bayer frame to a numpy image.

        Exact port of convert_frame_to_image (GrabImage.py:42-68), including the
        pixel-format guard. Note there is deliberately NO cv2.flip here: both
        flip calls are commented out in the legacy source, and applying them
        rotates the image 180deg, which reverses the belt's direction of travel
        in image space and breaks ObjectTracker's x/y assumptions.
        """
        import cv2
        from ctypes import cast, POINTER, c_ubyte

        width = frame.stFrameInfo.nWidth
        height = frame.stFrameInfo.nHeight
        pixel_type = frame.stFrameInfo.enPixelType

        if pixel_type != cls.PIXEL_TYPE_BAYER_RG8:
            raise ValueError(
                f"Unsupported pixel type 0x{pixel_type:x}; expected BayerRG8 "
                f"(0x{cls.PIXEL_TYPE_BAYER_RG8:x}). Check the camera's PixelFormat setting."
            )

        buffer = cast(
            frame.pBufAddr,
            POINTER(c_ubyte * frame.stFrameInfo.nFrameLen),
        ).contents
        raw = np.frombuffer(buffer, dtype=np.uint8)

        expected = width * height
        if raw.size != expected:
            raise ValueError(f"Buffer size {raw.size} != expected {expected}")

        raw = raw.reshape((height, width))
        return cv2.cvtColor(raw, cv2.COLOR_BAYER_RG2RGB).copy()

    # ---- interface ----
    def initialize(self) -> bool:
        from ctypes import cast, POINTER, byref, sizeof, memset

        device_list = self._MV_CC_DEVICE_INFO_LIST()
        tl = self._MV_GIGE_DEVICE | self._MV_USB_DEVICE

        ret = self._MvCamera.MV_CC_EnumDevices(tl, device_list)
        if ret != 0 or device_list.nDeviceNum == 0:
            logger.error("No camera device found or enumeration failed")
            return False

        self.cam = self._MvCamera()
        dev_info = cast(
            device_list.pDeviceInfo[settings.CAMERA_INDEX],
            POINTER(self._MV_CC_DEVICE_INFO),
        ).contents
        self._is_gige = dev_info.nTLayerType == self._MV_GIGE_DEVICE

        if self.cam.MV_CC_CreateHandle(dev_info) != 0:
            logger.error("Failed to create camera handle")
            self._release_handle()
            return False

        ret = self.cam.MV_CC_OpenDevice(self._MV_ACCESS_Exclusive, 0)
        if ret != 0:
            logger.error("Failed to open camera device: ret[0x%x]", ret)
            # Without this the handle leaks and the device can stay locked
            # until the Jetson is rebooted.
            self._release_handle()
            return False

        # GigE: negotiate the optimal packet size before streaming, or frames
        # arrive partial/dropped under load (GrabImage.py:662-665).
        if self._is_gige:
            packet_size = self.cam.MV_CC_GetOptimalPacketSize()
            if packet_size > 0:
                ret = self.cam.MV_CC_SetIntValue("GevSCPSPacketSize", packet_size)
                if ret != 0:
                    logger.warning("Could not set GevSCPSPacketSize: ret[0x%x]", ret)
                else:
                    logger.info("GevSCPSPacketSize set to %s", packet_size)
            else:
                logger.warning(
                    "MV_CC_GetOptimalPacketSize returned %s; leaving packet size at default",
                    packet_size,
                )

        self.cam.MV_CC_SetEnumValue("TriggerMode", self._MV_TRIGGER_MODE_OFF)
        self.cam.MV_CC_SetEnumValue("BalanceWhiteAuto", self._MV_BALANCEWHITE_AUTO_OFF)
        self.cam.MV_CC_SetEnumValue("ExposureAuto", self._MV_EXPOSURE_AUTO_MODE_OFF)
        self.cam.MV_CC_SetEnumValue("GainAuto", self._MV_GAIN_MODE_OFF)

        stParam = self._MVCC_INTVALUE()
        memset(byref(stParam), 0, sizeof(self._MVCC_INTVALUE))
        self.cam.MV_CC_GetIntValue("PayloadSize", stParam)
        self.nPayloadSize = stParam.nCurValue

        # Legacy values (GrabImage.py:677-678). The [CAMERA] runtime_* keys in
        # config.INI are not read by any legacy code path, so they are not the
        # source of truth here.
        self.cam.MV_CC_SetFloatValue("ExposureTime", settings.CAMERA_EXPOSURE_TIME)
        self.cam.MV_CC_SetFloatValue("Gain", settings.CAMERA_GAIN)

        # Calibration: white balance ratios, gamma and acquisition frame rate.
        # Without this the camera runs on factory defaults and the colour
        # profile the models were trained against is lost (GrabImage.py:679).
        self._load_feature_file()

        logger.info(
            "Camera initialized (exposure=%.1f gain=%.2f gige=%s)",
            settings.CAMERA_EXPOSURE_TIME,
            settings.CAMERA_GAIN,
            self._is_gige,
        )
        return True

    def _load_feature_file(self) -> None:
        import os

        path = settings.CAMERA_FEATURE_FILE
        if not path or not os.path.exists(path):
            logger.warning(
                "Camera feature file not found at %s — running on factory white "
                "balance and frame rate. Detection results will differ from the "
                "legacy system.",
                path,
            )
            return
        ret = self.cam.MV_CC_FeatureLoad(path)
        if ret != 0:
            logger.error("MV_CC_FeatureLoad(%s) failed: ret[0x%x]", path, ret)
        else:
            logger.info("Camera calibration loaded from %s", path)

    def _release_handle(self) -> None:
        """Destroy a partially-opened handle so the device is not left locked."""
        if self.cam is None:
            return
        try:
            self.cam.MV_CC_CloseDevice()
        except Exception:
            pass
        try:
            self.cam.MV_CC_DestroyHandle()
        except Exception:
            pass
        self.cam = None

    def start_grabbing(self) -> bool:
        if self.cam is None:
            logger.error("Camera not initialized")
            return False
        ret = self.cam.MV_CC_StartGrabbing()
        if ret != 0:
            logger.error(f"Start grabbing failed: ret[0x{ret:x}]")
            return False
        logger.info("Camera started grabbing")
        return True

    def grab_frame(self) -> np.ndarray | None:
        if self.cam is None:
            return None

        from ctypes import byref, sizeof, memset

        stOutFrame = self._MV_FRAME_OUT()
        memset(byref(stOutFrame), 0, sizeof(stOutFrame))

        ret = self.cam.MV_CC_GetImageBuffer(stOutFrame, 1000)
        if ret != self._MV_OK:
            # A hung or unplugged camera must not look like an idle one
            # (legacy logs every non-OK return, GrabImage.py:135-152).
            self._consecutive_errors += 1
            if self._consecutive_errors in (1, 10) or self._consecutive_errors % 100 == 0:
                logger.error(
                    "MV_CC_GetImageBuffer returned 0x%x (%s consecutive)",
                    ret & 0xFFFFFFFF,
                    self._consecutive_errors,
                )
            return None
        self._consecutive_errors = 0

        try:
            frame = self._convert_frame(stOutFrame)
        except Exception as e:
            logger.error(f"Frame conversion error: {e}")
            frame = None
        finally:
            self.cam.MV_CC_FreeImageBuffer(stOutFrame)

        return frame

    def stop_grabbing(self) -> None:
        if self.cam:
            self.cam.MV_CC_StopGrabbing()
            logger.info("Camera stopped grabbing")

    def close(self) -> None:
        if self.cam:
            self._release_handle()
            logger.info("Camera closed")


# ---------------------------------------------------------------------------
# Factory — returns the correct implementation based on env config
# ---------------------------------------------------------------------------

def get_camera_service() -> BaseCameraService:
    """Return a MockCameraService or RealCameraService based on USE_MOCK_CAMERA."""
    if settings.USE_MOCK_CAMERA:
        logger.info("USE_MOCK_CAMERA=true → using MockCameraService")
        return MockCameraService()
    else:
        logger.info("USE_MOCK_CAMERA not set → using RealCameraService (Hikrobot SDK)")
        return RealCameraService()
