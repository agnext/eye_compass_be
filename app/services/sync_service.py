"""
Qualix + Google Sheets synchronisation.

Ports api_handle.py (OAuth login, config fetch, analysis POST) and
sheet_update.py (row append) onto the FastAPI backend.

Two contracts from the legacy system are preserved exactly because the retry
worker and the History screen both depend on them:

  * post_analysis_data returns a three-valued status (api_handle.py:136-143):
        '1' HTTP 200 — accepted
        '2' HTTP 400 — rejected by Qualix; terminal, never retry
        '0' anything else — not delivered; the retry worker will try again
  * The Qualix payload is serialised with str(dict).replace("'", '"'), which is
    what the endpoint expects. json.dumps is used here instead — it produces
    the same valid JSON without breaking on apostrophes in vendor or brand
    names, which the legacy string hack corrupts.
"""

import logging
import os
from typing import Optional, Tuple

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

from app.core.config import settings
from app.models.schema import (
    BrandDetails,
    ClientInfo,
    CommodityDetails,
    SurveyorDetails,
    VendorDetails,
)

logger = logging.getLogger(__name__)


class SyncService:
    """Holds one Qualix session. A module-level instance is shared so the token
    obtained at login is reused for config fetches and result posts, exactly as
    the legacy api_handler object was."""

    def __init__(self):
        self.access_token = ""
        self.cookie = {}
        self.customer_id = ""
        self.first_name = ""
        self.customer_name = ""

        base = settings.QUALIX_API_URL
        if not base.endswith("/"):
            base += "/"
        self.oauth_uri_get = base + settings.OAUTH_URI_GET
        self.oauth_uri_post = base + settings.OAUTH_URI_POST
        self.analysis_post_uri = base + settings.ANALYSIS_POST_URI
        self.commodity_uri = base + settings.CONFIG_URI

    # ------------------------------------------------------------------
    @property
    def is_authenticated(self) -> bool:
        return bool(self.access_token)

    def login_qualix(self, username: str, password: str) -> bool:
        """OAuth against Qualix. Port of api_handle.handle_login (api_handle.py:59-102)."""
        try:
            session = requests.Session()
            session.headers["User-Agent"] = "Mozilla/5"

            session.get(
                self.oauth_uri_get,
                params={"response_type": "code", "client_id": "client-mobile"},
                timeout=15,
            )
            self.cookie = session.cookies.get_dict()

            encoder = MultipartEncoder(
                fields={
                    "Signin": "Sign+In",
                    "bearer": "mobile",
                    "username": username,
                    "password": password,
                }
            )
            response = session.post(
                self.oauth_uri_post,
                data=encoder,
                params={"bearer": "mobile"},
                headers={"Content-Type": encoder.content_type},
                cookies=self.cookie,
                timeout=30,
            )

            if response.status_code != 200:
                logger.error("Qualix login rejected: HTTP %s", response.status_code)
                return False

            payload = response.json()
            self.access_token = payload.get("access_token", "")
            # Legacy also captures the operator identity (api_handle.py:91-95).
            user = payload.get("user") or {}
            self.customer_id = user.get("user_id", "") or payload.get("user_id", "")
            self.first_name = user.get("first_name", "") or payload.get("first_name", "")
            self.customer_name = user.get("customer_name", "") or payload.get("customer_name", "")
            return bool(self.access_token)
        except Exception as exc:
            logger.error("Qualix login failed: %s", exc)
            return False

    def _auth_headers(self, json_body: bool = False) -> dict:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Cookie": self.cookie.get("JSESSIONID", ""),
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    # ------------------------------------------------------------------
    def post_analysis_data(self, raw_data: dict) -> Tuple[str, str]:
        """POST a scan result. Returns (post_status, error_code).

        post_status is the legacy three-valued flag — see the module docstring.
        """
        import json

        if not self.access_token:
            return "0", "No_access_token"

        try:
            body = json.dumps(raw_data)
            response = requests.post(
                self.analysis_post_uri,
                data=body,
                headers=self._auth_headers(json_body=True),
                timeout=30,
            )
            if response.status_code == 200:
                return "1", "ok"
            if response.status_code == 400:
                logger.error("Qualix rejected the payload (400): %s", response.text[:500])
                return "2", "bad_request"
            logger.error("Qualix POST returned HTTP %s", response.status_code)
            return "0", f"http_{response.status_code}"
        except Exception as exc:
            logger.error("Qualix POST failed: %s", exc)
            return "0", "exception"

    # ------------------------------------------------------------------
    def fetch_config(self) -> Optional[dict]:
        """GET the icompass config. Port of api_handle.get_commodity (api_handle.py:104-115)."""
        if not self.access_token:
            logger.warning("fetch_config called with no access token")
            return None
        try:
            response = requests.get(
                self.commodity_uri,
                params={"response_type": "code", "client_id": "client-mobile"},
                headers=self._auth_headers(),
                timeout=30,
            )
            if response.status_code != 200:
                logger.error("Config fetch failed: HTTP %s", response.status_code)
                return None
            return response.json()
        except Exception as exc:
            logger.error("Config fetch failed: %s", exc)
            return None

    def sync_commodity_config(self, db, username: str = None, password: str = None) -> bool:
        """Fetch and persist the full Qualix config.

        Legacy stored FIVE things from this response (api_handle.py:152-176,
        main.py:2772-2785): client info, surveyors, vendors, brands and
        commodities. Only commodities were being stored, which is why the
        vendor / brand / sorter dropdowns had no data source.

        The whole replacement runs in ONE transaction: the previous version
        deleted the commodity table before it knew the fetch had succeeded, so a
        failure mid-way left the device with no commodities at all.
        """
        if not self.is_authenticated:
            username = username or settings.QUALIX_USERNAME
            password = password or settings.QUALIX_PASSWORD
            if not self.login_qualix(username, password):
                logger.warning("Cannot sync config — Qualix login failed.")
                return False

        config = self.fetch_config()
        if not config:
            return False

        try:
            commodities = config.get("commodityAnalysisModels", []) or []
            surveyors = config.get("surveyorDetails", []) or []
            vendors = config.get("vendorDetails", []) or []
            brands = config.get("brandList", []) or []
            client_name = config.get("clientName")
            image_folder = config.get("imageFolderName")

            if not commodities:
                logger.warning("Config response contained no commodities — not replacing cache.")
                return False

            db.query(CommodityDetails).delete()
            for item in commodities:
                db.add(
                    CommodityDetails(
                        commodity=item.get("commodity_name"),
                        commodity_id=item.get("commodity_code"),
                        analysis=item.get("analysis", []) or [],
                        variety=item.get("varieties", []) or [],
                    )
                )

            db.query(SurveyorDetails).delete()
            for item in surveyors:
                if isinstance(item, dict):
                    db.add(
                        SurveyorDetails(
                            surveyor_id=str(item.get("surveyor_id", item.get("id", ""))),
                            name=item.get("name") or item.get("surveyor_name"),
                        )
                    )
                else:
                    db.add(SurveyorDetails(surveyor_id="", name=str(item)))

            db.query(VendorDetails).delete()
            for item in vendors:
                if isinstance(item, dict):
                    db.add(
                        VendorDetails(
                            vendor_name=item.get("vendor_name") or item.get("name"),
                            vendor_code=str(item.get("vendor_code", item.get("code", ""))),
                        )
                    )
                else:
                    db.add(VendorDetails(vendor_name=str(item), vendor_code=""))

            db.query(BrandDetails).delete()
            seen_brands = set()
            for item in brands:
                name = item.get("brand_name") if isinstance(item, dict) else str(item)
                if name and name not in seen_brands:
                    seen_brands.add(name)
                    db.add(BrandDetails(brand_name=name))

            if client_name or image_folder:
                db.query(ClientInfo).delete()
                db.add(ClientInfo(client_name=client_name, image_folder_name=image_folder))

            db.commit()
            logger.info(
                "Config synced: %s commodities, %s surveyors, %s vendors, %s brands, client=%s",
                len(commodities), len(surveyors), len(vendors), len(seen_brands), client_name,
            )
            return True
        except Exception as exc:
            db.rollback()
            logger.error("Config sync failed, rolled back: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Google Sheets
    # ------------------------------------------------------------------

    def _open_sheet(self):
        if not settings.SHEETS_ENABLED:
            return None
        path = settings.resolve_sheets_credentials()
        if not path:
            logger.warning(
                "Sheets enabled but credentials %r were not found relative to the "
                "backend root or %s.",
                settings.SHEETS_CREDENTIALS_FILE, settings.EYE_COMPASS_SRC,
            )
            return None
        if not settings.SHEETS_SPREADSHEET_ID:
            logger.warning("Sheets enabled but SHEETS_SPREADSHEET_ID is not set.")
            return None
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            creds = Credentials.from_service_account_file(
                path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            client = gspread.authorize(creds)
            client.set_timeout(30)
            return client.open_by_key(settings.SHEETS_SPREADSHEET_ID).sheet1
        except Exception as exc:
            logger.error("Sheets init failed: %s", exc)
            return None

    ANALYSIS_HEADERS = [
        "Others", "Husk", "Frame Count", "Blower FO", "Magnetic FO",
        "FM Stop Count", "Manual Stop Count", "FM Stop Time",
        "Manual Stop Time", "Total Stop Time", "total_fo_detected",
        "Metal Fragments", "Mud balls", "Thread", "Feathers", "Dried Leaves",
        "Plastic Pieces", "Insects", "Sticks", "Stones", "Paper",
        "Toffee wrappers", "Rubber", "Jute Fibers", "Glass pieces", "FM", "NON-FM",
    ]

    def already_in_sheet(self, sheet, start_time: str) -> bool:
        """Duplicate guard. Port of check_start_time_exists (sheet_update.py:76-111).

        Without it, the retry worker appends the same row on every attempt.
        """
        try:
            values = sheet.col_values(11)  # process_start_time column
            return start_time in values
        except Exception as exc:
            logger.warning("Sheets duplicate check failed (%s) — assuming not present", exc)
            return False

    def post_to_sheets(self, raw_data: dict) -> bool:
        sheet = self._open_sheet()
        if not sheet:
            return False

        try:
            scan_data = raw_data.get("scan_data", {}) or {}
            analysis = raw_data.get("analysis", []) or []

            start_time = scan_data.get("process_start_time", "")
            if start_time and self.already_in_sheet(sheet, start_time):
                logger.info("Sheets: row for %s already present, skipping.", start_time)
                return True

            by_name = {a.get("analysisName"): a.get("totalAmount") for a in analysis}

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
                scan_data.get("process_end_time", ""),
            ]
            row += [by_name.get(header, 0) for header in self.ANALYSIS_HEADERS]
            row += [
                a.get("totalAmount", 0)
                for a in analysis
                if a.get("analysisName") not in self.ANALYSIS_HEADERS
            ]
            row.append(scan_data.get("device_id", ""))

            sheet.append_row(row)
            return True
        except Exception as exc:
            logger.error("Sheets POST failed: %s", exc)
            return False


# Shared instance: the token captured at login is reused for config and results,
# so no endpoint needs the operator's password again.
sync_service = SyncService()
