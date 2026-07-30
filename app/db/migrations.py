"""Hand-rolled, idempotent schema migrations.

There is no Alembic in this project and `Base.metadata.create_all` only creates
*missing tables* — it will never add a column to a table that already exists.
Production (Railway Postgres) already holds trade history, so new columns have to
be added by hand here. Every step is guarded so running this on an already-migrated
database, or on a brand-new one create_all just built, is a no-op.

Called from `init_db()` on every boot.
"""

import logging

from sqlalchemy import Engine, inspect, text, update

from app.db.models import Trade

log = logging.getLogger("migrations")

# ALTER TABLE ... ADD COLUMN with these types is valid on both SQLite and Postgres.
_TRADE_COLUMNS = {
    "account": "VARCHAR",
    "lots": "FLOAT",
    "margin_used": "FLOAT",
    "risk_pct": "FLOAT",
}


def _existing_columns(engine: Engine, table: str) -> set[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def _add_missing_columns(engine: Engine) -> list[str]:
    present = _existing_columns(engine, "trades")
    if not present:
        return []  # table doesn't exist yet; create_all will build it complete

    added = []
    for column, sql_type in _TRADE_COLUMNS.items():
        if column in present:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE trades ADD COLUMN {column} {sql_type}"))
        added.append(column)
        log.info("migration: added trades.%s", column)
    return added


def _backfill_account(engine: Engine) -> int:
    """Derive `account` for rows written before the column existed.

    Legacy identity was the (is_shadow, strategy) pair: shadow rows belong to
    their strategy's own account, non-shadow rows to "ai_selected".
    """
    with engine.begin() as conn:
        shadow = conn.execute(
            update(Trade)
            .where(Trade.account.is_(None), Trade.is_shadow.is_(True))
            .values(account=Trade.strategy)
        )
        live = conn.execute(
            update(Trade)
            .where(Trade.account.is_(None), Trade.is_shadow.is_(False))
            .values(account="ai_selected")
        )
    total = (shadow.rowcount or 0) + (live.rowcount or 0)
    if total:
        log.info("migration: backfilled trades.account for %d rows", total)
    return total


def _ensure_account_index(engine: Engine) -> None:
    if "trades" not in inspect(engine).get_table_names():
        return
    # Name matches what create_all generates for index=True, so a fresh DB and a
    # migrated one converge on the same schema.
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_trades_account ON trades (account)"))


def run_migrations(engine: Engine) -> None:
    added = _add_missing_columns(engine)
    # Backfill whenever NULLs could exist: right after adding the column, and also
    # on a DB migrated by an earlier partial run.
    if "account" in added or _existing_columns(engine, "trades"):
        _backfill_account(engine)
    _ensure_account_index(engine)
