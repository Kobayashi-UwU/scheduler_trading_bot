"""One-time data migration: copy all rows from the existing SQLite database into
the new Postgres database.

Usage (run on Railway, where the SQLite volume is still mounted):

    railway run python scripts/migrate_sqlite_to_postgres.py

It reads the source SQLite path from SQLITE_URL (defaults to the project's
usual path) and the destination from DATABASE_URL (should already point at
the Postgres plugin by the time you run this). Safe to re-run: it skips
tables that already have rows in the destination.
"""

import os
import sys

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.models import Analysis, Base, EquityPoint, Trade  # noqa: E402

SQLITE_URL = os.environ.get("SQLITE_URL", "sqlite:///./data/trading_bot.db")
PG_URL = os.environ["DATABASE_URL"]

if not PG_URL.startswith("postgresql"):
    raise SystemExit(f"DATABASE_URL does not look like Postgres: {PG_URL!r}")

MODELS = [Analysis, Trade, EquityPoint]


def main() -> None:
    src_engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
    dst_engine = create_engine(PG_URL)

    if not inspect(src_engine).has_table("trades"):
        raise SystemExit(f"no tables found at {SQLITE_URL} - nothing to migrate")

    Base.metadata.create_all(bind=dst_engine)

    SrcSession = sessionmaker(bind=src_engine)
    DstSession = sessionmaker(bind=dst_engine)
    src = SrcSession()
    dst = DstSession()

    try:
        for model in MODELS:
            existing = dst.query(model).count()
            if existing:
                print(f"{model.__tablename__}: destination already has {existing} rows, skipping")
                continue

            rows = src.query(model).all()
            print(f"{model.__tablename__}: copying {len(rows)} rows")
            for row in rows:
                data = {c.key: getattr(row, c.key) for c in inspect(model).mapper.column_attrs}
                dst.add(model(**data))
            dst.commit()
        print("done")
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    main()
