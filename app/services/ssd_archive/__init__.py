"""Archive SSD: a copy of the device's raw capture folders on an external drive.

Copies Data_Collection/ and output_frame/ to this device's archive SSD once a
day, organised by date, and frees device space by removing samples older than
SSD_ARCHIVE_DEVICE_RETENTION_DAYS, but only after every file of the sample
has been re-verified byte for byte against its SSD copy. The SSD itself is
never cleaned. Runs inside the backend (worker.SsdArchiveTask) and records
each Data Collection run's SSD copy on its history row, so Data Collection
History can show the run from the SSD once it has left the device.

On the SSD:

    <SSD>/<YYYY-MM-DD>/<Data_Collection|output_frame>/<commodity>/<variety>/<sample>/

Setting up a drive (once, with the backend's Python):

    python -m app.services.ssd_archive init /media/nvidia/<drive>
    python -m app.services.ssd_archive status
    python -m app.services.ssd_archive dry-run
"""
