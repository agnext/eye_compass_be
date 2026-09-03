import os
import sys
from pathlib import Path
import json

# Add the backend dir to path so we can import app modules
backend_dir = Path(r"C:\Users\milin\OneDrive\Desktop\Desktop\Eye Compass\eye_compass_be")
sys.path.append(str(backend_dir))

from app.core.database import SessionLocal
from app.services.sync_service import SyncService
from app.models.schema import CommodityDetails

def test_sync():
    print("Testing Qualix Sync...")
    
    db = SessionLocal()
    try:
        service = SyncService()
        
        # Test login
        username = os.getenv("QUALIX_USERNAME", "cgi.op3")
        password = os.getenv("QUALIX_PASSWORD", "<REDACTED>")
        
        print(f"Logging in as {username}...")
        success = service.login_qualix(username, password)
        if not success:
            print("Failed to login to Qualix!")
            return
            
        print(f"Login successful! Access token length: {len(service.access_token)}")
        
        # Test fetch
        print("Fetching commodities from API...")
        headers = {
            'Authorization': f'Bearer {service.access_token}',
            'Cookie': service.cookie.get('JSESSIONID', '')
        }
        import requests
        res = requests.get(service.commodity_uri, headers=headers, timeout=30)
        
        if res.status_code != 200:
            print(f"Failed to fetch config, status {res.status_code}")
            return
            
        config_data = res.json()
        models = config_data.get("commodityAnalysisModels", [])
        print(f"Qualix returned {len(models)} commodities.")
        
        if len(models) > 0:
            print(f"First commodity: {models[0].get('commodity_name')} ({models[0].get('commodity_code')})")
            
        # Run the actual sync function
        print("\nRunning full sync_commodity_config function to populate database...")
        sync_success = service.sync_commodity_config(db)
        print(f"Sync function returned: {sync_success}")
        
        # Verify DB
        db_items = db.query(CommodityDetails).all()
        print(f"\nDatabase now has {len(db_items)} items!")
        
    except Exception as e:
        print(f"Error during test: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    test_sync()
