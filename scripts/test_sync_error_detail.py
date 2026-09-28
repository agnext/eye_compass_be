"""What a failed sync leaves behind on the record.

    /home/nvidia/.virtualenvs/eye_compass/bin/python scripts/test_sync_error_detail.py

Result.sync_error is not shown to the operator anywhere — History and the
saved-record screen show the sync status and nothing else. It exists for
whoever is diagnosing a failure from the database afterwards, so what matters
about it is that the evidence survives: which exception it was, which status
code came back, and whatever Qualix said. These checks pin that down, and that
it still fits the column.
"""
import sys
sys.path.insert(0, '.')
import requests

from app.core.config import settings
# _use_keycloak reads this, and the Keycloak path would try to fetch a real
# service token. Nothing here is about which identity syncing uses.
settings.AUTH_PROVIDER = "legacy"

from app.services.sync_service import (  # noqa: E402
    SyncService,
    _MAX_ERROR_DETAIL,
    _readable_qualix_error,
)


def service():
    s = SyncService()
    s.access_token = "test-token"          # enough for is_authenticated
    return s


def raising(exc):
    def _post(body, attempt):
        raise exc
    return _post


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


# 1. The failure from the field report: DNS cannot resolve the host. The class
#    name is the part that separates this from a refused connection or a
#    timeout, and str(exc) alone does not carry it.
dns = requests.exceptions.ConnectionError(
    "HTTPSConnectionPool(host='dev.perfeqtfoods.com', port=443): Max retries "
    "exceeded with url: /api/asu/gateway/assaying-dev/api/scan/v2/post-visio "
    "(Caused by NewConnectionError('Failed to establish a new connection: "
    "[Errno -3] Temporary failure in name resolution'))")
s = service()
s._post_analysis = raising(dns)
status, code, detail = s.post_analysis_data({"scan_data": {"sample_id": "T1"}})
assert (status, code) == ("0", "exception"), (status, code)
assert detail.startswith("ConnectionError: "), detail
assert "name resolution" in detail, detail
print("1. network failure -> %s" % detail[:96])

# 2. Every exception keeps its own class name, including the ones that are
#    subclasses of each other (ConnectTimeout is both a Timeout and a
#    ConnectionError, and collapsing them would lose which one happened).
for exc, expect in ((requests.exceptions.ConnectTimeout("c"), "ConnectTimeout: "),
                    (requests.exceptions.ReadTimeout("r"), "ReadTimeout: "),
                    (requests.exceptions.SSLError("bad handshake"), "SSLError: "),
                    (ValueError("something else"), "ValueError: ")):
    s = service()
    s._post_analysis = raising(exc)
    _, _, detail = s.post_analysis_data({"scan_data": {}})
    assert detail.startswith(expect), (expect, detail)
print("2. each exception type is distinguishable from the stored detail alone")

# 3. A long exception is truncated, not dropped — sync_error is a column, and
#    an over-long insert failing would lose the record's status with it.
s = service()
s._post_analysis = raising(RuntimeError("x" * 5000))
_, _, detail = s.post_analysis_data({"scan_data": {}})
assert len(detail) == _MAX_ERROR_DETAIL, len(detail)
print("3. an oversized exception is truncated to %s chars" % _MAX_ERROR_DETAIL)

# 4. An answer that is not 200 or 400 is retryable; the status code and the
#    body both survive onto the record.
s = service()
s._post_analysis = lambda body, attempt: FakeResponse(502, "<html>Bad Gateway</html>")
status, code, detail = s.post_analysis_data({"scan_data": {}})
assert (status, code) == ("0", "http_502"), (status, code)
assert detail.startswith("HTTP 502: "), detail
assert "Bad Gateway" in detail, detail
print("4. retryable HTTP failure -> %s" % detail)

# 5. A 400 is terminal and Qualix explains it. That message is the one thing
#    here nothing else can reconstruct, so it is lifted out with its code.
s = service()
s._post_analysis = lambda body, attempt: FakeResponse(
    400, '{"error-code": "E123", "error-message": "Device does not exist"}')
status, code, detail = s.post_analysis_data({"scan_data": {}})
assert (status, code) == ("2", "bad_request"), (status, code)
assert detail == "Device does not exist (Qualix error E123)", detail
print("5. rejection -> %s" % detail)

# 6. A body that is not that shape is kept as-is rather than summarised away.
assert _readable_qualix_error('{"message": "Sample already recorded"}') == "Sample already recorded"
assert _readable_qualix_error("") == "Qualix gave no reason."
assert _readable_qualix_error("<html>502</html>") == "<html>502</html>"
assert len(_readable_qualix_error("y" * 5000)) == _MAX_ERROR_DETAIL
print("6. an unrecognised rejection body is evidence too, and is kept")

print("ALL SYNC ERROR DETAIL CHECKS PASSED")
