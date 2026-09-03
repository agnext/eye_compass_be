import os
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

def test():
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5'
    
    # 1. GET
    url_get = "https://assaying.qualix.ai/portal/oauth/authorize"
    qs_get = {"response_type": "code", "client_id": "client-mobile"}
    r1 = session.get(url_get, params=qs_get)
    print("GET Status:", r1.status_code)
    
    cookie = session.cookies.get_dict()
    
    # 2. POST
    url_post = "https://assaying.qualix.ai/portal/login"
    mp = MultipartEncoder(fields={
        "Signin": "Sign+In",
        "bearer": "mobile",
        "username": "cgi.op3",
        "password": "<REDACTED>"
    })
    
    r2 = session.post(
        url_post,
        data=mp,
        params={"bearer": "mobile"},
        headers={'Content-Type': mp.content_type},
        cookies=cookie
    )
    print("POST Status:", r2.status_code)
    print("POST Response snippet:", r2.text[:500])
    try:
        print("Access token:", r2.json().get('access_token'))
    except:
        print("Not JSON response")

test()
