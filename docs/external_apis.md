# External API contracts

**What this file is:** every call this backend makes *off the device*, what it
sends, what it gets back, and why it is made at all. If you are debugging a
sync failure, changing a payload field, or pointing a device at a different
environment, start here.

**What this file is not:** the internal REST API the frontend calls. That is
this backend's own surface and is documented alongside the endpoints
themselves.

---

## The calls at a glance

| # | Call | To | When | Auth |
|---|---|---|---|---|
| 1 | `POST` scan result | Qualix (direct) or Assurance gateway | Right after a batch is saved, on the 30-min retry, and on manual re-sync | Fixed sync account |
| 2 | `GET` config | Qualix (direct) or Assurance gateway | On demand (`POST /api/config/sync`), and at login | Fixed sync account |
| 3 | Keycloak token calls | Keycloak | Login, sync, session revalidation | Various — see §3 |
| 4 | `GET` Qualix user profile | Assurance gateway | Once per Keycloak login | **The operator's own token** |
| 5 | Legacy Qualix OAuth login | Qualix | Login, when `AUTH_PROVIDER=legacy` | The operator's credentials |
| 6 | Google Sheets append | Google | After a scan is **accepted** by Qualix | Service account |
| 7 | S3 upload | AWS | Background sweep | Cognito identity pool |

Two settings decide where 1 and 2 go:

- `AUTH_PROVIDER=legacy` → straight to Qualix at `QUALIX_API_URL`, paths
  prefixed `portal/`.
- `AUTH_PROVIDER=keycloak` → through the Assurance gateway at
  `ASSURANCE_API_URL`, **same endpoints without the `portal/` prefix**.

That prefix difference is the single most common cause of a 404 after
switching a device.

---

## 1. `POST` scan result — the one that matters

`sync_service.post_analysis_data()`

| | |
|---|---|
| **URL** | `{QUALIX_API_URL}portal/api/scan/v2/post-visio` (legacy)<br>`{ASSURANCE_API_URL}api/scan/v2/post-visio` (keycloak) |
| **Method** | `POST`, `Content-Type: application/json` |
| **Timeout** | 30s |
| **Body** | the *stored* datagram — see below |

### Why it is made

This is the entire point of the device. A scan that is not delivered here has,
as far as the business is concerned, not happened. Qualix is the system of
record; everything else (the local Postgres, the Google Sheet) is a copy or a
staging area.

### What is in the body

Two top-level keys, `scan_data` and `analysis`, built by
`services/datagram.py::build_datagram()`.

`scan_data` — one object describing the batch:

| Field | Source | Notes |
|---|---|---|
| `sample_id` | the batch number | |
| `uuid` | `uuid.uuid1()` at build time | Stable for the life of the record — see the callout below |
| `commodity_name` | batch form, else the scan session | |
| `variety_name` | batch form, else the scan session | |
| `surveyor_name` | operator's dropdown pick | Free text, typed per batch |
| `image_unique_id` | the scan's output folder name | Ties the payload to the images on disk and in S3 |
| `inspection_date` | `int(time.time() * 1000)` | Epoch **milliseconds** |
| `batch_no` | same value as `sample_id` | Deliberately duplicated; Qualix reads both |
| `weight` | Sorting Quantity | Positive whole number, validated both ends |
| `weight_unit` | hardcoded `"kg"` | |
| `variety_id` | resolved from the synced config | |
| `vendor_name`, `vendor_code`, `po`, `brand`, `site_code` | batch form | |
| `manufacturing_date`, `receiving_date` | batch form | Reformatted to `dd/MM/yyyy` |
| `process_start_time`, `process_end_time` | scan session | `dd/MM/yyyy HH:MM:SS` |
| `device_id` | `/etc/machine-id`, else `/var/lib/dbus/machine-id`, else the `DEVICE_ID` setting | The machine's own fingerprint — **not** the `DEVICE_ID` setting, except on a machine with neither file. See the note below |
| `device_serial_no` | `DEVICE_CODE` setting | What Qualix matches against its registered devices |
| `operator_id` | Qualix's `user.user_id`, from login | Who ran the batch |
| `warehouse_name` | `WAREHOUSE_NAME` setting | Where |

`analysis` — a flat list of `{analysisName, totalAmount, analysisType}`, where
`analysisType` is always `"ICOMPASS"`. It concatenates the per-item result
counts, the Looker rollups, and a final `total_fo_detected` entry.

> ### The three device/operator fields exist to decouple *who posted* from *who scanned*
>
> Qualix originally mapped a scan's location from the email of the account that
> posted it. That cannot work here, because syncing authenticates as one fixed
> account regardless of who is logged in — and it has to, since an operator who
> signed in offline has no Keycloak token at all.
>
> `device_serial_no`, `operator_id` and `warehouse_name` were added so the
> payload states these explicitly. See `keycloak_integration/4 -
> assurance_gateway.md`.

> ### `device_id` is a fourth thing again, and its fallback is a dev-only wart
>
> It is the Linux machine fingerprint, read from `/etc/machine-id` (a 32-char
> hex string), which is what legacy's `get_cpu_id` did. It is not
> `device_serial_no` and not the batch prefix.
>
> On a machine with neither machine-id file — i.e. a Windows or macOS
> development box, never the Jetson — it falls back to `settings.DEVICE_ID`.
> Since `DEVICE_ID` now means "2-character batch prefix", that fallback sends
> something like `"T1"` in a field meant to hold a hardware fingerprint. Real
> devices are unaffected; be aware of it when reading dev-environment payloads.

> ### `uuid` is per *record*, not per *request*
>
> It is generated once when the datagram is built, then stored in the database
> inside `result.result`. Every subsequent delivery attempt — the retry worker
> and the manual re-sync alike — re-sends that same stored JSON, so Qualix sees
> a stable id across all attempts for one scan and can deduplicate on it.
>
> This is inherited from legacy (`main.py:1669`), which behaves the same way
> for the same reason. **Do not** regenerate it at send time; that would silently
> turn every retry into a new scan from Qualix's point of view.
>
> Note it is *not* what protects this device's own database from duplicates —
> that is `result.client_request_id`, a separate key with a separate job. See
> §"Two different uuids" at the end of this file.

### What comes back

| Status | Meaning | `sync_status` stored | Retried? |
|---|---|---|---|
| `200` | Accepted | `'1'` | — |
| `400` + `error-code 12063` | **Already recorded** — Qualix has this scan from an earlier attempt. Counted as **delivered** | `'1'` | — |
| `400` (any other code) | **Rejected** — the payload is wrong, e.g. `{"error-code":"12092","error-message":"Device does not exist"}` | `'2'` | **No.** Terminal. Resending an unchanged payload cannot produce a different answer |
| `401` | Token expired | — | Token refreshed, request retried **once** inline |
| anything else / network failure | Undelivered | `'0'` | Yes, by the 30-minute worker |

### `12063 Sample ID already exists` is a success, not a rejection

This is the expected answer when re-sending a scan Qualix already stored, and
it happens for an ordinary reason: **this endpoint is slow.** A live call
measured against the dev gateway answered in **28.7s**, and the request just
before it had exceeded the then-30s timeout and been filed as undelivered —
even though Qualix had received and stored it. The retry is then told the
sample already exists, which is confirmation the data arrived.

Filing that as `'2'` (Rejected) was actively misleading: it reads as lost data
on the History screen, and being terminal it would have stayed there forever
while the scan sat safely in Qualix all along. It is now recorded as `'1'`,
and the stored `sync_error` is cleared.

Two things make this safe rather than a way to hide real failures:

- **Batch numbers are globally unique** — device code plus epoch seconds, with
  a `UNIQUE` constraint behind them. A `sample_id` can therefore only already
  exist at Qualix if this same scan reached them before. The one way it could
  be wrong is two devices sharing a `DEVICE_ID`, which is precisely what the
  "assign it from one central list" rule exists to prevent.
- **It is logged at WARNING, not INFO.** The outcome is fine, but arriving
  here at all means a delivery was filed as failed when it had succeeded, and
  a run of them is worth noticing:
  ```
  Qualix already has sample T11790155376 (error 12063) — counting it as
  delivered, not rejected. An earlier attempt reached them; this device just
  never saw the response.
  ```

The check lives in `post_analysis_data`, so the immediate post, the retry
worker and the manual re-sync all inherit it.

### Why the timeout is 90s

The scan POST waits `_POST_TIMEOUT_SECONDS` (90), not the 30 it originally
used — 30 sat right on top of the endpoint's real response time, so delivered
scans were routinely being filed as failures.

Nobody is waiting on this call. The post right after a batch runs as a
background task once `/confirm` has already answered the browser, and the
retry worker has no user attached at all; only History's manual Re-sync holds
an HTTP request open, and that shows a spinner. The config `GET` is
deliberately left at 30s — there is no evidence of it being slow, and it *is*
on the login path.

Raising the timeout makes the duplicate case rarer but cannot eliminate it: no
timeout can distinguish "still working" from "never going to answer".

On `400` the reason is parsed out (`_readable_qualix_error` understands
Qualix's `error-code`/`error-message` shape) and stored on `result.sync_error`,
which is what the History list and the record page display. A later successful
sync clears it.

**A rejected or undelivered scan is not written to Google Sheets.** Only a
`'1'` is. This keeps the sheet and Qualix agreeing about which scans exist.

### The exact body is logged on every attempt

```
[SYNC] POST https://.../api/scan/v2/post-visio body: {"scan_data": {...}, "analysis": [...]}
```

Emitted at INFO from `post_analysis_data`, which all three senders go through,
so the immediate post, the retry worker and the manual re-sync are all covered
by the one line. Headers are deliberately **not** logged — the bearer token
lives there; the body is only scan data.

This exists because a `400` almost always comes down to a single wrong field,
and reconstructing what was actually on the wire from the database after the
fact was the missing step when diagnosing one.

---

## 2. `GET` config — what populates every dropdown

`sync_service.fetch_config()` → `sync_service.sync_commodity_config()`

| | |
|---|---|
| **URL** | `{QUALIX_API_URL}portal/api/icompass/v1/config` (legacy)<br>`{ASSURANCE_API_URL}api/icompass/v1/config` (keycloak) |
| **Method** | `GET`, params `response_type=code`, `client_id=client-mobile` |
| **Timeout** | 30s |
| **401** | Token refreshed, retried once |

### Why it is made

The device does not know what it is allowed to scan until Qualix tells it.
This one response supplies every dropdown on the New Batch form and the
commodity/variety list the scan itself is driven by. Without it the device is
effectively unusable, which is why the cache is only ever replaced on a
confirmed-good response.

### What it serves, and where each part lands

| Response key | Stored in | Used for |
|---|---|---|
| `commodityAnalysisModels` | `commodity_details` | Commodity + variety pickers; `variety_id` lookup at datagram build time |
| `surveyorDetails` | `surveyor_details` | Surveyor dropdown |
| `vendorDetails` | `vendor_details` | Vendor name/code |
| `brandList` | `brand_details` | Brand dropdown, de-duplicated by name |
| `clientName`, `imageFolderName` | `client_info` | Display, and the S3/output folder layout |

Two deliberate safety behaviours, both of which exist because of real failures:

- **A response with no commodities is rejected outright** rather than cached.
- **The whole replacement is one transaction.** An earlier version deleted the
  commodity table *before* knowing the fetch had succeeded, so a mid-way failure
  left the device with no commodities at all.

One field-shape trap worth knowing: Qualix returns the surveyor id as
camelCase `surveyorId`. Dev's config had an empty surveyor list, so this only
surfaced against prod. The parser accepts `surveyorId`, `surveyor_id` and `id`.

### How it is triggered

`POST /api/config/sync` runs it **inline** and returns
`{"status": ..., "synced": bool}` — it reports real completion, not mere
acceptance. It previously returned success immediately from a background task,
which meant a caller could not distinguish a finished sync from a failed one.

---

## 3. Keycloak

`services/keycloak_service.py`. Only used when `AUTH_PROVIDER=keycloak`.
Token endpoint is `{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token`
throughout; every request adds `client_secret` when one is configured.

| Call | Endpoint | Grant / method | Why |
|---|---|---|---|
| `is_reachable()` | `GET .../.well-known/openid-configuration` | none, 10s | Decides whether an offline session should be asked to sign in again. Only the status code is read |
| `login()` | `POST` token | **`password`** (ROPC) | Verifies an operator's credentials. Reads `access_token` + `refresh_token` |
| `refresh()` | `POST` token | **`refresh_token`** | The daily session re-check. Keycloak rotates the refresh token; the new one is stored |
| `service_token()` | `POST` token | **`password`**, as `SYNC_SERVICE_*` | The token every sync uses. Cached in memory |
| `_admin_token()` | `POST` token | **`client_credentials`** | The *only* client-credentials grant here |
| `fetch_account_status()` | `GET /admin/realms/{realm}/users/{id}` and `.../credentials` | Admin API, 15s each | Is the account still enabled, and has its password changed |

Notes that matter when debugging:

- **`service_token()` is a password grant, not client-credentials.** This is
  deliberate and documented in `keycloak_integration/4 - assurance_gateway.md`:
  syncing authenticates as a fixed *user* account (`SYNC_SERVICE_*`). It must
  be a real Keycloak account, and it has **no fallback** — blank fails loudly.
  It is deliberately a different account from `EMERGENCY_LOGIN_*`, the tier-3
  device key, which is checked on-device and authenticates nothing outbound.
  The two used to share one setting, and the shared value was a Qualix-only
  login Keycloak had never heard of, so every sync failed.
- **Access-token claims are decoded without signature verification.** They are
  read for display fields (name, email, roles, `sub`) only; the gateway does
  the actual verification on every request.
- **"Rejected" and "unreachable" are different answers and are kept apart.**
  `_token_request` returns a `{"__rejected__": True}` sentinel for a non-200
  and `None` for a network failure. The whole offline story depends on this: a
  device that cannot reach Keycloak must never be treated as a device whose
  account was refused.
- `fetch_account_status` reads the password credential's `createdDate` in
  **epoch milliseconds**. A 401 invalidates the admin token but does **not**
  retry inside the call — the next cycle re-fetches.
- The Admin API path needs realm configuration that this code cannot create or
  check: Service Accounts enabled on `KEYCLOAK_CLIENT_ID`, with
  realm-management `view-users`. If it is missing, Keycloak answers 400/403 and
  this degrades to the same behaviour as "unreachable".

---

## 4. `GET` Qualix user profile — the one call made as the operator

`keycloak_service.fetch_qualix_profile()`

| | |
|---|---|
| **URL** | `{ASSURANCE_API_URL}/api/user/keycloak-profile` |
| **Method** | `GET`, 15s, no retry |
| **Auth** | **the operator's own access token** — the only outbound call that uses it |

### Why it is made

Keycloak is a shared realm. A successful Keycloak login proves the password is
valid *somewhere in the organisation*; it does not prove the person has any
business using this device. This call is what actually confirms they are a
Qualix operator, and it is the reason a valid Keycloak account alone cannot
get someone in.

It is also where `operator_id` comes from: the response's `user.user_id`,
which is then stored and sent on every scan (§1).

Failure handling is deliberately asymmetric:

| Response | Treated as |
|---|---|
| `200` | Confirmed. `user.first_name`, `user.user_id` read |
| `500` with `USERNR01` in the body | **Explicit rejection.** Login refused, cached credentials cleared, `needs_relogin` set |
| Any other status, or a connection error | **Inconclusive.** Login is *not* blocked |

---

## 5. Legacy Qualix OAuth login

`sync_service.login_qualix()`. Only when `AUTH_PROVIDER=legacy`. A two-step
port of `api_handle.handle_login`:

1. `GET {QUALIX_API_URL}portal/oauth/authorize?response_type=code&client_id=client-mobile`
   (15s). The body is ignored — this call exists purely to obtain the
   `JSESSIONID` cookie, which is then sent on every subsequent request.
2. `POST {QUALIX_API_URL}portal/login?bearer=mobile` (30s) as **multipart**
   with fields `Signin`, `bearer`, `username`, `password`.

Reads `access_token` plus `user.user_id` / `first_name` / `customer_name`.
The `User-Agent` is spoofed to `Mozilla/5`, as in legacy.

---

## 6. Google Sheets

`sync_service._open_sheet()` / `already_in_sheet()` / `post_to_sheets()`.
Off unless `SHEETS_ENABLED` and `SHEETS_SPREADSHEET_ID` are both set.

| | |
|---|---|
| **Auth** | Google service-account JSON (`GOOGLE_APPLICATION_CREDENTIALS`), via `gspread` |
| **Scope** | `https://www.googleapis.com/auth/spreadsheets` — nothing wider |
| **Target** | `sheet1` of the spreadsheet id, appended with `append_row` |
| **Timeout** | 30s |

### Why it is made

An operational convenience copy, not a system of record: it gives the site team
a live view without access to Qualix. Nothing reads it back.

### What a row contains

In order: 12 `scan_data` fields (`sample_id`, `commodity_name`,
`variety_name`, `weight`, `brand`, `vendor_name`, `vendor_code`,
`manufacturing_date`, `receiving_date`, `site_code`, `process_start_time`,
`process_end_time`), then 27 analysis totals in a fixed header order
(defaulting to 0 when absent), then any analysis entries not in that list, and
finally `device_id`.

### Two duplicate guards, for two different reasons

- **Only an accepted (`'1'`) scan is written at all.** Rejected and undelivered
  scans are not, so the sheet cannot disagree with Qualix about what exists.
- **`already_in_sheet()`** reads column 11 (`process_start_time`) and skips the
  append if that exact timestamp is already present — a port of legacy's
  `check_start_time_exists`. On any error it returns `False` and lets the write
  proceed, i.e. it prefers a possible duplicate row over a lost one.

> The service-account email needs Editor access on the spreadsheet, or every
> write fails. The credentials file lives under `credentials/`, which is
> gitignored.

---

## 7. S3 upload

Two separate clients, both authenticating the same way.

**Credentials** are obtained from an **AWS Cognito identity pool**, not from
static keys: `cognito-identity.GetId(IdentityPoolId=AWS_IDENTITY_POOL_ID)` then
`GetCredentialsForIdentity(...)`, yielding temporary
`AccessKeyId`/`SecretKey`/`SessionToken`. Nothing long-lived is stored on the
device. Boto config: 15s connect, 60s read, 3 attempts.

| | `s3_worker.py` (sweep) | `s3_service.py` (on demand) |
|---|---|---|
| Trigger | Every `S3_UPLOAD_INTERVAL_SECONDS` (default 60), started in the app lifespan | Called with an explicit key |
| Scans | `{OUTPUT_DIR}/output` and `{OUTPUT_DIR}/output_frame` | n/a |
| Key layout | `{image_folder_name}/{client_name}/{output or output_frame}/{relative path}` — folder and client from the synced `ClientInfo` row, falling back to `S3_BUCKET_FOLDER` / `S3_CLIENT` | Caller's key |

The sweep compares sizes rather than re-uploading blindly: it reads the remote
object's `content_length` and uploads only when the remote is smaller than the
local file (a missing object counts as `-1`), which resumes truncated uploads
and skips completed ones. Transfers are multipart above 8 MB with
`max_concurrency=4`.

On an `ExpiredToken` / `InvalidAccessKeyId` error both clients re-issue Cognito
credentials — the worker aborts the cycle and resumes on the next tick.

> The key prefix depends on config that arrives from the **config GET** (§2).
> A device that has never synced config falls back to the `.env` values, so
> uploads still work but land under a different prefix.

---

## Nothing else leaves the device

A sweep for `requests`, `httpx`, `aiohttp`, `urllib`, `boto3`, `gspread` and
raw URLs across `app/` finds no other outbound caller. Specifically:

- The **camera and conveyor are local** — SDK and serial, no network.
- `/ws/camera/stream` and `/ws/data_collection/stream` are **inbound**
  WebSockets served by this backend, not calls out.
- Inference runs locally against on-device model files.

---

## Two different uuids, doing two different jobs

This trips people up, because both are UUIDs attached to the same scan.

| | `scan_data.uuid` | `result.client_request_id` |
|---|---|---|
| Generated by | the backend, when the datagram is built | the backend, at `/submit`; the browser then holds it |
| Sent to | **Qualix**, in the payload | nowhere — it never leaves this device |
| Stored in | `result.result` (inside the JSON blob) | its own column, `UNIQUE` |
| Protects against | Qualix recording one scan twice across delivery retries | this device's database recording one scan twice across save retries |
| Inherited from legacy? | **Yes** — `main.py:1669` | **No** — new |

### What legacy did and did not do

Legacy **does** generate `scan_data.uuid` (`uuid.uuid1()`, `main.py:1669`),
once per scan in `generate_datagram`, and stores the whole datagram as a string
in its results table. Its retry loop parses that string back and re-posts it
verbatim (`main.py:2891-2928`), so the uuid is stable across every delivery
attempt — the same behaviour this port has.

Legacy has **no** equivalent of `client_request_id`, and did not need one:
it was a single PyQt process, so the Save click called the database directly.
There was no HTTP boundary between the click and the insert, and therefore no
way for a save to succeed while the caller saw a timeout. That failure mode is
a consequence of splitting the app into a backend and a browser frontend, and
it is what `client_request_id` exists to close. Legacy's own `write_results`
is a bare `INSERT` with no uniqueness guard of any kind
(`database.py:266-294`).

One related legacy behaviour was deliberately *not* reproduced: legacy's
`post_api_Thread` calls `sync_data()` (the Sheets write) **before**
`post_analysis_data()` and unconditionally (`main.py:2818-2821`), so a scan
Qualix went on to reject still reached the sheet. That is precisely why legacy
needed `check_start_time_exists`. This port gates the Sheets write on a `'1'`
from Qualix instead, and keeps the timestamp check as a second line of defence.
