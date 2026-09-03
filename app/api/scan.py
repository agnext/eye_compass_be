import logging
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from typing import Any, Dict, List
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.database_service import DatabaseService
from app.services.sync_service import SyncService
from app.services.s3_service import S3Service

logger = logging.getLogger(__name__)
router = APIRouter()

# Instantiate Singletons for Sync and S3
sync_service = SyncService()
s3_service = S3Service()

class ScanSubmitPayload(BaseModel):
    sample_id: str
    commodity: str
    variety: str
    analysis: List[Dict[str, Any]]
    scan_data: Dict[str, Any]
    username: str = ""
    password: str = ""

def process_background_sync(db: Session, result_id: int, payload: ScanSubmitPayload):
    """
    Background task to sync the result to Qualix API and Google Sheets.
    """
    logger.info(f"Starting background sync for Result ID {result_id} (Sample {payload.sample_id})")
    db_service = DatabaseService(db)
    
    # 1. Login to Qualix API if username/password are provided
    api_success = False
    if payload.username and payload.password:
        if sync_service.login_qualix(payload.username, payload.password):
            api_success = sync_service.post_to_qualix({"scan_data": payload.scan_data, "analysis": payload.analysis})
        else:
            logger.error("Failed to login to Qualix API")
    else:
        logger.warning("No credentials provided for Qualix API, skipping API POST.")
        
    # 2. Post to Google Sheets
    sheets_success = sync_service.post_to_sheets({"scan_data": payload.scan_data, "analysis": payload.analysis})
    
    # 3. Update Sync Status
    if api_success or sheets_success:
        db_service.mark_as_synced(result_id)
        logger.info(f"Successfully synced Result ID {result_id}")
    else:
        logger.warning(f"Failed to sync Result ID {result_id}")


@router.post("/submit")
async def submit_scan_result(
    payload: ScanSubmitPayload, 
    background_tasks: BackgroundTasks, 
    db: Session = Depends(get_db)
):
    """
    Saves the scan result to the local database and triggers a background sync.
    """
    try:
        # Save to DB instantly
        db_service = DatabaseService(db)
        saved_result = db_service.save_scan_result(
            sample_id=payload.sample_id,
            commodity=payload.commodity,
            variety=payload.variety,
            analysis_data={"scan_data": payload.scan_data, "analysis": payload.analysis},
            start_time=payload.scan_data.get("process_start_time")
        )
        
        # Dispatch background synchronization
        background_tasks.add_task(process_background_sync, db, saved_result.id, payload)
        
        return {"status": "success", "message": "Result saved and sync queued", "result_id": saved_result.id}
    except Exception as e:
        logger.error(f"Failed to submit scan: {e}")
        return {"status": "error", "message": str(e)}
