"""
CRAVE v10.0 — Database Manager
================================
Abstraction layer over SQLite (now) → PostgreSQL (later).
The rest of the codebase NEVER imports sqlite3 directly.
Everything goes through this module.

To migrate to PostgreSQL later:
  python Setup/migrate_to_postgres.py
  Change DB_BACKEND = "postgresql" in .env
  Zero other code changes needed.

TABLES:
  trades        — every closed trade with full metadata
  signals       — every signal generated (traded and skipped)
  positions     — currently open positions (mirror of positions.json)
  daily_bias    — bias decisions per day per instrument
  streak_state  — circuit breaker state history
  ohlcv         — cached OHLCV candles (saves API calls)
  ml_features   — feature snapshots at each signal (for future ML training)
  paper_trades  — paper trading specific log
"""

import sqlite3
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("crave.database")


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- ── Closed trades ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT    UNIQUE,         -- UUID assigned at open
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,       -- buy / sell
    entry_price     REAL    NOT NULL,
    exit_price      REAL,
    stop_loss       REAL    NOT NULL,
    tp1_price       REAL,
    tp2_price       REAL,
    lot_size        REAL    NOT NULL,
    r_multiple      REAL,                   -- final R result
    outcome         TEXT,                   -- tp2_via_tp1 / sl / tp1_then_be etc
    grade           TEXT,                   -- A+ / A / B+ / B
    confidence      INTEGER,
    exchange        TEXT,
    is_paper        INTEGER DEFAULT 1,      -- 1=paper, 0=live
    open_time       TEXT    NOT NULL,
    close_time      TEXT,
    hold_duration_h REAL,                   -- hours held
    pnl_pct         REAL,                   -- % of equity
    node            TEXT,                   -- which node executed
    notes           TEXT,                   -- JSON blob for extra data
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- ── All signals (traded + skipped) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       TEXT    UNIQUE,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    confidence      INTEGER,
    grade           TEXT,
    was_traded      INTEGER DEFAULT 0,      -- 1=trade fired, 0=skipped
    skip_reason     TEXT,                   -- why it was skipped
    entry_price     REAL,
    atr             REAL,
    session         TEXT,                   -- london / ny / asian
    macro_trend     TEXT,
    fvg_hit         INTEGER DEFAULT 0,
    ob_hit          INTEGER DEFAULT 0,
    sweep_detected  INTEGER DEFAULT 0,
    rsi_divergence  TEXT,
    volume_signal   TEXT,
    structure_event TEXT,
    signal_time     TEXT    NOT NULL,
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- ── Open positions (mirror of positions.json) ───────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT    UNIQUE,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    current_sl      REAL    NOT NULL,
    tp1_price       REAL,
    current_tp      REAL,                   -- dynamic TP (can be extended)
    original_tp2    REAL,                   -- original TP2 for reference
    lot_size        REAL    NOT NULL,
    remaining_pct   REAL    DEFAULT 100,    -- % of position still open
    tp1_hit         INTEGER DEFAULT 0,
    grade           TEXT,
    exchange        TEXT,
    is_paper        INTEGER DEFAULT 1,
    open_time       TEXT    NOT NULL,
    atr_at_open     REAL,
    sl_multiplier   REAL,
    node            TEXT,
    last_updated    TEXT    DEFAULT (datetime('now'))
);

-- ── Daily bias per instrument ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_bias (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    bias            TEXT    NOT NULL,       -- BUY / SELL / NO_TRADE
    strength        INTEGER,                -- 1-3
    reason          TEXT,
    daily_inv_level REAL,                   -- price that kills the bias
    key_levels      TEXT,                   -- JSON list
    created_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(date, symbol)
);

-- ── OHLCV cache ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ohlcv (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,       -- 1h / 4h / 1d
    time            TEXT    NOT NULL,
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          REAL    DEFAULT 0,
    UNIQUE(symbol, timeframe, time)
);

-- ── ML feature snapshots ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ml_features (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       TEXT,
    symbol          TEXT    NOT NULL,
    signal_time     TEXT    NOT NULL,
    features        TEXT    NOT NULL,       -- JSON blob of all features
    outcome         TEXT,                   -- filled in when trade closes
    r_multiple      REAL,                   -- filled in when trade closes
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- ── Streak/circuit breaker history ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS streak_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,
    day_pnl_pct     REAL,
    consecutive_loss_days INTEGER,
    circuit_breaker_fired INTEGER DEFAULT 0,
    risk_level      TEXT,                   -- streak state key
    trades_today    INTEGER DEFAULT 0,
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- ── Indices for common queries ───────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_open_time ON trades(open_time);
CREATE INDEX IF NOT EXISTS idx_signals_symbol   ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_time     ON signals(signal_time);
CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup     ON ohlcv(symbol, timeframe, time);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
"""


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class DatabaseManager:
    """
    Thread-safe SQLite database manager.
    Uses connection-per-thread to avoid SQLite threading issues.
    All methods are safe to call from multiple threads simultaneously.
    """

    def __init__(self, db_path: Optional[str] = None):
        from config.config import DB_PATH
        self.db_path  = db_path or str(DB_PATH)
        self._local   = threading.local()   # thread-local connections
        self._init_db()
        logger.info(f"[DB] Initialised at {self.db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row   # dict-like rows
            self._local.conn.execute("PRAGMA journal_mode=WAL")   # better concurrency
            self._local.conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")  # balanced safety/speed
        return self._local.conn

    def _init_db(self):
        """Create all tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        logger.info("[DB] Schema verified.")

    # ─────────────────────────────────────────────────────────────────────────
    # GENERIC HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write statement."""
        conn = self._get_conn()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        except Exception as e:
            conn.rollback()
            logger.error(f"[DB] Execute error: {e} | SQL: {sql[:100]}")
            raise

    def query(self, sql: str, params: tuple = ()) -> List[Dict]:
        """Execute a read query, return list of dicts."""
        conn = self._get_conn()
        try:
            cur  = conn.execute(sql, params)
            rows = cur.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"[DB] Query error: {e} | SQL: {sql[:100]}")
            return []

    def query_one(self, sql: str, params: tuple = ()) -> Optional[Dict]:
        """Execute a read query, return single dict or None."""
        results = self.query(sql, params)
        return results[0] if results else None

    # ─────────────────────────────────────────────────────────────────────────
    # TRADES
    # ─────────────────────────────────────────────────────────────────────────

    def save_trade(self, trade: dict) -> bool:
        """Save a closed trade to the database."""
        try:
            self.execute("""
                INSERT OR REPLACE INTO trades (
                    trade_id, symbol, direction, entry_price, exit_price,
                    stop_loss, tp1_price, tp2_price, lot_size, r_multiple,
                    outcome, grade, confidence, exchange, is_paper,
                    open_time, close_time, hold_duration_h, pnl_pct, node, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.get("trade_id"),
                trade.get("symbol"),
                trade.get("direction"),
                trade.get("entry_price"),
                trade.get("exit_price"),
                trade.get("stop_loss", trade.get("original_sl", trade.get("current_sl"))),
                trade.get("tp1_price"),
                trade.get("tp2_price"),
                trade.get("lot_size"),
                trade.get("r_multiple"),
                trade.get("outcome"),
                trade.get("grade"),
                trade.get("confidence"),
                trade.get("exchange"),
                1 if trade.get("is_paper", True) else 0,
                trade.get("open_time"),
                trade.get("close_time"),
                trade.get("hold_duration_h"),
                trade.get("pnl_pct"),
                trade.get("node"),
                json.dumps(trade.get("notes", {})),
            ))
            return True
        except Exception as e:
            logger.error(f"[DB] save_trade failed: {e}")
            return False

    def get_recent_trades(self, limit: int = 50,
                           is_paper: Optional[bool] = None) -> List[Dict]:
        """Get recent closed trades."""
        if is_paper is None:
            return self.query(
                "SELECT * FROM trades ORDER BY close_time DESC LIMIT ?",
                (limit,)
            )
        return self.query(
            "SELECT * FROM trades WHERE is_paper=? ORDER BY close_time DESC LIMIT ?",
            (1 if is_paper else 0, limit)
        )

    def get_trade_stats(self, days: int = 30,
                         is_paper: bool = True) -> Dict:
        """Calculate win rate, expectancy, profit factor for recent period."""
        cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = self.query("""
            SELECT r_multiple, outcome FROM trades
            WHERE is_paper=? AND close_time >= date('now', ?)
            AND r_multiple IS NOT NULL
        """, (1 if is_paper else 0, f"-{days} days"))

        if not trades:
            return {"trades": 0, "message": "No trades in period."}

        r_vals   = [t["r_multiple"] for t in trades]
        wins     = sum(1 for r in r_vals if r > 0)
        total    = len(r_vals)
        win_rate = wins / total * 100 if total > 0 else 0

        gross_p  = sum(r for r in r_vals if r > 0)
        gross_l  = abs(sum(r for r in r_vals if r < 0))
        pf       = gross_p / gross_l if gross_l > 0 else 999.0

        return {
            "trades":       total,
            "wins":         wins,
            "losses":       total - wins,
            "win_rate":     f"{win_rate:.1f}%",
            "expectancy_r": f"{sum(r_vals)/total:.3f}R",
            "profit_factor": f"{pf:.2f}",
            "best_trade":   f"+{max(r_vals):.1f}R",
            "worst_trade":  f"{min(r_vals):.1f}R",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # SIGNALS
    # ─────────────────────────────────────────────────────────────────────────

    def save_signal(self, signal: dict) -> bool:
        """Save a signal (whether traded or skipped)."""
        try:
            self.execute("""
                INSERT OR IGNORE INTO signals (
                    signal_id, symbol, direction, confidence, grade,
                    was_traded, skip_reason, entry_price, atr, session,
                    macro_trend, fvg_hit, ob_hit, sweep_detected,
                    rsi_divergence, volume_signal, structure_event, signal_time
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                signal.get("signal_id"),
                signal.get("symbol"),
                signal.get("direction"),
                signal.get("confidence"),
                signal.get("grade"),
                1 if signal.get("was_traded") else 0,
                signal.get("skip_reason"),
                signal.get("entry_price"),
                signal.get("atr"),
                signal.get("session"),
                signal.get("macro_trend"),
                1 if signal.get("fvg_hit") else 0,
                1 if signal.get("ob_hit") else 0,
                1 if signal.get("sweep_detected") else 0,
                signal.get("rsi_divergence"),
                signal.get("volume_signal"),
                signal.get("structure_event"),
                signal.get("signal_time",
                           datetime.now(timezone.utc).isoformat()),
            ))
            return True
        except Exception as e:
            logger.error(f"[DB] save_signal failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # POSITIONS
    # ─────────────────────────────────────────────────────────────────────────

    def upsert_position(self, position: dict) -> bool:
        """Insert or update an open position."""
        try:
            self.execute("""
                INSERT OR REPLACE INTO positions (
                    trade_id, symbol, direction, entry_price,
                    current_sl, tp1_price, current_tp, original_tp2,
                    lot_size, remaining_pct, tp1_hit, grade, exchange,
                    is_paper, open_time, atr_at_open, sl_multiplier,
                    node, last_updated
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                position.get("trade_id"),
                position.get("symbol"),
                position.get("direction"),
                position.get("entry_price"),
                position.get("current_sl"),
                position.get("tp1_price"),
                position.get("current_tp"),
                position.get("original_tp2"),
                position.get("lot_size"),
                position.get("remaining_pct", 100),
                1 if position.get("tp1_hit") else 0,
                position.get("grade"),
                position.get("exchange"),
                1 if position.get("is_paper", True) else 0,
                position.get("open_time"),
                position.get("atr_at_open"),
                position.get("sl_multiplier"),
                position.get("node"),
                datetime.now(timezone.utc).isoformat(),
            ))
            return True
        except Exception as e:
            logger.error(f"[DB] upsert_position failed: {e}")
            return False

    def delete_position(self, trade_id: str) -> bool:
        """Remove a position when it closes."""
        try:
            self.execute("DELETE FROM positions WHERE trade_id=?", (trade_id,))
            return True
        except Exception as e:
            logger.error(f"[DB] delete_position failed: {e}")
            return False

    def get_open_positions(self) -> List[Dict]:
        """Get all currently open positions."""
        return self.query(
            "SELECT * FROM positions ORDER BY open_time ASC"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # OHLCV CACHE
    # ─────────────────────────────────────────────────────────────────────────

    def cache_ohlcv(self, symbol: str, timeframe: str,
                    df) -> int:
        """
        Cache OHLCV data to avoid repeated API calls.
        Returns number of rows inserted.

        PRIORITY 3: Uses executemany() instead of row-by-row INSERT.
        Batches all rows into a single transaction = 10-50× faster.
        Critical at 06:30 UTC when bias engine writes 100+ candles per symbol.
        """
        import pandas as pd
        conn = self._get_conn()
        try:
            # Build batch of tuples — one pass through DataFrame
            rows = []
            for _, row in df.iterrows():
                t = row["time"]
                t = t.isoformat() if hasattr(t, "isoformat") else str(t)
                rows.append((
                    symbol, timeframe, t,
                    float(row["open"]), float(row["high"]),
                    float(row["low"]),  float(row["close"]),
                    float(row.get("volume", 0)),
                ))

            if not rows:
                return 0

            conn.executemany(
                """INSERT OR IGNORE INTO ohlcv
                   (symbol, timeframe, time, open, high, low, close, volume)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )
            conn.commit()
            inserted = len(rows)
            logger.debug(
                f"[DB] cache_ohlcv: {inserted} rows for {symbol} {timeframe} "
                f"(executemany batch)"
            )
            return inserted
        except Exception as e:
            conn.rollback()
            logger.error(f"[DB] cache_ohlcv failed: {e}")
            return 0

    def get_cached_ohlcv(self, symbol: str, timeframe: str,
                          limit: int = 500):
        """
        Retrieve cached OHLCV as a pandas DataFrame.
        Returns None if not enough data cached.
        """
        try:
            import pandas as pd
            rows = self.query("""
                SELECT time, open, high, low, close, volume
                FROM ohlcv
                WHERE symbol=? AND timeframe=?
                ORDER BY time DESC
                LIMIT ?
            """, (symbol, timeframe, limit))

            if len(rows) < 20:
                return None

            df = pd.DataFrame(rows)
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df = df.sort_values("time").reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"[DB] get_cached_ohlcv failed: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # ML FEATURES
    # ─────────────────────────────────────────────────────────────────────────

    def save_ml_features(self, signal_id: str, symbol: str,
                          signal_time: str, features: dict) -> bool:
        """
        Save feature snapshot at signal time.
        Outcome filled in later when trade closes.
        This builds the training dataset for future ML models.
        """
        try:
            self.execute("""
                INSERT OR IGNORE INTO ml_features
                (signal_id, symbol, signal_time, features)
                VALUES (?,?,?,?)
            """, (signal_id, symbol, signal_time, json.dumps(features)))
            return True
        except Exception as e:
            logger.error(f"[DB] save_ml_features failed: {e}")
            return False

    def update_ml_outcome(self, signal_id: str,
                           outcome: str, r_multiple: float) -> bool:
        """Fill in outcome after trade closes — completes the training row."""
        try:
            self.execute("""
                UPDATE ml_features
                SET outcome=?, r_multiple=?
                WHERE signal_id=?
            """, (outcome, r_multiple, signal_id))
            return True
        except Exception as e:
            logger.error(f"[DB] update_ml_outcome failed: {e}")
            return False

    def get_ml_training_data(self, min_rows: int = 100) -> Optional[list]:
        """
        Get all completed ML feature rows (has outcome + r_multiple).
        Returns None if fewer than min_rows available.
        """
        rows = self.query("""
            SELECT features, outcome, r_multiple
            FROM ml_features
            WHERE outcome IS NOT NULL AND r_multiple IS NOT NULL
            ORDER BY created_at ASC
        """)
        if len(rows) < min_rows:
            logger.info(f"[DB] ML data: {len(rows)}/{min_rows} rows ready.")
            return None
        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # STREAK / DAILY STATS
    # ─────────────────────────────────────────────────────────────────────────

    def save_day_stats(self, date: str, pnl_pct: float,
                        consecutive_losses: int,
                        circuit_breaker_fired: bool,
                        risk_level: str,
                        trades_today: int) -> bool:
        try:
            self.execute("""
                INSERT OR REPLACE INTO streak_history
                (date, day_pnl_pct, consecutive_loss_days,
                 circuit_breaker_fired, risk_level, trades_today)
                VALUES (?,?,?,?,?,?)
            """, (date, pnl_pct, consecutive_losses,
                  1 if circuit_breaker_fired else 0,
                  risk_level, trades_today))
            return True
        except Exception as e:
            logger.error(f"[DB] save_day_stats failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # DAILY BIAS
    # ─────────────────────────────────────────────────────────────────────────

    def save_daily_bias(self, date: str, symbol: str, bias: str,
                         strength: int, reason: str,
                         invalidation_level: float,
                         key_levels: list) -> bool:
        try:
            self.execute("""
                INSERT OR REPLACE INTO daily_bias
                (date, symbol, bias, strength, reason,
                 daily_inv_level, key_levels)
                VALUES (?,?,?,?,?,?,?)
            """, (date, symbol, bias, strength, reason,
                  invalidation_level, json.dumps(key_levels)))
            return True
        except Exception as e:
            logger.error(f"[DB] save_daily_bias failed: {e}")
            return False

    def get_today_bias(self, symbol: str) -> Optional[Dict]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row   = self.query_one("""
            SELECT * FROM daily_bias WHERE date=? AND symbol=?
        """, (today, symbol))
        if row and row.get("key_levels"):
            try:
                row["key_levels"] = json.loads(row["key_levels"])
            except Exception:
                pass
        return row

    # ─────────────────────────────────────────────────────────────────────────
    # MAINTENANCE
    # ─────────────────────────────────────────────────────────────────────────

    def prune_old_ohlcv(self, keep_days: int = 90):
        """
        Delete OHLCV cache older than keep_days.
        Keeps database lean — phone storage is limited.
        """
        deleted = self.execute("""
            DELETE FROM ohlcv
            WHERE time < datetime('now', ?)
        """, (f"-{keep_days} days",)).rowcount
        logger.info(f"[DB] Pruned {deleted} old OHLCV rows.")
        return deleted

    def vacuum(self):
        """Reclaim disk space after deletions. Run weekly."""
        conn = self._get_conn()
        conn.execute("VACUUM")
        logger.info("[DB] VACUUM complete.")

    def get_db_size_mb(self) -> float:
        """Return database file size in MB."""
        path = Path(self.db_path)
        if path.exists():
            return round(path.stat().st_size / 1024 / 1024, 2)
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────────────────────
# Import this anywhere:  from core.database_manager import db

_db_instance: Optional[DatabaseManager] = None

def get_db() -> DatabaseManager:
    global _db_instance
    if _db_instance is None:
        _db_instance = DatabaseManager()
    return _db_instance

db = get_db()
