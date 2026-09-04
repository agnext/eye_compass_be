"""
SQLAlchemy models mapping the legacy SQLite schema (eye_compass/database.py:23-69)
onto PostgreSQL.

Table and column names are kept identical to the legacy ones so that
scripts/migrate_sqlite_to_postgres.py can move data across without renaming.

Three columns that the legacy code stored as stringified Python literals
(`str(list)` / `str(dict)`, read back with ast.literal_eval) are JSONB here:
    com_details.analysis, com_details.variety, result.result
"""

from sqlalchemy import Column, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from app.core.database import Base

# JSONB on PostgreSQL, plain JSON everywhere else (so a SQLite fallback still works).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Creds(Base):
    """Cached operator credentials for offline login (legacy `creds`)."""

    __tablename__ = "creds"

    id = Column(Integer, primary_key=True)
    user = Column(String(150), index=True)
    password = Column(String(255))


class ClientInfo(Base):
    """Client name + image folder, fetched from Qualix at login (legacy `clientinfo`).

    image_folder_name drives the S3 key prefix — see services/s3_worker.py.
    """

    __tablename__ = "clientinfo"

    id = Column(Integer, primary_key=True)
    client_name = Column(String(100))
    image_folder_name = Column(String(150))


class SurveyorDetails(Base):
    """Populates the Sorter Name dropdown (legacy `surveyordetails`)."""

    __tablename__ = "surveyordetails"

    id = Column(Integer, primary_key=True)
    surveyor_id = Column(String(50))
    name = Column(String(150))


class BrandDetails(Base):
    """Populates the Brand dropdown (legacy `branddetails`)."""

    __tablename__ = "branddetails"

    id = Column(Integer, primary_key=True)
    brand_name = Column(String(150))

    __table_args__ = (UniqueConstraint("brand_name", name="uq_branddetails_brand_name"),)


class VendorDetails(Base):
    """Populates the Vendor Name dropdown and the vendor_code auto-fill
    (legacy `vendordetails`, consumed at main.py:1177-1184)."""

    __tablename__ = "vendordetails"

    id = Column(Integer, primary_key=True)
    vendor_name = Column(String(150))
    vendor_code = Column(String(50))


class CommodityDetails(Base):
    """Commodity / variety / foreign-matter config from Qualix (legacy `com_details`).

    `analysis` is the list of FM analysis names for the commodity.
    `variety`  is the list of variety objects ({variety_code, variety_name, ...}).
    Legacy stored both as str(list); they are JSON here.
    """

    __tablename__ = "com_details"

    id = Column(Integer, primary_key=True)
    commodity = Column(String(150), index=True)
    commodity_id = Column(String(50))
    analysis = Column(JSONType, default=list)
    variety = Column(JSONType, default=list)


class Result(Base):
    """A completed scan (legacy `result`).

    `result` holds the full Qualix datagram ({"scan_data": {...}, "analysis": [...]}).
    Legacy stored str(dict) capped at varchar(400) — unenforced by SQLite, and the
    longest live payload is 1587 chars — so this is JSONB with no length cap.

    sync_status keeps the legacy three-valued contract:
        '1' = Qualix accepted (HTTP 200)
        '2' = Qualix rejected the payload (HTTP 400) — terminal, do not retry
        '0' = not yet delivered — the retry worker picks these up
    """

    __tablename__ = "result"

    id = Column(Integer, primary_key=True)
    sample_id = Column(String(150), index=True)
    commodity = Column(String(150))
    variety = Column(String(150))
    result = Column(JSONType)
    date = Column(String(50), index=True)
    start_time = Column(String(50))
    stop_time = Column(String(50))
    sync_status = Column(String(20), index=True, default="0")

    __table_args__ = (
        # Legacy identified a record by this 4-tuple (database.py:433-445).
        Index("ix_result_identity", "sample_id", "date", "start_time", "stop_time"),
    )


class BatchDetails(Base):
    """The 12-field batch form (legacy main.py:1663-1710 read these straight off the UI).

    Legacy never persisted them separately — they were embedded in the result
    datagram. We persist them so the scan submission can reference a batch by id
    and rebuild the full datagram server-side.
    """

    __tablename__ = "batch_details"

    id = Column(Integer, primary_key=True)
    batch_number = Column(String(150), index=True)
    po_number = Column(String(150))
    manufacturing_date = Column(String(50))
    vendor_name = Column(String(150))
    receiving_date = Column(String(50))
    sorting_quantity = Column(String(50))
    product_name = Column(String(150))
    brand = Column(String(150))
    vendor_code = Column(String(50))
    site_code = Column(String(50))
    product_code = Column(String(150))
    sorter_name = Column(String(150))
    created_at = Column(String(50))
