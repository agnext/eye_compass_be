"""
Header-driven row builder for the Google Sheet.

The sheet's own header row (row 1) is the source of truth. Every value is
placed in the column whose header matches it, never by position, so a new
column, a reordered column, or an analysis name this code has never seen
cannot shift anything else along the row.

This replaced a positional builder that appended "unknown" analysis names as
extra unlabelled cells in the middle of the row, which pushed `device_id`
rightwards by however many there were. Rows came out 40-45 cells wide against
a 41-column header, and `Device_id` held an FM count. See
docs/.../sheet_insertion (and the 13 Oct 2026 investigation) for the trail.

Pure functions, no network and no gspread: the caller passes the header list
as read from the sheet plus the stored datagram, and gets back the row and a
report of what went where. Writing it is the caller's job — see
sync_service.post_to_sheets, which writes at last-used-row + 1 rather than
using append_row (whose table detection landed rows in the middle of the
sheet, overwriting unrelated data).

Matching, in order:
  1. exact, once normalised (case, underscores and runs of spaces ignored)
  2. plural-insensitive, so "Glass Piece" finds "Glass pieces"
  3. ALIASES, for names that mean the same thing but are spelt differently
  4. otherwise the value is SKIPPED and reported — never silently dropped
"""

import re
from typing import Dict, List, Optional, Tuple

# scan_data key -> the sheet header it belongs under.
METADATA_COLUMNS: Dict[str, str] = {
    "sample_id": "sample_id",
    "commodity_name": "Commodity",
    "variety_name": "Variety",
    "weight": "weight",
    "brand": "brand",
    "vendor_name": "vendor_name",
    "vendor_code": "vendor_code",
    "manufacturing_date": "manufacturing_date",
    "receiving_date": "receiving_date",
    "site_code": "site_code",
    "process_start_time": "start_time",
    "process_end_time": "end_time",
    "device_id": "Device_id",
}

# The sheet's Epoch_Timestamp column holds epoch SECONDS (10 digits) — the
# same number the legacy sample_ids carry as a suffix. The datagram's
# inspection_date is epoch MILLISECONDS, so it is divided down to match the
# rows already in the sheet rather than writing a 13-digit number into a
# column of 10-digit ones.
EPOCH_HEADER = "Epoch_Timestamp"
EPOCH_SOURCE = "inspection_date"

# Columns that are not analysis counts. Every OTHER header is treated as an
# analysis column and written as 0 when the scan has no value for it, so rows
# match the shape of the ones already in the sheet.
NON_ANALYSIS_HEADERS = set(METADATA_COLUMNS.values()) | {EPOCH_HEADER}

# Normalised source name -> the header it should be counted under, for pairs
# the generic rules above cannot see. Only genuine synonyms belong here: a
# name that has its own column is matched exactly in step 1 and never reaches
# this table.
ALIASES: Dict[str, str] = {
    "insects or pest": "Insects",
    "metal": "Metal Fragments",
}


def normalise(name: str) -> str:
    """Lower case; '_', '-' and runs of spaces collapse to one space; trimmed.

    The sheet's own header is 'Commodity ' with a trailing space, so trimming
    is not cosmetic here.
    """
    return re.sub(r"[\s_\-]+", " ", str(name or "")).strip().lower()


def match_key(name: str) -> str:
    """Normalised and plural-insensitive, so 'Glass Piece' == 'Glass pieces'."""
    s = normalise(name)
    return s[:-1] if len(s) > 3 and s.endswith("s") else s


def header_index(header: List[str]) -> Tuple[Dict[str, int], List[str]]:
    """Map each header's match key to its column index.

    First occurrence wins; any later duplicate is returned so the caller can
    warn rather than silently writing to only one of two identical columns.
    """
    index: Dict[str, int] = {}
    duplicates: List[str] = []
    for i, name in enumerate(header):
        key = match_key(name)
        if not key:
            continue
        if key in index:
            duplicates.append(str(name))
            continue
        index[key] = i
    return index, duplicates


def build_row(header: List[str], scan_data: dict, analysis: List[dict],
              aliases: Optional[Dict[str, str]] = None):
    """Return (row, report); `row` is exactly len(header) cells wide.

    report = {
      "placed":  [(source_name, value, header_name, column_index)],
      "skipped": [(source_name, value)],           # no column matched
      "summed":  [(header_name, [source_names])],  # several names, one column
      "duplicate_headers": [...],
      "blank": [header_name, ...],                 # left empty
    }
    """
    alias_map = {**ALIASES, **(aliases or {})}
    index, duplicates = header_index(header)
    scan_data = scan_data or {}

    row: List = [""] * len(header)
    report = {"placed": [], "skipped": [], "summed": [],
              "duplicate_headers": duplicates, "blank": []}

    non_analysis_keys = {match_key(h) for h in NON_ANALYSIS_HEADERS}
    for i, name in enumerate(header):
        if name and match_key(name) not in non_analysis_keys:
            row[i] = 0

    for source, header_name in METADATA_COLUMNS.items():
        value = scan_data.get(source, "")
        column = index.get(match_key(header_name))
        if column is None:
            report["skipped"].append((source, value))
            continue
        row[column] = value
        report["placed"].append((source, value, header[column], column))

    epoch_column = index.get(match_key(EPOCH_HEADER))
    raw_epoch = scan_data.get(EPOCH_SOURCE)
    if epoch_column is not None and isinstance(raw_epoch, (int, float)):
        seconds = int(raw_epoch // 1000) if raw_epoch > 1e11 else int(raw_epoch)
        row[epoch_column] = seconds
        report["placed"].append(
            (EPOCH_SOURCE, seconds, header[epoch_column], epoch_column))

    landed: Dict[int, List[str]] = {}
    for item in analysis or []:
        name = item.get("analysisName", "")
        amount = item.get("totalAmount", 0)
        column = index.get(match_key(name))
        if column is None:
            target = alias_map.get(normalise(name))
            if target:
                column = index.get(match_key(target))
        if column is None:
            report["skipped"].append((name, amount))
            continue
        landed.setdefault(column, []).append(name)
        # Summed, not overwritten: two source names mapping to one column
        # (e.g. an alias colliding with a real one) must not lose a count.
        previous = row[column] if isinstance(row[column], (int, float)) else 0
        row[column] = previous + (amount if isinstance(amount, (int, float)) else 0)
        report["placed"].append((name, amount, header[column], column))

    for column, names in landed.items():
        if len(names) > 1:
            report["summed"].append((header[column], names))

    report["blank"] = [header[i] for i, value in enumerate(row) if value == ""]
    return row, report
