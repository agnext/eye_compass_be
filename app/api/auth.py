from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.services.sync_service import SyncService
import os

router = APIRouter()

class LoginRequest(BaseModel):
    username: str
    password: str

@router.post("/login")
def login(request: LoginRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # Authenticate against local environment (legacy config.INI credentials)
    # This ensures the edge device can authenticate operators even offline
    valid_user = os.getenv("QUALIX_USERNAME")
    valid_pass = os.getenv("QUALIX_PASSWORD")

    if not valid_user or not valid_pass:
        raise HTTPException(status_code=500, detail="Server authentication not configured")

    # Normalize usernames for comparison (strip @agnext.in if present)
    req_user_base = request.username.split("@")[0] if request.username else ""
    valid_user_base = valid_user.split("@")[0] if valid_user else ""

    if req_user_base == valid_user_base and request.password == valid_pass:
        # Trigger config sync in background
        svc = SyncService()
        background_tasks.add_task(svc.sync_commodity_config, db)
        
        return {"status": "success", "token": "dummy_offline_token"}
    else:
        raise HTTPException(status_code=401, detail="Invalid credentials")
