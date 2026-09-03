import sqlite3
import json
import os
import sys

# Add current directory to path so we can import FastAPI app modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.core.database import SessionLocal
from app.models.schema import Result

# Path to the legacy SQLite database
SQLITE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eye_compass", "eye_compass.db")

def migrate_results():
    if not os.path.exists(SQLITE_DB_PATH):
        print(f"Error: Could not find legacy database at {SQLITE_DB_PATH}")
        return

    print("Connecting to legacy SQLite database...")
    sqlite_conn = sqlite3.connect(SQLITE_DB_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    cursor = sqlite_conn.cursor()

    try:
        cursor.execute("SELECT * FROM result")
        rows = cursor.fetchall()
        print(f"Found {len(rows)} historical records in SQLite.")
    except Exception as e:
        print(f"Error reading from SQLite: {e}")
        return

    print("Connecting to new PostgreSQL database...")
    db = SessionLocal()
    
    try:
        migrated_count = 0
        for row in rows:
            # Check if this sample_id already exists to prevent duplicates if script is run twice
            exists = db.query(Result).filter(Result.sample_id == row["sample_id"]).first()
            if exists:
                continue

            # In SQLite, result was stored as a stringified JSON. In Postgres, we use JSONB.
            result_json = None
            if row["result"]:
                try:
                    result_json = json.loads(row["result"])
                except Exception:
                    result_json = {"raw_string": row["result"]} # fallback if badly formatted

            new_result = Result(
                sample_id=row["sample_id"],
                commodity=row["commodity"],
                variety=row["variety"],
                result=result_json,
                date=row["date"],
                start_time=row["start_time"],
                stop_time=row["stop_time"],
                sync_status=row["sync_status"]
            )
            db.add(new_result)
            migrated_count += 1
            
        db.commit()
        print(f"Successfully migrated {migrated_count} records into PostgreSQL!")
        
    except Exception as e:
        print(f"Error migrating to PostgreSQL: {e}")
        db.rollback()
    finally:
        db.close()
        sqlite_conn.close()

if __name__ == "__main__":
    migrate_results()
