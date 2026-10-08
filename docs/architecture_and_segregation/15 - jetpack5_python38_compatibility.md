# 15 — JetPack 5 / Python 3.8 Compatibility

Production devices run **JetPack 5** (L4T R35.2.1) with **Python 3.8.10**.
The dev unit runs **JetPack 6** with **Python 3.10.12**.

The codebase is developed and tested on Python 3.10, but all six gaps listed
below were addressed so that **the same branch runs on both platforms**
without a separate JetPack-5 fork. Every fix is either a no-op on 3.10 or
lives outside the repo (pip packages, device-level config).

---

## 1. `str | None` union syntax (Python 3.10+)

**Symptom:** `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`

Pydantic models and FastAPI endpoints use the `X | None` union shorthand
introduced in Python 3.10. On 3.8 the `|` operator is not defined on `type`
objects.

**Fix — pip package, no code change:**

```bash
pip install eval_type_backport
```

Pydantic detects this package automatically and uses it to evaluate the union
syntax at runtime on older Pythons.

> **This covers pydantic model fields only.** A `X | None` annotation on a
> plain function or module-level variable is evaluated eagerly by Python at
> import time, and `eval_type_backport` never sees it. Those need fix #6.

---

## 2. `asyncio.to_thread` (Python 3.9+)

**Symptom:** `AttributeError: module 'asyncio' has no attribute 'to_thread'`

`asyncio.to_thread` was added in Python 3.9. The backend uses it in the S3
worker, resource monitor, and sync worker.

**Fix — polyfill in `app/main.py`, guarded so it is a no-op on 3.10+:**

```python
if not hasattr(asyncio, "to_thread"):  # Python 3.8 polyfill
    import contextvars
    import functools

    async def _to_thread(func, /, *args, **kwargs):
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(
            None, functools.partial(ctx.run, func, *args, **kwargs)
        )

    asyncio.to_thread = _to_thread
```

This is already committed in the canonical repo — no per-device action needed.

---

## 3. ML stack not visible across venvs

**Symptom:** `ModuleNotFoundError: No module named 'onnxruntime'`

On prod, the ML packages (onnxruntime, torch, torchvision) live in a separate
venv (`~/.virtualenvs/m38/`). The `eye_compass` venv that runs the backend
cannot see them.

**Fix — `.pth` file on each prod device (not in the repo):**

```bash
echo '/home/nvidia/.virtualenvs/m38/lib/python3.8/site-packages' \
  > /home/nvidia/.virtualenvs/eye_compass/lib/python3.8/site-packages/zz_m38_ml_stack.pth
```

Python reads `.pth` files at startup and adds each line to `sys.path`. The
`zz_` prefix ensures it loads last, so the eye_compass venv's own packages
take priority.

This is a device-level config step — it is not in the repo because the venv
paths are local. The `m38` venv is not modified (no risk of breaking the
legacy app that also uses it).

---

## 4. Missing `tqdm`

**Symptom:** `ModuleNotFoundError: No module named 'tqdm'`

`run_inference.py` imports tqdm for progress bars. It is present on the dev
unit but not in a fresh prod venv.

**Fix:**

```bash
pip install tqdm
```

---

## 5. `@` in DATABASE_URL password

**Symptom:** `could not translate host name "123@localhost" to address`

If the PostgreSQL password contains `@` (e.g. `nvidia@123`), the `@` breaks
the `postgresql://user:password@host/db` URL parsing — Python reads everything
after the first `@` as the host.

**Fix — in `.env` on the affected device:**

```
# Before (broken):
DATABASE_URL=postgresql://postgres:nvidia@123@localhost:5432/eye_compass

# After (working):
DATABASE_URL=postgresql://postgres:nvidia%40123@localhost:5432/eye_compass
```

`%40` is the URL-encoded form of `@`. This only affects devices whose
database password contains special characters.

---

## 6. `X | None` outside a pydantic model

**Symptom:** `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`
at **import time**, crash-looping the service before it ever serves a request:

```
File "app/services/camera_service.py", line 40, in <module>
    def grab_frame(self) -> np.ndarray | None:
TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
```

This looks like fix #1 but is a different problem, and `eval_type_backport`
does **not** solve it. That package only teaches *pydantic* to resolve unions
in model fields. A function return annotation, a function parameter, or a
module-level variable annotation is evaluated by Python itself the moment the
module is imported — pydantic is never involved.

**Fix — `from __future__ import annotations`, after the module docstring:**

```python
"""Module docstring stays first."""

from __future__ import annotations   # <- PEP 563: annotations become lazy strings

import logging
...
```

PEP 563 stores every annotation as a string instead of evaluating it, so the
`|` is never executed at import. `eval_type_backport` then resolves those
strings at runtime in the places FastAPI and pydantic genuinely need the real
type — the two fixes are complements, not alternatives.

Applied to the six modules that annotate this way outside a pydantic model:

| File | Occurrences |
|---|---|
| `app/api/camera.py` | 3 module-level variables |
| `app/api/history.py` | 1 route parameter |
| `app/api/scan.py` | 1 module-level variable |
| `app/services/camera_service.py` | 3 method return types |
| `app/services/conveyor_service.py` | 1 instance attribute |
| `app/services/resource_monitor.py` | 1 function return type |

Two further hits are **false positives** — `app/services/keycloak_service.py`
and `app/services/Tracker/bytetrack/tracker/matching.py` mention `|` only
inside docstrings, which Python never evaluates. Leave them alone.

**Placement matters.** The declaration must come *after* the module docstring.
Put it on line 1 and the docstring stops being the docstring (`__doc__`
becomes `None`). A future statement may be preceded only by the docstring,
comments, and blank lines — anything else is a `SyntaxError`.

To find every affected file in the repo:

```bash
grep -rnE "(->|:) *[A-Za-z_][A-Za-z0-9_.]*(\[[^]]*\])? *\|" app/
```

Then discard any hit that falls inside a docstring.

---

## Why not upgrade Python on JetPack 5?

JetPack 5 ships Python 3.8 as the system Python. NVIDIA's CUDA, TensorRT,
and cuDNN packages are built against it. Upgrading Python would break the
entire GPU stack, and JetPack 5 will not receive a newer Python from NVIDIA.

The five fixes above are simpler and lower-risk than replacing the system
Python.

---

## One branch, both platforms

| Fix | Where it lives | Effect on Python 3.10 |
|---|---|---|
| `eval_type_backport` | pip package in prod venv | Harmless — pydantic ignores it |
| `asyncio.to_thread` polyfill | `app/main.py` (repo) | No-op (`hasattr` guard) |
| `.pth` file | device filesystem | N/A — dev doesn't use it |
| `tqdm` | pip package | Already present on dev |
| `%40` encoding | device `.env` | N/A — per-device password |
| `from __future__ import annotations` | 6 modules (repo) | Valid and supported since 3.7 |

No `if sys.version` branches, no feature flags, no separate branch. The repo
is identical on both platforms.

---

## Deployment checklist (prod devices)

When deploying to a new JetPack 5 device, run these in the `eye_compass`
venv:

```bash
# Activate the venv
source ~/.virtualenvs/eye_compass/bin/activate

# Fix #1 — union syntax backport
pip install eval_type_backport

# Fix #4 — tqdm
pip install tqdm

# Fix #3 — ML stack visibility
echo '/home/nvidia/.virtualenvs/m38/lib/python3.8/site-packages' \
  > ~/.virtualenvs/eye_compass/lib/python3.8/site-packages/zz_m38_ml_stack.pth

# Fix #5 — check .env for special chars in DATABASE_URL
# URL-encode any @ as %40, # as %23, etc.
```

Fixes #2 and #6 are code-level and already in the repo — nothing to do on
the device.
