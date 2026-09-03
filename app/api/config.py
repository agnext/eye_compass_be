from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.models.schema import CommodityDetails
from app.services.sync_service import SyncService
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/commodities")
def get_commodities(db: Session = Depends(get_db)):
    """
    Returns structured commodities exactly like the legacy UI logic:
    {
      "commodities": ["Kabuli Chana", "Toor Dal"],
      "varieties": {"Kabuli Chana": [...varieties]},
      "analyses": {"Kabuli Chana": [...fm types]}
    }
    """
    items = db.query(CommodityDetails).all()
    
    commodity_list = []
    variety_dict = {}
    analysis_dict = {}
    
    for item in items:
        comm_name = item.commodity
        if comm_name not in variety_dict:
            commodity_list.append(comm_name)
            variety_dict[comm_name] = []
            analysis_dict[comm_name] = []
            
        # Deduplicate varieties
        existing_v_codes = {v.get("variety_code") for v in variety_dict[comm_name]}
        for v in (item.variety or []):
            if v.get("variety_code") not in existing_v_codes:
                variety_dict[comm_name].append(v)
                existing_v_codes.add(v.get("variety_code"))
                
        # Deduplicate analysis
        for a in (item.analysis or []):
            if a not in analysis_dict[comm_name]:
                analysis_dict[comm_name].append(a)
                
    return {
        "status": "success",
        "commodities": commodity_list,
        "varieties": variety_dict,
        "analyses": analysis_dict
    }

@router.post("/sync")
def sync_config(db: Session = Depends(get_db)):
    """Trigger a manual background sync from Qualix"""
    svc = SyncService()
    success = svc.sync_commodity_config(db)
    return {"status": "success" if success else "failed"}
