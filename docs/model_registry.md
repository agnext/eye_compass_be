# Model registry — why "inference model not loaded" happens

`MODEL_REGISTRY` (`app/services/inference_service.py`) maps a model key to a
filename inside `MODEL_DIR`. `resolve_model` maps a commodity/variety to a key.
If the resulting file is not on disk, `load_model` returns False and the
Dashboard shows "inference model not loaded" — for that commodity only.

Both tables are ports of legacy `GrabImage.py`: `model_paths` (lines 221-247)
and `get_model_infer` (lines 384-443). **Legacy is the authority.** When they
disagree, legacy is right, because that is what the machine ran before.

## 2026-10-16: realigned with legacy

A redeploy of `eye_compass_new` reintroduced filenames that do not exist in
`MODEL_DIR` (now `legacy_models_copy`). 9 of 19 entries were unresolvable, so
those commodities could not start a scan. The registry was realigned
key-by-key with legacy `model_paths`; all 22 keys now match it exactly.

Also corrected in `resolve_model`, where the port pointed a commodity at a
different model than legacy does:

| Commodity | Was | Legacy, now |
|---|---|---|
| `toor_dal` | `toor` | `chanadal` |
| `urad_white` | `chanadal` | `urad_dal` |
| `moong_whole` | `moong_whole` | `green_moong_dal` |
| `masoor_red_whole` | `masoor_dal` | `masoor_red_whole` |

Three keys legacy has and the port lacked were added: `sona_masoori_rice`,
`green_moong_dal`, `masoor_red_whole`, along with the `green_moong_dal` and
`sonamasoori_rice` commodities.

Note `chanadal` genuinely points at `Toor_v6.optimized` in legacy. That is not
a mistake in the port.

## Four keys still unresolvable, deliberately

`stem_rice`, `toor`, `masoor_dal` and `chitra_rajma` name files that are absent
from `MODEL_DIR`. They are absent in legacy's own model directory too, so
legacy fails for those commodities in exactly the same way. They are left
pointing at the legacy filename rather than repointed at a nearby file,
because substituting a different model would change detection results.

`stem_rice` is also the fallback for any unmapped commodity, so an unmapped
commodity fails to load a model rather than silently inferring with a rice
model.

`masoor_dal` is unreferenced by `resolve_model`, as in legacy.

## Startup audit

`audit_model_registry()` runs from `app/main.py`'s lifespan and logs every
unresolvable entry at startup. It changes no behaviour; it exists so a
filename drift is visible in `journalctl` at boot instead of surfacing when an
operator selects that commodity mid-shift.

## Still divergent from legacy: confidence thresholds

Not changed, because it alters detection sensitivity on a live machine.
Reported for a decision:

| Commodity / variety | Port | Legacy |
|---|---|---|
| `chana_dal` | 0.20 | 0.10 |
| `moong_whole` | 0.20 | 0.10 |
| `black_chana` | 0.32 | 0.10 |
| rice / `dawat` | 0.20 | 0.10 |
| rice / `tibar` | 0.30 | 0.10 |
| rice / `GR1-RI01-ST2`, `GR1-RI01-RW1` | 0.30 | 0.20 |

A higher threshold means fewer detections, so the port is currently less
sensitive than legacy for these six.

## Checking it

    cd eye_compass_be
    USE_MOCK_CAMERA=true python -c "from app.services.inference_service import audit_model_registry; audit_model_registry()"

`USE_MOCK_CAMERA=true` is required for anything that imports the conveyor, so
the real belt is never driven from a test.
