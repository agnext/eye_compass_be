"""
Qualix datagram assembly.

Port of generate_datagram (main.py:1663-1710). The legacy version read its 21
scan_data fields straight off the Qt widgets; here they come from the persisted
BatchDetails row plus the ScanSession, so the payload is built server-side and
does not depend on the browser sending them back.

Every field the legacy payload carried is present. The previous implementation
sent three, which meant Qualix and the Google Sheet received almost nothing.
"""

import logging
import os
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from app.core.config import settings
from app.models.schema import BatchDetails, CommodityDetails

logger = logging.getLogger(__name__)

ANALYSIS_TYPE = "ICOMPASS"

# The Qualix "analysis" array below deliberately merges ScanSession.finish()'s
# `result` (item/count breakdown), `looker_data` (stop-time/frame-count
# metrics), and `total_fo_detected` into one flat list — that's what Qualix's
# schema wants, and it's also exactly what legacy's own History-detail table
# shows (set_history_options_assessment, main.py:2181-2205, confirmed against
# a real prod device) — unlike the FRESH-submit results table
# (populate_result_table, main.py:2141-2152, called with ONLY
# create_results()'s output at main.py:1689), which only ever sees the
# item/count breakdown and never these extra rows.


def get_device_id() -> str:
    """Machine id. Port of get_cpu_id (sheet_update.py:13-30).

    Legacy's real get_cpu_id() has no config fallback at all — it's purely
    /etc/machine-id -> /var/lib/dbus/machine-id, full stop; config.INI's
    CONFIG_SETTINGS.device_id is never read by any legacy code path (confirmed
    dead in main.py/api_handle.py/sheet_update.py). settings.DEVICE_ID is kept
    here only as a last-resort fallback for a device with neither file (e.g. a
    non-systemd dev environment), tried AFTER the real machine-id files, not
    before them. (DEVICE_ID is now the short 2-character batch-id code — see
    batch.py — but this fallback path is a rare dev-only case and any non-empty
    string is fine here; it isn't reused as device_serial_no any more.)

    Normalised to "" rather than None: the payload must not carry a bare
    Python None into the JSON body.
    """
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    value = fh.read().strip()
                    if value:
                        return value
            except Exception:
                continue
    return settings.DEVICE_ID or ""


def resolve_variety_id(db, commodity: str, variety_code: str) -> str:
    """Look up variety_id from the cached Qualix config.

    Port of get_variety_id (main.py:1248-1271), including its "1" fallback.
    Without this the Qualix record loses its variety linkage.
    """
    try:
        row = (
            db.query(CommodityDetails)
            .filter(CommodityDetails.commodity == commodity)
            .first()
        )
        if row and row.variety:
            for item in row.variety:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("variety_code", "")).lower()
                if code == str(variety_code or "").lower():
                    return str(item.get("variety_id", "1"))
    except Exception as exc:
        logger.error("resolve_variety_id failed: %s", exc)
    return "1"


def _fmt_date(value: str) -> str:
    """Legacy sends dates as dd/MM/yyyy (main.py:1687-1688)."""
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return value


def _fmt_process_time(date_str: str, time_str: str) -> str:
    """Legacy format: "dd/MM/yyyy HH:MM:SS" (main.py:1690-1696)."""
    if not date_str:
        return time_str or ""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        d = date_str
    return f"{d} {time_str}".strip()


def build_datagram(
    db,
    session_status: Dict,
    result_payload: Dict,
    batch: Optional[BatchDetails] = None,
    surveyor_name: str = "",
    operator_id: str = "",
) -> Dict:
    """Assemble the full {"scan_data": {...}, "analysis": [...]} payload.

    session_status  — ScanSession.status() at the time of the scan
    result_payload  — ScanSession.finish() output (result / looker_data / totals)
    batch           — the BatchDetails row for this sample, if one was created
    operator_id     — Qualix's own user_id for whoever was logged in when this
                       was submitted (from the caller's session — see
                       api/scan.py's /submit). Sent alongside device_serial_no
                       and warehouse_name so Qualix can map location explicitly
                       from the payload, instead of inferring it from whichever
                       account authenticated the sync post — which is a fixed
                       account, not necessarily this operator (see
                       docs/keycloak_integration/4 - assurance_gateway.md).
                       Baked into the datagram at build time rather than looked
                       up again at sync time, so it survives unchanged through
                       the retry worker and manual resync, which only ever
                       resend this same stored JSON blob.
    """
    b = batch
    commodity = session_status.get("commodity", "")
    variety = session_status.get("variety", "")

    scan_data = {
        "sample_id": result_payload.get("sample_id", ""),
        "uuid": str(uuid.uuid1()),
        "commodity_name": (b.product_name if b and b.product_name else commodity),
        "variety_name": (b.product_code if b and b.product_code else variety),
        "surveyor_name": surveyor_name or (b.sorter_name if b else "") or "",
        "image_unique_id": session_status.get("image_unique_id", ""),
        "inspection_date": int(time.time() * 1000),
        "batch_no": result_payload.get("sample_id", ""),
        "weight": (b.sorting_quantity if b else "") or "",
        "weight_unit": "kg",
        "variety_id": resolve_variety_id(db, commodity, variety),
        "vendor_name": (b.vendor_name if b else "") or "",
        "vendor_code": (b.vendor_code if b else "") or "",
        "po": (b.po_number if b else "") or "",
        "brand": (b.brand if b else "") or "",
        "manufacturing_date": _fmt_date(b.manufacturing_date if b else ""),
        "receiving_date": _fmt_date(b.receiving_date if b else ""),
        "site_code": (b.site_code if b else "") or "",
        "process_start_time": _fmt_process_time(
            result_payload.get("date", ""), result_payload.get("start_time", "")
        ),
        "process_end_time": _fmt_process_time(
            result_payload.get("date", ""), result_payload.get("end_time", "")
        ),
        "device_id": get_device_id(),
        # Explicit location-mapping trio (see this function's docstring). Not
        # to be confused with device_id above, which is the machine's own
        # /etc/machine-id fingerprint — device_serial_no is the human-assigned
        # per-device identifier from settings.DEVICE_CODE. (DEVICE_ID is the
        # short 2-character code used for batch numbers instead — see batch.py.)
        "device_serial_no": settings.DEVICE_CODE,
        "operator_id": operator_id,
        "warehouse_name": settings.WAREHOUSE_NAME,
    }

    analysis: List[Dict] = []
    for name, amount in (result_payload.get("result") or {}).items():
        analysis.append(
            {"analysisName": name, "totalAmount": amount, "analysisType": ANALYSIS_TYPE}
        )
    for name, amount in (result_payload.get("looker_data") or {}).items():
        analysis.append(
            {"analysisName": name, "totalAmount": amount, "analysisType": ANALYSIS_TYPE}
        )
    analysis.append(
        {
            "analysisName": "total_fo_detected",
            "totalAmount": result_payload.get("total_fo_detected", 0),
            "analysisType": ANALYSIS_TYPE,
        }
    )

    return {"scan_data": scan_data, "analysis": analysis}
