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


def get_device_id() -> str:
    """Machine id. Port of get_cpu_id (sheet_update.py:13-30).

    Normalised to "" rather than None: the payload must not carry a bare
    Python None into the JSON body.
    """
    if settings.DEVICE_ID:
        return settings.DEVICE_ID
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    value = fh.read().strip()
                    if value:
                        return value
            except Exception:
                continue
    return ""


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
) -> Dict:
    """Assemble the full {"scan_data": {...}, "analysis": [...]} payload.

    session_status  — ScanSession.status() at the time of the scan
    result_payload  — ScanSession.finish() output (result / looker_data / totals)
    batch           — the BatchDetails row for this sample, if one was created
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
