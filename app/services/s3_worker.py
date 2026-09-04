"""
Background S3 uploader.

Port of s3_upload.py's s3Uploading QThread (s3_upload.py:52-158), which walked
the output tree and uploaded anything not already in the bucket at full size.

Three things were wrong with the previous version and are fixed here:
  * It was never started — the class was defined and nothing instantiated it.
    app/main.py's lifespan now runs it.
  * It read config.INI from the process working directory, which under the
    systemd WorkingDirectory does not exist. It now uses `settings`.
  * The Cognito identity pool was the literal string "<REDACTED>".

The S3 key prefix follows legacy: "<bucket_folder>/<client>/<relative path>",
where bucket_folder and client come from the Qualix client info cached at login
(main.py:2781-2786), falling back to the configured values.
"""

import asyncio
import logging
import os
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


class S3UploaderTask:
    def __init__(self):
        self.is_running = False
        self.bucket = None
        self._resource = None

    # ------------------------------------------------------------------
    def _init_s3(self) -> bool:
        if not settings.S3_IDENTITY_POOL:
            logger.warning(
                "S3 disabled: no Cognito identity pool configured "
                "(set AWS_IDENTITY_POOL_ID in .env)."
            )
            return False
        try:
            import boto3
            from botocore.config import Config as BotoConfig

            cfg = BotoConfig(
                connect_timeout=15, read_timeout=60, retries={"max_attempts": 3}
            )
            client = boto3.client("cognito-identity", region_name=settings.S3_REGION, config=cfg)
            identity = client.get_id(IdentityPoolId=settings.S3_IDENTITY_POOL)
            creds = client.get_credentials_for_identity(IdentityId=identity["IdentityId"])[
                "Credentials"
            ]

            self._resource = boto3.resource(
                "s3",
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretKey"],
                aws_session_token=creds["SessionToken"],
                region_name=settings.S3_REGION,
                config=cfg,
            )
            self.bucket = self._resource.Bucket(settings.S3_BUCKET)
            logger.info("S3 credentials initialised for bucket %s", settings.S3_BUCKET)
            return True
        except Exception as exc:
            logger.error("S3 init failed: %s", exc)
            self._resource = None
            self.bucket = None
            return False

    def _key_prefix(self) -> str:
        """Prefix from the Qualix client info if we have it, else config."""
        folder = settings.S3_BUCKET_FOLDER
        client = settings.S3_CLIENT
        try:
            from app.core.database import SessionLocal
            from app.models.schema import ClientInfo

            db = SessionLocal()
            try:
                info = db.query(ClientInfo).first()
                if info:
                    folder = info.image_folder_name or folder
                    client = info.client_name or client
            finally:
                db.close()
        except Exception as exc:
            logger.debug("Could not read client info for S3 prefix: %s", exc)

        parts = [p.strip("/") for p in (folder, client) if p]
        return ("/".join(parts) + "/") if parts else ""

    # ------------------------------------------------------------------
    async def start(self):
        self.is_running = True
        logger.info("S3 background worker started.")
        try:
            while self.is_running:
                try:
                    await asyncio.to_thread(self._run_cycle)
                except Exception as exc:
                    logger.error("S3 upload cycle error: %s", exc)
                await asyncio.sleep(settings.S3_UPLOAD_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("S3 background worker stopped.")
            raise

    def stop(self):
        self.is_running = False

    # ------------------------------------------------------------------
    def _run_cycle(self):
        if self.bucket is None and not self._init_s3():
            return

        prefix = self._key_prefix()
        for sub in ("output", "output_frame"):
            root = os.path.join(settings.OUTPUT_DIR, sub)
            if os.path.isdir(root):
                self._sync_directory(root, prefix + sub + "/")

    def _sync_directory(self, local_root: str, s3_prefix: str):
        from boto3.s3.transfer import TransferConfig

        # Legacy used 1600-byte thresholds (s3_upload.py:143-147), which forces
        # multipart on essentially every file. 8 MB is the sane equivalent for
        # the image sizes actually being uploaded.
        transfer = TransferConfig(
            multipart_threshold=8 * 1024 * 1024,
            multipart_chunksize=8 * 1024 * 1024,
            max_concurrency=4,
            use_threads=True,
        )

        for subdir, _dirs, files in os.walk(local_root):
            for name in files:
                full_path = os.path.join(subdir, name)
                rel = os.path.relpath(full_path, local_root).replace(os.sep, "/")
                key = s3_prefix + rel
                try:
                    local_size = os.stat(full_path).st_size
                    try:
                        remote_size = self.bucket.Object(key).content_length
                    except Exception:
                        remote_size = -1  # not present yet

                    if remote_size < local_size:
                        self.bucket.upload_file(full_path, key, Config=transfer)
                        logger.debug("Uploaded %s -> %s", full_path, key)
                except Exception as exc:
                    logger.error("S3 upload failed for %s: %s", full_path, exc)
                    # Credentials expire after an hour; re-init and let the next
                    # cycle pick up where this one stopped.
                    if "ExpiredToken" in str(exc) or "InvalidAccessKeyId" in str(exc):
                        self._init_s3()
                        return
