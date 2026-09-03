from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.core.database import get_db
from app.models.schema import BatchDetails

router = APIRouter()

class BatchCreate(BaseModel):
    batch_number: Optional[str] = None
    po_number: Optional[str] = None
    manufacturing_date: Optional[str] = None
    vendor_name: Optional[str] = None
    receiving_date: Optional[str] = None
    sorting_quantity: Optional[str] = None
    product_name: Optional[str] = None
    brand: Optional[str] = None
    vendor_code: Optional[str] = None
    site_code: Optional[str] = None
    product_code: Optional[str] = None
    sorter_name: Optional[str] = None

@router.post("/new")
async def create_new_batch(batch: BatchCreate, db: Session = Depends(get_db)):
    try:
        new_batch = BatchDetails(
            batch_number=batch.batch_number,
            po_number=batch.po_number,
            manufacturing_date=batch.manufacturing_date,
            vendor_name=batch.vendor_name,
            receiving_date=batch.receiving_date,
            sorting_quantity=batch.sorting_quantity,
            product_name=batch.product_name,
            brand=batch.brand,
            vendor_code=batch.vendor_code,
            site_code=batch.site_code,
            product_code=batch.product_code,
            sorter_name=batch.sorter_name,
            created_at=datetime.now().isoformat()
        )
        db.add(new_batch)
        db.commit()
        db.refresh(new_batch)
        return {"status": "success", "message": "Batch details saved", "id": new_batch.id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save batch details: {str(e)}")
