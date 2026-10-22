"""Archive SSD: a copy of the device's session folders on an external drive.

Copies Data_Collection/, output_frame/ and (SSD_ARCHIVE_INCLUDE_CROPS)
output/ to this device's archive SSD once a day, organised by date, and frees
device space by removing each sample (once SSD_ARCHIVE_DEVICE_RETENTION_DAYS
have passed, 0 = at once, and not in use), but only after every file of it
has been re-verified byte for byte against its SSD copy. Samples older than
SSD_ARCHIVE_SSD_RETENTION_DAYS are deleted from the SSD. Runs inside the backend
(worker.SsdArchiveTask), records every copy in folder_copies, and also runs
the low-disk safety net. See docs/ssd_archive.md.

On the SSD:

    <SSD>/<YYYY-MM-DD>/<Data_Collection|output_frame|output>/<commodity>/<variety>/<sample>/

Setting up a drive (once, with the backend's Python):

    python -m app.services.ssd_archive init /media/nvidia/<drive>
    python -m app.services.ssd_archive status
    python -m app.services.ssd_archive dry-run
"""
