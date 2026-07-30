import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.migrations import run_migrations
from app.db.models import Base

is_sqlite = settings.database_url.startswith("sqlite")
if is_sqlite:
    os.makedirs("data", exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if is_sqlite else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    # create_all cannot alter existing tables; see app/db/migrations.py
    run_migrations(engine)


def get_session() -> Session:
    return SessionLocal()
