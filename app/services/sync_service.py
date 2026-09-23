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

# How much of a Qualix error body to keep when it isn't the shape we expect.
# Long enough to be diagnostic, short enough to sit in a table cell.
_MAX_ERROR_DETAIL = 300


def _readable_qualix_error(body: str) -> str:
    """Qualix's error body, reduced to something worth showing an operator.

    It normally answers with {"error-code": "...", "error-message": "..."},
    in which case the message is what matters and the code is worth keeping
    alongside it for support to quote. Anything else (HTML from a proxy, an
    empty body, a gateway error page) is passed through truncated rather than
    discarded — an unhelpful message still beats "Rejected" with no reason.
    """
    import json

    text = (body or "").strip()
    if not text:
        return "Qualix gave no reason."
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:_MAX_ERROR_DETAIL]
    if not isinstance(parsed, dict):
        return text[:_MAX_ERROR_DETAIL]

    message = parsed.get("error-message") or parsed.get("message") or ""
    code = parsed.get("error-code") or ""
    if message and code:
        return f"{message} (Qualix error {code})"
    if message:
        return str(message)[:_MAX_ERROR_DETAIL]
    return text[:_MAX_ERROR_DETAIL]


def _qualix_error_code(body: str) -> str:
    """Just the `error-code` from a Qualix error body, or "" if there isn't one."""
    import json

    try:
        parsed = json.loads((body or "").strip())
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("error-code") or "")


# Qualix's code for "a scan with this sample_id is already recorded". Treated
# as a SUCCESS, not a rejection.
#
# It is the expected answer to re-sending a scan that Qualix already accepted,
# which happens for a specific and entirely normal reason: when Qualix takes
# longer to answer than _POST_TIMEOUT_SECONDS below, the scan is stored on
# their side while this device gives up waiting and records the result as
# undelivered. The retry that follows is then told the sample already exists —
# which is confirmation the data arrived, not a failure. Raising that timeout
# makes this rarer; it cannot make it impossible, because no timeout can
# distinguish "still working" from "never going to answer".
# Marking it '2' (Rejected) instead was actively misleading: it reads as lost
# data on the History screen, and being terminal it would sit there forever.
#
# Safe to treat this way *because batch numbers are globally unique* — device
# code plus epoch seconds, with a UNIQUE constraint behind it (see
# api/batch.py). A sample_id can therefore only already exist at Qualix if
# this same scan reached them before.
#
# The one way that could be wrong is two devices sharing a DEVICE_ID, which
# would let device B's genuinely different scan be waved through as "already
# synced" because device A had used that number. That is exactly what the
# "assign DEVICE_ID from one central list" rule exists to prevent, and it is
# why the acceptance below is logged at WARNING rather than silently.
_QUALIX_ALREADY_RECORDED_CODES = {"12063"}

# How long to wait for the scan POST to answer.
#
# Was 30s, which turned out to sit right on top of how long this endpoint
# actually takes: a measured live call against the dev gateway answered in
# 28.7s, and the same call moments earlier had exceeded 30s and been recorded
# as undelivered. Qualix was receiving and storing those scans either way, so
# the only thing the tight limit achieved was marking delivered scans as
# failed and re-sending them.
#
# Nobody is waiting on this. The post right after a batch runs as a background
# task once /confirm has already answered the browser, and the retry worker has
# no user attached at all; only History's manual Re-sync holds an HTTP request
# open, and that shows a spinner. So the cost of waiting longer is nil, while
# the cost of giving up early is a scan that looks lost.
_POST_TIMEOUT_SECONDS = 90


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

        # Under AUTH_PROVIDER=keycloak the same Qualix endpoints are reached
        # through the Assurance gateway instead of directly: Assurance accepts
        # a Keycloak token, adds whatever headers Qualix needs, and proxies on.
        # The gateway also drops the "portal/" path prefix, so the paths differ
        # as well as the host — hence the separate ASSURANCE_* URIs.
        use_gateway = settings.AUTH_PROVIDER == "keycloak" and settings.ASSURANCE_API_URL
        base = settings.ASSURANCE_API_URL if use_gateway else settings.QUALIX_API_URL
        if not base.endswith("/"):
            base += "/"

        config_uri = settings.ASSURANCE_CONFIG_URI if use_gateway else settings.CONFIG_URI
        analysis_uri = (
            settings.ASSURANCE_ANALYSIS_POST_URI if use_gateway
            else settings.ANALYSIS_POST_URI
        )

        # Only used on the legacy path — the gateway has no Qualix OAuth login
        # to call, since Keycloak issues the token instead.
        self.oauth_uri_get = base + settings.OAUTH_URI_GET
        self.oauth_uri_post = base + settings.OAUTH_URI_POST
        self.analysis_post_uri = base + analysis_uri
        self.commodity_uri = base + config_uri

    # ------------------------------------------------------------------
    @property
    def _use_keycloak(self) -> bool:
        return settings.AUTH_PROVIDER == "keycloak"

    @property
    def is_authenticated(self) -> bool:
        if self._use_keycloak:
            # Syncing authenticates as its own fixed account, so it is
            # "authenticated" whenever that account can get a token — never
            # dependent on an operator having logged in, which matters because
            # an operator who signed in offline has no Keycloak token at all.
            from app.services.keycloak_service import keycloak_service

            return bool(keycloak_service.service_token())
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
        if self._use_keycloak:
            from app.services.keycloak_service import keycloak_service

            headers = {"Authorization": f"Bearer {keycloak_service.service_token() or ''}"}
        else:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Cookie": self.cookie.get("JSESSIONID", ""),
            }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    # ------------------------------------------------------------------
    def post_analysis_data(self, raw_data: dict) -> Tuple[str, str, str]:
        """POST a scan result. Returns (post_status, error_code, error_detail).

        post_status is the legacy three-valued flag — see the module docstring.

        error_detail is what actually went wrong, in Qualix's own words where
        it gave them (e.g. 'Device does not exist'), for storing on the record
        and showing the operator. It used to exist only in this process's log,
        which left a "Rejected" row on the History screen with no way to find
        out why.
        """
        import json

        if not self.is_authenticated:
            return "0", "No_access_token", "Not authenticated with Qualix."

        try:
            body = json.dumps(raw_data)
            # The exact bytes being sent, every time, for all three callers
            # (the post right after a batch, the retry worker, the manual
            # re-sync) — this method is the single chokepoint they share.
            #
            # Logged in full rather than summarised: when Qualix rejects a
            # payload the reason is usually one wrong field, and reconstructing
            # what was actually sent from the database afterwards is exactly
            # the step that was missing. Nothing here is a secret — it is scan
            # data, and the credentials live in the headers, which are
            # deliberately not logged.
            logger.info("[SYNC] POST %s body: %s", self.analysis_post_uri, body)
            response = requests.post(
                self.analysis_post_uri,
                data=body,
                headers=self._auth_headers(json_body=True),
                timeout=_POST_TIMEOUT_SECONDS,
            )
            # The service token can expire between calls. Fetch a fresh one and
            # retry once before treating this as a delivery failure, otherwise
            # every record would sit pending until the next worker cycle.
            if response.status_code == 401 and self._use_keycloak:
                from app.services.keycloak_service import keycloak_service

                logger.info("Assurance returned 401 — refreshing service token and retrying.")
                keycloak_service.invalidate_service_token()
                response = requests.post(
                    self.analysis_post_uri,
                    data=body,
                    headers=self._auth_headers(json_body=True),
                    timeout=_POST_TIMEOUT_SECONDS,
                )
            if response.status_code == 200:
                return "1", "ok", ""
            if response.status_code == 400:
                error_code = _qualix_error_code(response.text)
                if error_code in _QUALIX_ALREADY_RECORDED_CODES:
                    # Qualix already has this scan — see the note on
                    # _QUALIX_ALREADY_RECORDED_CODES. WARNING rather than INFO
                    # because, while the outcome is fine, reaching this line at
                    # all means a delivery was recorded as failed when it had
                    # in fact succeeded, and a run of these is worth noticing.
                    logger.warning(
                        "Qualix already has sample %s (error %s) — counting it as "
                        "delivered, not rejected. An earlier attempt reached them; "
                        "this device just never saw the response.",
                        (raw_data.get("scan_data") or {}).get("sample_id", "?"),
                        error_code,
                    )
                    return "1", "already_recorded", ""
                logger.error("Qualix rejected the payload (400): %s", response.text[:500])
                return "2", "bad_request", _readable_qualix_error(response.text)
            logger.error(
                "Qualix POST returned HTTP %s from %s — %s",
                response.status_code, self.analysis_post_uri, response.text[:500],
            )
            return (
                "0",
                f"http_{response.status_code}",
                f"HTTP {response.status_code}: {_readable_qualix_error(response.text)}",
            )
        except Exception as exc:
            logger.error("Qualix POST failed: %s (%s)", exc, self.analysis_post_uri)
            return "0", "exception", f"Could not reach Qualix: {exc}"

    # ------------------------------------------------------------------
    def fetch_config(self) -> Optional[dict]:
        """GET the icompass config. Port of api_handle.get_commodity (api_handle.py:104-115)."""
        if not self.is_authenticated:
            logger.warning("fetch_config called with no access token")
            return None
        try:
            response = requests.get(
                self.commodity_uri,
                params={"response_type": "code", "client_id": "client-mobile"},
                headers=self._auth_headers(),
                timeout=30,
            )
            if response.status_code == 401 and self._use_keycloak:
                from app.services.keycloak_service import keycloak_service

                logger.info("Assurance returned 401 on config fetch — retrying with a fresh token.")
                keycloak_service.invalidate_service_token()
                response = requests.get(
                    self.commodity_uri,
                    params={"response_type": "code", "client_id": "client-mobile"},
                    headers=self._auth_headers(),
                    timeout=30,
                )
            if response.status_code != 200:
                logger.error(
                    "Config fetch failed: HTTP %s from %s — %s",
                    response.status_code, self.commodity_uri, response.text[:500],
                )
                return None
            return response.json()
        except Exception as exc:
            logger.error("Config fetch failed: %s (%s)", exc, self.commodity_uri)
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
            # Under Keycloak, is_authenticated already obtained (or failed to
            # obtain) the service token itself — there is no separate Qualix
            # login to attempt, so a False here is terminal for this cycle.
            if self._use_keycloak:
                logger.warning("Cannot sync config — Keycloak service login failed.")
                return False
            # The sync account under BOTH providers — deliberately not the
            # emergency/tier-3 device credentials, which exist only to unlock
            # the device and have no business authenticating a sync.
            username = username or settings.SYNC_SERVICE_USERNAME
            password = password or settings.SYNC_SERVICE_PASSWORD
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
                    # Qualix returns this field as camelCase ("surveyorId"),
                    # not snake_case — confirmed against the prod config
                    # response, where dev's had always been an empty list so
                    # this mismatch never actually got exercised.
                    surveyor_id = (
                        item.get("surveyorId")
                        or item.get("surveyor_id")
                        or item.get("id", "")
                    )
                    db.add(
                        SurveyorDetails(
                            surveyor_id=str(surveyor_id),
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
