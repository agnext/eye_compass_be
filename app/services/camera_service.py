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

        # Throttle to ~20 FPS
        time.sleep(1.0 / settings.STREAM_FPS)
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

    # ---- helpers ----
    @staticmethod
    def _convert_frame(frame) -> np.ndarray:
        """Convert a raw Bayer frame to BGR numpy array (mirrors GrabImage.py)."""
        import cv2
        from ctypes import cast, POINTER, c_ubyte

        width = frame.stFrameInfo.nWidth
        height = frame.stFrameInfo.nHeight

        buffer = cast(
            frame.pBufAddr,
            POINTER(c_ubyte * frame.stFrameInfo.nFrameLen),
        ).contents
        raw = np.frombuffer(buffer, dtype=np.uint8)

        expected = width * height
        if raw.size != expected:
            raise ValueError(f"Buffer size {raw.size} != expected {expected}")

        raw = raw.reshape((height, width))
        image = cv2.cvtColor(raw, cv2.COLOR_BAYER_RG2RGB)
        image = cv2.flip(image, 0)
        image = cv2.flip(image, 1)
        return image.copy()

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

        if self.cam.MV_CC_CreateHandle(dev_info) != 0:
            logger.error("Failed to create camera handle")
            return False

        if self.cam.MV_CC_OpenDevice(self._MV_ACCESS_Exclusive, 0) != 0:
            logger.error("Failed to open camera device")
            return False

        self.cam.MV_CC_SetEnumValue("TriggerMode", self._MV_TRIGGER_MODE_OFF)
        self.cam.MV_CC_SetEnumValue("BalanceWhiteAuto", self._MV_BALANCEWHITE_AUTO_OFF)
        self.cam.MV_CC_SetEnumValue("ExposureAuto", self._MV_EXPOSURE_AUTO_MODE_OFF)
        self.cam.MV_CC_SetEnumValue("GainAuto", self._MV_GAIN_MODE_OFF)

        stParam = self._MVCC_INTVALUE()
        memset(byref(stParam), 0, sizeof(self._MVCC_INTVALUE))
        self.cam.MV_CC_GetIntValue("PayloadSize", stParam)
        self.nPayloadSize = stParam.nCurValue

        self.cam.MV_CC_SetFloatValue("ExposureTime", settings.CAMERA_EXPOSURE_TIME)
        self.cam.MV_CC_SetFloatValue("Gain", settings.CAMERA_GAIN)

        logger.info("Camera initialized successfully")
        return True

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
            return None

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
            self.cam.MV_CC_CloseDevice()
            self.cam.MV_CC_DestroyHandle()
            self.cam = None
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
