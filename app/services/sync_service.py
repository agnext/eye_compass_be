import os
import json
import logging
import requests
import gspread
from requests_toolbelt.multipart.encoder import MultipartEncoder
from google.oauth2.service_account import Credentials
from app.core.config import settings

logger = logging.getLogger(__name__)

class SyncService:
    def __init__(self):
        self.access_token = ""
        self.cookie = {}
        
        base_url = os.getenv("QUALIX_API_URL", "https://assaying.qualix.ai/")
        self.oauth_uri_get = base_url + "portal/oauth/authorize"
        self.oauth_uri_post = base_url + "portal/login"
        self.analysis_post_uri = base_url + "portal/api/scan/v2/post-visio"
        self.commodity_uri = base_url + "portal/api/icompass/v1/config"
        
        # Sheets Settings
        self.sheets_enabled = True
        self.spreadsheet_id = "1WqnBWivIIYHcUiLKkSBSdLxzAe4u4SVqNRzEm2CizGs"
        self.service_account_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "sturdy-lore-271106-75bcb0be8976.json")

    def login_qualix(self, username, password):
        try:
            session = requests.Session()
            session.headers['User-Agent'] = 'Mozilla/5'
            querystring = {"response_type": "code", "client_id": "client-mobile"}
            
            response = session.get(self.oauth_uri_get, params=querystring, timeout=15)
            self.cookie = session.cookies.get_dict()
            
            mp_encoder = MultipartEncoder(fields={
                "Signin": "Sign+In",
                "bearer": "mobile",
                "username": username,
                "password": password
            })
            
            response = session.post(
                self.oauth_uri_post,
                data=mp_encoder,
                params={"bearer": "mobile"},
                headers={'Content-Type': mp_encoder.content_type},
                cookies=self.cookie,
                timeout=30
            )
            
            if response.status_code == 200:
                self.access_token = response.json().get('access_token', '')
                return True
            
            logger.error(f"Qualix login rejected! Status: {response.status_code}, Response: {response.text}")
            return False
        except Exception as e:
            logger.error(f"Qualix login failed: {e}")
            return False

    def post_to_qualix(self, raw_data):
        if not self.access_token:
            return False
            
        try:
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Cookie': self.cookie.get('JSESSIONID', ''),
                'Content-Type': 'application/json'
            }
            
            raw_data_str = str(raw_data).replace("'", '"')
            response = requests.post(self.analysis_post_uri, data=raw_data_str, headers=headers, timeout=30)
            
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Qualix POST failed: {e}")
            return False

    def sync_commodity_config(self, db):
        # 1. Login in background to get access_token
        username = os.getenv("QUALIX_USERNAME")
        password = os.getenv("QUALIX_PASSWORD")
        if not self.login_qualix(username, password):
            logger.warning("Failed to background login to Qualix for config sync.")
            return False
        
        # 2. Fetch config
        try:
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Cookie': self.cookie.get('JSESSIONID', '')
            }
            res = requests.get(self.commodity_uri, headers=headers, timeout=30)
            if res.status_code != 200:
                logger.error(f"Failed to fetch config, status {res.status_code}")
                return False
                
            config_data = res.json()
            models = config_data.get("commodityAnalysisModels", [])
            
            # 3. Save to DB
            from app.models.schema import CommodityDetails
            # Clear old data (full refresh)
            db.query(CommodityDetails).delete()
            
            for commodity in models:
                db_item = CommodityDetails(
                    commodity=commodity.get("commodity_name"),
                    commodity_id=commodity.get("commodity_code"),
                    analysis=commodity.get("analysis", []),
                    variety=commodity.get("varieties", [])
                )
                db.add(db_item)
            
            db.commit()
            logger.info(f"Successfully synced {len(models)} commodities from Qualix.")
            return True
            
        except Exception as e:
            logger.error(f"Failed to sync commodity config: {e}")
            return False

    def initialize_google_sheets(self):
        try:
            scope = ["https://www.googleapis.com/auth/spreadsheets"]
            
            # If the file doesn't exist locally, we can't sync, but we shouldn't crash
            if not os.path.exists(self.service_account_file):
                logger.warning(f"Sheets credentials not found at {self.service_account_file}")
                return None
                
            creds = Credentials.from_service_account_file(self.service_account_file, scopes=scope)
            client = gspread.authorize(creds)
            client.set_timeout(30)
            return client.open_by_key(self.spreadsheet_id).sheet1
        except Exception as e:
            logger.error(f"Sheets init failed: {e}")
            return None

    def post_to_sheets(self, raw_data):
        if not self.sheets_enabled:
            return False
            
        sheet = self.initialize_google_sheets()
        if not sheet:
            return False

        try:
            scan_data = raw_data.get("scan_data", {})
            analysis = raw_data.get("analysis", [])

            # Extract analysis values
            analysis_dict = {a.get("analysisName"): a.get("totalAmount") for a in analysis}

            row = [
                scan_data.get("sample_id", ""),
                scan_data.get("commodity_name", ""),
                scan_data.get("variety_name", ""),
                scan_data.get("weight", ""),
                scan_data.get("brand", ""),
                scan_data.get("vendor_name", ""),
                scan_data.get("vendor_code", ""),
                scan_data.get("manufacturing_date", ""),
                scan_data.get("receiving_date", ""),
                scan_data.get("site_code", ""),
                scan_data.get("process_start_time", ""),
                scan_data.get("process_end_time", "")
            ]

            # Standard columns
            analysis_headers = [
                "Others", "Husk", "Frame Count", "Blower FO", "Magnetic FO", 
                "FM Stop Count", "Manual Stop Count", "FM Stop Time", 
                "Manual Stop Time", "Total Stop Time", "total_fo_detected",
                "Metal Fragments", "Mud balls", "Thread", "Feathers", "Dried Leaves",
                "Plastic Pieces", "Insects", "Sticks", "Stones", "Paper", 
                "Toffee wrappers", "Rubber", "Jute Fibers", "Glass pieces", "FM", "NON-FM"
            ]

            for ah in analysis_headers:
                row.append(analysis_dict.get(ah, 0))

            # Extra columns dynamically appended
            for a in analysis:
                if a.get("analysisName") not in analysis_headers:
                    row.append(a.get("totalAmount", 0))

            row.append(scan_data.get("device_id", ""))

            sheet.append_row(row)
            return True
        except Exception as e:
            logger.error(f"Sheets POST failed: {e}")
            return False
