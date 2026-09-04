"""Database engine and session factory."""

import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

_url = settings.DATABASE_URL

# SQLite does not accept the pooling arguments a server database needs, and it
# needs check_same_thread=False because background workers touch the session
# factory from other threads. PostgreSQL is the deployment target; SQLite is
# there so the app can be exercised without a running server.
if _url.startswith("sqlite"):
    engine = create_engine(_url, connect_args={"check_same_thread": False})
    logger.warning("Using SQLite at %s — PostgreSQL is the deployment target.", _url)
else:
    engine = create_engine(
        _url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Request-scoped session.

    Note this session is closed as soon as the response is returned. Anything
    running after that — a BackgroundTask, a worker — must open its own
    SessionLocal() rather than capturing this one.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
