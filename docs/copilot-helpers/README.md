# Camera settings and black-frame checks

## Configuration

- `.env` sets `CAMERA_FEATURE_FILE` to
  `/home/nvidia/MVS/Samples/aarch64/Python/ParametrizeCamera_LoadAndSave/FeatureFile.ini`.
- `app/core/config.py` reads this setting. Process environment variables take
  precedence over `.env`.
- `RealCameraService.initialize()` in `app/services/camera_service.py` sets
  fallback exposure/gain, then loads the feature file using `MV_CC_FeatureLoad`.
  The feature file's camera settings take precedence over those fallback values.
- GigE packet size is negotiated AFTER loading the feature file so its saved
  network settings cannot overwrite the packet size supported by this connection.

## Verified on 2026-10-14

The requested feature file and bundled `legacy/FeatureFile_new.ini` were identical.
Both saved `GevSCPSPacketSize=8164`, while the SDK negotiated 1500 on this device.
Loading the file after packet negotiation produced all-zero raw frames, even
with the camera's internal color-bar test pattern enabled.

After moving packet negotiation after feature loading, readback confirmed 1500
and three 1920x1200 color frames had pixel values 17-247 with mean brightness
about 126/255. Exposure readback was 600 microseconds and gain was 0.
Temporary diagnostic settings were restored; the feature file was not edited.

## Runtime check

Close the camera connection in MVS before starting the backend: the SDK opens
the camera exclusively. Restart the backend after editing `.env`:

```sh
sudo systemctl restart eye-compass-backend.service
journalctl --no-pager -u eye-compass-backend.service -n 100
```

Confirm the log shows the requested feature file followed by
`GevSCPSPacketSize set to 1500` (or the optimal size for the current connection).
Refresh the frontend and open its camera view. Use the backend interpreter
`/home/nvidia/.virtualenvs/eye_compass/bin/python` for direct SDK checks, with
the backend stopped so it does not own the camera.

## Change-comment template

```text
Author: <name>
Ticket: <JIRA-ID, when applicable>
<JIRA-ID> #time <duration> #comment Load MVS settings before GigE packet negotiation.
Security (OWASP): Keep credentials out of diagnostic logs; restrict write access
to the backend environment and camera feature files.
```