import logging
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from app.core.database import get_db
from app.models.schema import Result

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/")
def get_history(db: Session = Depends(get_db)):
    """
    Fetches historical scan results from the database.
    Returns a list of batch scans sorted from newest to oldest.
    """
    try:
        # Fetch results ordered by ID descending (newest first)
        results = db.query(Result).order_by(Result.id.desc()).limit(100).all()
        
        history_list = []
        for r in results:
            history_list.append({
                "id": r.id,
                "sample_id": r.sample_id,
                "commodity": r.commodity,
                "variety": r.variety,
                "date": r.date or r.start_time, # Fallback to start_time if date is missing
                "sync_status": r.sync_status,
                "analysis": r.result.get("analysis", []) if r.result else [],
                "scan_data": r.result.get("scan_data", {}) if r.result else {}
            })
            
        return {"status": "success", "data": history_list}
    except Exception as e:
        logger.error(f"Failed to fetch history: {e}")
        return {"status": "error", "message": str(e), "data": []}
