import asyncio
import os
import boto3
import configparser
from boto3.s3.transfer import TransferConfig

class S3UploaderTask:
    def __init__(self, scan_res_path="output/"):
        self.scan_res_path = scan_res_path
        self.is_running = False
        self.cognito_s3 = None
        self._init_s3()

    def _init_s3(self):
        try:
            # We will assume a config.INI is present in the deployment environment
            # For modern FastAPI, this should eventually move to Pydantic BaseSettings
            self.parser = configparser.RawConfigParser()
            self.parser.read('config.INI')
            
            client = boto3.client('cognito-identity', region_name=self.parser['S3']['region'])
            resp = client.get_id(IdentityPoolId="<REDACTED>")
            resp = client.get_credentials_for_identity(IdentityId=resp['IdentityId'])
            
            creds = resp['Credentials']
            self.cognito_s3 = boto3.resource(
                's3',
                aws_access_key_id=creds['AccessKeyId'],
                aws_secret_access_key=creds['SecretKey'],
                aws_session_token=creds['SessionToken'],
                region_name=self.parser['S3']['region']
            )
        except Exception as e:
            print(f"S3 init failed: {e}")

    async def start(self):
        """Starts the asynchronous S3 uploading background loop."""
        self.is_running = True
        print("S3 Background Worker Started.")
        while self.is_running:
            await self._run_upload_cycle()
            await asyncio.sleep(60) # Sleep for 60 seconds before checking directory again

    def stop(self):
        self.is_running = False

    async def _run_upload_cycle(self):
        if not self.cognito_s3:
            return

        try:
            path = os.path.join(self.parser["_PATH_"]["parent"], self.scan_res_path)
            if not os.path.exists(path):
                return

            bucket_folder = f"{self.parser['S3']['bucket_folder']}/{self.parser['S3']['client']}/"
            bucket = self.cognito_s3.Bucket(self.parser['S3']['bucket'])
            
            # Offload blocking IO to a separate thread so it doesn't block FastAPI
            await asyncio.to_thread(self._sync_directory_to_s3, path, bucket_folder, bucket)
            
        except Exception as e:
            print(f"Error in S3 upload cycle: {e}")
            self._init_s3() # Re-init credentials if they expired

    def _sync_directory_to_s3(self, local_path, s3_prefix, bucket):
        config = TransferConfig(
            multipart_threshold=64*25, 
            max_concurrency=10,
            multipart_chunksize=64*25, 
            use_threads=True
        )

        for subdir, dirs, files in os.walk(local_path):
            for file in files:
                full_path = os.path.join(subdir, file)
                s3_key = s3_prefix + full_path[len(local_path):].lstrip('\\/')
                
                try:
                    s3_object_size = bucket.Object(s3_key).content_length
                    local_size = os.stat(full_path).st_size
                    if s3_object_size < local_size:
                        bucket.upload_file(full_path, s3_key, Config=config)
                except Exception:
                    # Object doesn't exist yet, safe to upload
                    bucket.upload_file(full_path, s3_key, Config=config)
