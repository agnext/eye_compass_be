"""
Configuration endpoints — the reference data the batch and details forms need.

Legacy populated these dropdowns from five local tables that the login-time
config sync filled (main.py:1135-1184, 2772-2785). Only commodities were being
served, which is why the Vendor / Brand / Sorter fields in the React app had to
be free text and why vendor_code auto-fill was lost.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.models.schema import (
    BrandDetails,
    ClientInfo,
    CommodityDetails,
    SurveyorDetails,
    VendorDetails,
)
from app.services.sync_service import sync_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/commodities")
def get_commodities(db: Session = Depends(get_db)):
    """Commodity -> varieties -> analysis (FM) vocabulary.

    Shape matches what the legacy UI built in populate_commodity /
    populate_variety / populate_fm.
    """
    items = db.query(CommodityDetails).all()

    commodities = []
    codes = {}
    varieties = {}
    analyses = {}

    for item in items:
        name = item.commodity
        if name is None:
            continue
        if name not in varieties:
            commodities.append(name)
            varieties[name] = []
            analyses[name] = []
            codes[name] = item.commodity_id

        seen = {v.get("variety_code") for v in varieties[name] if isinstance(v, dict)}
        for v in item.variety or []:
            if isinstance(v, dict) and v.get("variety_code") not in seen:
                varieties[name].append(v)
                seen.add(v.get("variety_code"))

        for a in item.analysis or []:
            if a not in analyses[name]:
                analyses[name].append(a)

    return {
        "status": "success",
        "commodities": commodities,
        "commodity_codes": codes,
        "varieties": varieties,
        "analyses": analyses,
    }


@router.get("/vendors")
def get_vendors(db: Session = Depends(get_db)):
    """Vendor list. The frontend uses vendor_code to auto-fill the code field
    when a vendor is picked (legacy populate_vendor_code, main.py:1177-1184)."""
    rows = db.query(VendorDetails).all()
    return {
        "status": "success",
        "vendors": [
            {"vendor_name": r.vendor_name, "vendor_code": r.vendor_code} for r in rows
        ],
    }


@router.get("/brands")
def get_brands(db: Session = Depends(get_db)):
    rows = db.query(BrandDetails).all()
    return {"status": "success", "brands": [r.brand_name for r in rows if r.brand_name]}


@router.get("/surveyors")
def get_surveyors(db: Session = Depends(get_db)):
    """Sorter Name options (legacy main.py:914, 1159-1161)."""
    rows = db.query(SurveyorDetails).all()
    return {
        "status": "success",
        "surveyors": [{"surveyor_id": r.surveyor_id, "name": r.name} for r in rows],
    }


@router.get("/client")
def get_client(db: Session = Depends(get_db)):
    row = db.query(ClientInfo).first()
    if not row:
        return {"status": "success", "client_name": "", "image_folder_name": ""}
    return {
        "status": "success",
        "client_name": row.client_name,
        "image_folder_name": row.image_folder_name,
    }


@router.get("/all")
def get_all_config(db: Session = Depends(get_db)):
    """Everything the batch form needs, in one round trip."""
    return {
        "status": "success",
        "commodities": get_commodities(db),
        "vendors": get_vendors(db)["vendors"],
        "brands": get_brands(db)["brands"],
        "surveyors": get_surveyors(db)["surveyors"],
        "client": get_client(db),
    }


@router.post("/sync")
def sync_config():
    """Trigger a config refresh from Qualix.

    Uses its own session rather than the request-scoped one so the transaction
    boundary belongs to the sync, not to the HTTP response.
    """
    db = SessionLocal()
    try:
        ok = sync_service.sync_commodity_config(db)
        return {"status": "success" if ok else "failed"}
    finally:
        db.close()
