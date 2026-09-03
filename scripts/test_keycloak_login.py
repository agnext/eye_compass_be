"""
Try multiple approaches to get a token that Qualix API accepts:
1. admin-cli without openid scope
2. Legacy Qualix login with email username  
3. qualix-frontend with different grant types
"""
import requests
import json

KEYCLOAK_TOKEN_URL = "https://keycloak.agnext.in/realms/CentralIAM/protocol/openid-connect/token"
LEGACY_LOGIN_URL = "https://assaying.qualix.ai/portal/login"
LEGACY_AUTH_URL = "https://assaying.qualix.ai/portal/oauth/authorize"
COMMODITY_URL = "https://assaying.qualix.ai/portal/api/icompass/v1/config"

USERNAME = "cgi.op3@agnext.in"
PASSWORD = "<REDACTED>"

def test_commodity(label, access_token):
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(COMMODITY_URL, headers=headers, timeout=30)
    print(f"  [{label}] Commodity API status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        models = data.get("commodityAnalysisModels", [])
        print(f"  SUCCESS! Got {len(models)} commodities!")
        for m in models[:3]:
            print(f"    - {m.get('commodity_name')} ({m.get('commodity_code')})")
        return True
    else:
        print(f"  Response: {r.text[:150]}")
        return False

# ---- Approach 1: admin-cli WITHOUT openid scope ----
print("=" * 60)
print("Approach 1: admin-cli without openid scope")
print("=" * 60)
resp = requests.post(KEYCLOAK_TOKEN_URL, data={
    "grant_type": "password",
    "client_id": "admin-cli",
    "username": USERNAME,
    "password": PASSWORD,
}, timeout=30)
if resp.status_code == 200:
    token = resp.json()["access_token"]
    print(f"  Token obtained (expires: {resp.json().get('expires_in')}s)")
    test_commodity("admin-cli no scope", token)

# ---- Approach 2: Legacy login with email username ----
print("\n" + "=" * 60)
print("Approach 2: Legacy Qualix login with email username")
print("=" * 60)
from requests_toolbelt.multipart.encoder import MultipartEncoder

session = requests.Session()
session.headers['User-Agent'] = 'Mozilla/5'
r1 = session.get(LEGACY_AUTH_URL, params={"response_type": "code", "client_id": "client-mobile"}, timeout=15)
print(f"  GET authorize: {r1.status_code}")
cookie = session.cookies.get_dict()

mp = MultipartEncoder(fields={
    "Signin": "Sign+In",
    "bearer": "mobile",
    "username": USERNAME,
    "password": PASSWORD
})
r2 = session.post(LEGACY_LOGIN_URL, data=mp, params={"bearer": "mobile"},
                   headers={'Content-Type': mp.content_type}, cookies=cookie, timeout=30)
print(f"  POST login: {r2.status_code}")
print(f"  Response: {r2.text[:200]}")

if r2.status_code == 200:
    try:
        token = r2.json().get("access_token", "")
        if token:
            test_commodity("legacy+email", token)
    except:
        pass

# ---- Approach 3: Legacy login with short username ----
print("\n" + "=" * 60)
print("Approach 3: Legacy Qualix login with short username (cgi.op3)")
print("=" * 60)
session2 = requests.Session()
session2.headers['User-Agent'] = 'Mozilla/5'
r3 = session2.get(LEGACY_AUTH_URL, params={"response_type": "code", "client_id": "client-mobile"}, timeout=15)
cookie2 = session2.cookies.get_dict()

mp2 = MultipartEncoder(fields={
    "Signin": "Sign+In",
    "bearer": "mobile",
    "username": "cgi.op3",
    "password": PASSWORD
})
r4 = session2.post(LEGACY_LOGIN_URL, data=mp2, params={"bearer": "mobile"},
                    headers={'Content-Type': mp2.content_type}, cookies=cookie2, timeout=30)
print(f"  POST login: {r4.status_code}")
print(f"  Response: {r4.text[:200]}")

print("\nDone!")
