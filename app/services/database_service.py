import datetime
from sqlalchemy.orm import Session
from app.models.schema import Result

class DatabaseService:
    def __init__(self, db: Session):
        self.db = db

    def save_scan_result(self, sample_id: str, commodity: str, variety: str, analysis_data: dict, start_time: str = None) -> Result:
        """
        Saves a scan result into the database.
        Since we use PostgreSQL JSONB, `analysis_data` is saved as a native JSON dict.
        """
        now = datetime.datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        stop_time_str = now.strftime("%H:%M:%S")
        
        if not start_time:
            start_time = stop_time_str

        new_result = Result(
            sample_id=sample_id,
            commodity=commodity,
            variety=variety,
            result=analysis_data,
            date=date_str,
            start_time=start_time,
            stop_time=stop_time_str,
            sync_status="0"
        )
        self.db.add(new_result)
        self.db.commit()
        self.db.refresh(new_result)
        return new_result

    def mark_as_synced(self, result_id: int):
        """
        Marks a result as synced.
        """
        result = self.db.query(Result).filter(Result.id == result_id).first()
        if result:
            result.sync_status = "1"
            self.db.commit()
            return True
        return False

    def get_unsynced_results(self):
        """
        Retrieves all unsynced results.
        """
        return self.db.query(Result).filter(Result.sync_status == "0").all()
