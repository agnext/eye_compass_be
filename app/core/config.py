import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from the backend root
_backend_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_backend_root / ".env", override=True)


class Settings:
    """
    Central configuration sourced from environment variables.

    On the real Jetson device, USE_MOCK_CAMERA is NOT set (defaults to False),
    so the real Hikrobot SDK and TensorRT engines are loaded — and if they
    fail, the server crashes loudly instead of silently falling back.

    On your Windows dev machine, set USE_MOCK_CAMERA=true in .env to use
    mock frames and skip hardware imports entirely.
    """

    USE_MOCK_CAMERA: bool = os.getenv("USE_MOCK_CAMERA", "false").lower() == "true"

    # Paths (defaults match the Jetson layout; overridable via env)
    CONFIG_INI_PATH: str = os.getenv(
        "CONFIG_INI_PATH",
        "/home/nvidia/eye_compass/config.INI",
    )
    MODEL_DIR: str = os.getenv(
        "MODEL_DIR",
        "/home/nvidia/eye_compass/ml_m",
    )
    MVS_SDK_PATH: str = os.getenv(
        "MVS_SDK_PATH",
        "/home/nvidia/MVS/Samples/aarch64/Python/MvImport",
    )

    # Camera settings (can be overridden; defaults from config.INI)
    CAMERA_INDEX: int = int(os.getenv("CAMERA_INDEX", "0"))
    CAMERA_EXPOSURE_TIME: float = float(os.getenv("CAMERA_EXPOSURE_TIME", "575.0"))
    CAMERA_GAIN: float = float(os.getenv("CAMERA_GAIN", "1.10"))
    CAMERA_FRAME_QUEUE_SIZE: int = int(os.getenv("CAMERA_FRAME_QUEUE_SIZE", "32"))

    # WebSocket streaming
    STREAM_FPS: int = int(os.getenv("STREAM_FPS", "20"))
    STREAM_JPEG_QUALITY: int = int(os.getenv("STREAM_JPEG_QUALITY", "70"))

    # Database Settings
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/eye_compass"
    )


settings = Settings()
