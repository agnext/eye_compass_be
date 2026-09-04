"""
On-demand S3 uploads for a single file.

The periodic sweep of the output tree lives in s3_worker.py; this is for
one-off uploads. Credentials are created lazily on first use — the previous
version ran a blocking Cognito network call at module import, which delayed
startup on an offline-first device for a client nothing actually called.
"""

import logging
import os
import threading

from app.core.config import settings

logger = logging.getLogger(__name__)


class S3Service:
    def __init__(self):
        self._resource = None
        self._lock = threading.Lock()

    def _ensure_client(self) -> bool:
        if self._resource is not None:
            return True
        if not settings.S3_IDENTITY_POOL:
            logger.warning("S3 identity pool is not configured; uploads disabled.")
            return False

        with self._lock:
            if self._resource is not None:
                return True
            try:
                import boto3
                from botocore.config import Config as BotoConfig

                cfg = BotoConfig(
                    connect_timeout=15, read_timeout=60, retries={"max_attempts": 3}
                )
                client = boto3.client(
                    "cognito-identity", region_name=settings.S3_REGION, config=cfg
                )
                identity = client.get_id(IdentityPoolId=settings.S3_IDENTITY_POOL)
                creds = client.get_credentials_for_identity(
                    IdentityId=identity["IdentityId"]
                )["Credentials"]

                self._resource = boto3.resource(
                    "s3",
                    aws_access_key_id=creds["AccessKeyId"],
                    aws_secret_access_key=creds["SecretKey"],
                    aws_session_token=creds["SessionToken"],
                    region_name=settings.S3_REGION,
                    config=cfg,
                )
                return True
            except Exception as exc:
                logger.error("Failed to initialise S3 Cognito credentials: %s", exc)
                self._resource = None
                return False

    def upload_file(self, local_file_path: str, s3_key: str) -> bool:
        if not self._ensure_client():
            return False
        if not os.path.exists(local_file_path):
            logger.error("File not found: %s", local_file_path)
            return False

        from boto3.s3.transfer import TransferConfig

        transfer = TransferConfig(
            multipart_threshold=8 * 1024 * 1024,
            multipart_chunksize=8 * 1024 * 1024,
            max_concurrency=4,
            use_threads=True,
        )
        try:
            self._resource.Bucket(settings.S3_BUCKET).upload_file(
                local_file_path, s3_key, Config=transfer
            )
            logger.info("Uploaded %s -> %s", local_file_path, s3_key)
            return True
        except Exception as exc:
            # Cognito credentials expire; drop them so the next call re-issues.
            if "ExpiredToken" in str(exc) or "InvalidAccessKeyId" in str(exc):
                self._resource = None
            logger.error("Failed to upload %s: %s", local_file_path, exc)
            return False


s3_service = S3Service()
