import os
import boto3
import logging
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig

logger = logging.getLogger(__name__)

class S3Service:
    def __init__(self):
        self.region = os.getenv("S3_REGION", "us-east-2")
        self.identity_pool_id = os.getenv("COGNITO_IDENTITY_POOL", "<REDACTED>")
        self.bucket_name = os.getenv("S3_BUCKET", "agnext-cognito")
        self.boto_cfg = BotoConfig(connect_timeout=15, read_timeout=60, retries={'max_attempts': 3})
        
        self.s3_resource = None
        self._init_client()

    def _init_client(self):
        """Initializes the S3 resource using Cognito Identity Pool."""
        try:
            client = boto3.client('cognito-identity', region_name=self.region, config=self.boto_cfg)
            resp = client.get_id(IdentityPoolId=self.identity_pool_id)
            creds = client.get_credentials_for_identity(IdentityId=resp['IdentityId'])
            
            self.s3_resource = boto3.resource(
                's3',
                aws_access_key_id=creds['Credentials']['AccessKeyId'],
                aws_secret_access_key=creds['Credentials']['SecretKey'],
                aws_session_token=creds['Credentials']['SessionToken'],
                region_name=self.region,
                config=self.boto_cfg
            )
        except Exception as e:
            logger.error(f"Failed to initialize S3 Cognito credentials: {e}")
            self.s3_resource = None

    def upload_file(self, local_file_path: str, s3_key: str):
        """
        Uploads a file to S3 using multipart transfer configuration.
        Designed to be called from a FastAPI BackgroundTask.
        """
        if not self.s3_resource:
            self._init_client()
            if not self.s3_resource:
                logger.error("S3 resource not available. Cannot upload.")
                return False

        if not os.path.exists(local_file_path):
            logger.error(f"File {local_file_path} not found.")
            return False

        config = TransferConfig(
            multipart_threshold=64*1024*1024, # 64MB
            max_concurrency=10,
            multipart_chunksize=64*1024*1024,
            use_threads=True
        )
        
        try:
            self.s3_resource.Bucket(self.bucket_name).upload_file(
                local_file_path, 
                s3_key,
                Config=config
            )
            logger.info(f"Successfully uploaded {local_file_path} to {s3_key}")
            return True
        except Exception as e:
            logger.error(f"Failed to upload {local_file_path} to S3: {e}")
            return False
