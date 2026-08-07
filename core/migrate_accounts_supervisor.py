"""
CRAVE — Accounts table migration for Process Supervisor (Phase 1)
=====================================================================
Adds the columns the supervisor needs to track one OS process per MT5
account: which broker it is, what profile/port it runs on, its PID, and
its last known health.

SQLite has no "ALTER COLUMN IF NOT EXISTS" — this checks pragma table_info
first so it's safe to run multiple times (idempotent), including on a
fresh DB that already has these columns from a future schema version.

Run once: python core/migrate_accounts_supervisor.py
"""

import logging

logger = logging.getLogger("crave.migration")

NEW_COLUMNS = [
    # (name, sql_type, default_clause)
    ("broker",            "TEXT",    "DEFAULT 'mt5'"),
    ("profile",           "TEXT",    ""),
    ("pid",               "INTEGER", ""),
    ("port",              "INTEGER", ""),
    ("process_status",    "TEXT",    "DEFAULT 'stopped'"),   # stopped|starting|running|crashed|stopping
    ("last_health_check", "TEXT",    ""),
    ("restart_count",     "INTEGER", "DEFAULT 0"),
    ("last_error",        "TEXT",    ""),
]


def migrate(db):
    """db: a DatabaseManager instance (core.database_manager.get_db())"""
    existing = {row["name"] for row in db.query("PRAGMA table_info(accounts)")}

    for name, sql_type, default in NEW_COLUMNS:
        if name in existing:
            continue
        ddl = f"ALTER TABLE accounts ADD COLUMN {name} {sql_type} {default}".strip()
        try:
            db.execute(ddl)
            logger.info(f"[Migration] Added accounts.{name}")
        except Exception as e:
            logger.error(f"[Migration] Failed adding accounts.{name}: {e}")

    # Backfill: any pre-existing row saved by the OLD /api/accounts/add
    # (before account_endpoints_patch.py) has account_type in {"prop_firm",
    # "real", "demo", "binance"} but no broker column — infer broker from
    # server string as a best-effort default so the supervisor doesn't
    # choke on rows created before this migration existed.
    rows = db.query("SELECT id, server, broker FROM accounts WHERE broker IS NULL OR broker = ''")
    for row in rows:
        server = (row.get("server") or "").lower()
        if "binance" in server:
            inferred = "binance"
        elif "zerodha" in server or "kite" in server:
            inferred = "zerodha"
        else:
            inferred = "mt5"
        db.execute("UPDATE accounts SET broker=? WHERE id=?", (inferred, row["id"]))
        logger.info(f"[Migration] Backfilled account {row['id']} broker={inferred}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from core.database_manager import get_db
    migrate(get_db())
    print("Migration complete.")
