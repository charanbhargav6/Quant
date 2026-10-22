-- ═══════════════════════════════════════════════════════════════════════════
-- CRAVE Quant — Supabase Schema Migration  (idempotent)
-- Run this in the Supabase SQL editor.
-- Safe to re-run: uses CREATE TABLE IF NOT EXISTS + ADD COLUMN IF NOT EXISTS.
-- ═══════════════════════════════════════════════════════════════════════════
--
-- SECURITY MODEL
-- ──────────────
-- SUPABASE_SERVICE_KEY  → Python engine only (never sent to browser)
--   Bypasses RLS. Used by supabase_pusher.py to write all tables.
--
-- SUPABASE_KEY (anon)   → browser dashboard only (served via /api/supabase/config)
--   Subject to RLS below. Read-only. Cannot INSERT/UPDATE/DELETE.
-- ═══════════════════════════════════════════════════════════════════════════


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. BOT / SYSTEM HEALTH  (realtime heartbeat — one row per node)
-- Dashboard: System Health page, header status chip
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crave_system_status (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);

ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS node_name           TEXT         NOT NULL DEFAULT 'laptop';
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS active_node         TEXT         NOT NULL DEFAULT 'laptop';
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS bot_running         BOOLEAN      NOT NULL DEFAULT false;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS engine_mode         TEXT         NOT NULL DEFAULT 'PAPER';
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS engine_version      TEXT;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS git_hash            TEXT;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS trading_mode        TEXT         NOT NULL DEFAULT 'paper';
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS mt5_connected       BOOLEAN      NOT NULL DEFAULT false;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS binance_connected   BOOLEAN      NOT NULL DEFAULT false;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS zerodha_connected   BOOLEAN      NOT NULL DEFAULT false;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS open_positions      INTEGER      NOT NULL DEFAULT 0;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS today_pnl           NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS today_pnl_pct       NUMERIC(8,4)  NOT NULL DEFAULT 0;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS total_equity        NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS circuit_breaker     BOOLEAN      NOT NULL DEFAULT false;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS cpu_pct             NUMERIC(5,2);
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS ram_pct             NUMERIC(5,2);
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS uptime_seconds      INTEGER;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS last_heartbeat      TIMESTAMPTZ  NOT NULL DEFAULT now();
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS last_trade_at       TIMESTAMPTZ;
ALTER TABLE crave_system_status ADD COLUMN IF NOT EXISTS updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS idx_crave_system_status_node ON crave_system_status (node_name);
ALTER TABLE crave_system_status REPLICA IDENTITY FULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. OPEN POSITIONS  (realtime mirror — upserted every 5s on profit change)
-- Dashboard: Positions page, Overview KPIs
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crave_open_positions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);

ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS trade_id        TEXT          NOT NULL;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS ticket          BIGINT;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS account_id      TEXT          NOT NULL DEFAULT 'mt5';
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS symbol          TEXT          NOT NULL DEFAULT '';
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS broker_symbol   TEXT;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS direction       TEXT          NOT NULL DEFAULT 'buy';
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS volume          NUMERIC(12,4) NOT NULL DEFAULT 0;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS entry_price     NUMERIC(16,6) NOT NULL DEFAULT 0;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS current_price   NUMERIC(16,6);
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS stop_loss       NUMERIC(16,6);
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS take_profit     NUMERIC(16,6);
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS tp1_price       NUMERIC(16,6);
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS tp1_hit         BOOLEAN       NOT NULL DEFAULT false;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS remaining_pct   NUMERIC(6,2)  NOT NULL DEFAULT 100;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS profit          NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS profit_pct      NUMERIC(8,4)  NOT NULL DEFAULT 0;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS pips_moved      NUMERIC(10,2);
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS swap            NUMERIC(10,2);
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS strategy_id     TEXT;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS grade           TEXT;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS confidence      INTEGER;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS exchange        TEXT          NOT NULL DEFAULT 'mt5';
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS is_paper        BOOLEAN       NOT NULL DEFAULT false;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS node            TEXT;
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS open_time       TIMESTAMPTZ   NOT NULL DEFAULT now();
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS atr_at_open     NUMERIC(12,8);
ALTER TABLE crave_open_positions ADD COLUMN IF NOT EXISTS last_updated    TIMESTAMPTZ   NOT NULL DEFAULT now();

-- Unique constraint on trade_id (needed for upsert)
DO $$ BEGIN
    ALTER TABLE crave_open_positions ADD CONSTRAINT crave_open_positions_trade_id_key UNIQUE (trade_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

ALTER TABLE crave_open_positions REPLICA IDENTITY FULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. CLOSED TRADES  (historical — written once per trade close)
-- Dashboard: Trade Journal, Analytics, Strategy pages
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crave_trades (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);

ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS trade_id        TEXT          NOT NULL;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS ticket          BIGINT;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS account_id      TEXT          NOT NULL DEFAULT 'mt5';
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS symbol          TEXT          NOT NULL DEFAULT '';
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS direction       TEXT          NOT NULL DEFAULT 'buy';
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS volume          NUMERIC(12,4) NOT NULL DEFAULT 0;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS entry_price     NUMERIC(16,6);
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS exit_price      NUMERIC(16,6);
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS stop_loss       NUMERIC(16,6);
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS tp1_price       NUMERIC(16,6);
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS tp2_price       NUMERIC(16,6);
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS profit_dollars  NUMERIC(12,2);
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS profit_pct      NUMERIC(8,4);
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS r_multiple      NUMERIC(8,4);
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS outcome         TEXT;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS grade           TEXT;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS confidence      INTEGER;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS strategy_id     TEXT;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS node            TEXT;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS exchange        TEXT          NOT NULL DEFAULT 'mt5';
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS is_paper        BOOLEAN       NOT NULL DEFAULT false;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS open_time       TIMESTAMPTZ   NOT NULL DEFAULT now();
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS close_time      TIMESTAMPTZ;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS hold_duration_h NUMERIC(8,2);
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS notes           JSONB;
ALTER TABLE crave_trades ADD COLUMN IF NOT EXISTS created_at      TIMESTAMPTZ   NOT NULL DEFAULT now();

DO $$ BEGIN
    ALTER TABLE crave_trades ADD CONSTRAINT crave_trades_trade_id_key UNIQUE (trade_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_crave_trades_close_time ON crave_trades (close_time DESC);
CREATE INDEX IF NOT EXISTS idx_crave_trades_symbol     ON crave_trades (symbol);
CREATE INDEX IF NOT EXISTS idx_crave_trades_strategy   ON crave_trades (strategy_id);
CREATE INDEX IF NOT EXISTS idx_crave_trades_account    ON crave_trades (account_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. DAILY P&L  (written at session close ~21:00 UTC)
-- Dashboard: P&L calendar, Analytics equity curve
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crave_daily_pnl (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);

ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS account_id             TEXT         NOT NULL DEFAULT 'mt5';
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS session_date           DATE         NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS open_equity            NUMERIC(12,2);
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS close_equity           NUMERIC(12,2);
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS pnl_dollars            NUMERIC(12,2);
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS pnl_pct                NUMERIC(8,4);
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS realized_pnl           NUMERIC(12,2);
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS unrealized_pnl         NUMERIC(12,2);
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS trades_count           INTEGER      NOT NULL DEFAULT 0;
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS wins                   INTEGER      NOT NULL DEFAULT 0;
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS losses                 INTEGER      NOT NULL DEFAULT 0;
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS max_drawdown_pct       NUMERIC(8,4);
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS circuit_breaker_fired  BOOLEAN      NOT NULL DEFAULT false;
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS consecutive_loss_days  INTEGER      NOT NULL DEFAULT 0;
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS risk_level             TEXT;
ALTER TABLE crave_daily_pnl ADD COLUMN IF NOT EXISTS created_at             TIMESTAMPTZ  NOT NULL DEFAULT now();

DO $$ BEGIN
    ALTER TABLE crave_daily_pnl ADD CONSTRAINT crave_daily_pnl_account_date_key UNIQUE (account_id, session_date);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_crave_daily_pnl_date ON crave_daily_pnl (session_date DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. PAPER TRADING EQUITY CURVE  (snapshot every 60s during paper session)
-- Dashboard: Paper equity chart
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crave_paper_equity (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);

ALTER TABLE crave_paper_equity ADD COLUMN IF NOT EXISTS ts              TIMESTAMPTZ   NOT NULL DEFAULT now();
ALTER TABLE crave_paper_equity ADD COLUMN IF NOT EXISTS equity          NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE crave_paper_equity ADD COLUMN IF NOT EXISTS balance         NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE crave_paper_equity ADD COLUMN IF NOT EXISTS open_positions  INTEGER       NOT NULL DEFAULT 0;
ALTER TABLE crave_paper_equity ADD COLUMN IF NOT EXISTS floating_pnl   NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE crave_paper_equity ADD COLUMN IF NOT EXISTS drawdown_pct   NUMERIC(8,4)  NOT NULL DEFAULT 0;
ALTER TABLE crave_paper_equity ADD COLUMN IF NOT EXISTS trades_today   INTEGER       NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_crave_paper_equity_ts ON crave_paper_equity (ts DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- 6. PORTFOLIO HEAT  (exposure snapshot every 60s when positions exist)
-- Dashboard: Portfolio Heat page, Overview exposure bar
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crave_portfolio_heat (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);

ALTER TABLE crave_portfolio_heat ADD COLUMN IF NOT EXISTS account_id    TEXT          NOT NULL DEFAULT 'ALL';
ALTER TABLE crave_portfolio_heat ADD COLUMN IF NOT EXISTS snapshot_ts   TIMESTAMPTZ   NOT NULL DEFAULT now();
ALTER TABLE crave_portfolio_heat ADD COLUMN IF NOT EXISTS total_equity  NUMERIC(12,2);
ALTER TABLE crave_portfolio_heat ADD COLUMN IF NOT EXISTS items         JSONB         NOT NULL DEFAULT '[]';
ALTER TABLE crave_portfolio_heat ADD COLUMN IF NOT EXISTS corr_groups   JSONB         NOT NULL DEFAULT '[]';

CREATE INDEX IF NOT EXISTS idx_crave_portfolio_heat_ts ON crave_portfolio_heat (snapshot_ts DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- 7. NODE HEALTH  (one row per machine, upserted every 30s)
-- Dashboard: System Health page
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crave_nodes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);

ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS node_name       TEXT         NOT NULL;
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS status          TEXT         NOT NULL DEFAULT 'idle';
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS ip_address      TEXT;
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS cpu_pct         NUMERIC(5,2);
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS ram_pct         NUMERIC(5,2);
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS disk_pct        NUMERIC(5,2);
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS uptime_seconds  INTEGER;
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS git_hash        TEXT;
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS engine_version  TEXT;
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS last_heartbeat  TIMESTAMPTZ  NOT NULL DEFAULT now();
ALTER TABLE crave_nodes ADD COLUMN IF NOT EXISTS extra           JSONB;

DO $$ BEGIN
    ALTER TABLE crave_nodes ADD CONSTRAINT crave_nodes_node_name_key UNIQUE (node_name);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_crave_nodes_name ON crave_nodes (node_name);
ALTER TABLE crave_nodes REPLICA IDENTITY FULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- 8. ALERTS  (active risk/news alerts — realtime)
-- Dashboard: Header alerts, Risk page
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crave_alerts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);

ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS alert_id        TEXT         NOT NULL;
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS level           TEXT         NOT NULL DEFAULT 'info';
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS category        TEXT         NOT NULL DEFAULT 'system';
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS text            TEXT         NOT NULL DEFAULT '';
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS symbol          TEXT;
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS account_id      TEXT;
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS action_required BOOLEAN      NOT NULL DEFAULT false;
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS auto_action     TEXT;
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS is_active       BOOLEAN      NOT NULL DEFAULT true;
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS ts              TIMESTAMPTZ  NOT NULL DEFAULT now();
ALTER TABLE crave_alerts ADD COLUMN IF NOT EXISTS expires_at      TIMESTAMPTZ;

DO $$ BEGIN
    ALTER TABLE crave_alerts ADD CONSTRAINT crave_alerts_alert_id_key UNIQUE (alert_id);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;

ALTER TABLE crave_alerts REPLICA IDENTITY FULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- 9. ML BACKTEST RESULTS  (already written by ml/backtest_runner.py)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ml_backtest_results (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);

ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS run_id      TEXT          NOT NULL DEFAULT '';
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS symbol      TEXT          NOT NULL DEFAULT '';
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS dataset     TEXT;
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS rows        INTEGER;
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS mean_acc    NUMERIC(6,4);
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS std_acc     NUMERIC(6,4);
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS min_acc     NUMERIC(6,4);
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS max_acc     NUMERIC(6,4);
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS degradation NUMERIC(6,4);
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS details     JSONB;
ALTER TABLE ml_backtest_results ADD COLUMN IF NOT EXISTS created_at  TIMESTAMPTZ   NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_ml_backtest_run_id ON ml_backtest_results (run_id, created_at DESC);


-- ═══════════════════════════════════════════════════════════════════════════
-- ROW LEVEL SECURITY
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE crave_system_status   ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_open_positions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_trades          ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_daily_pnl       ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_paper_equity    ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_portfolio_heat  ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_nodes           ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_alerts          ENABLE ROW LEVEL SECURITY;
ALTER TABLE ml_backtest_results   ENABLE ROW LEVEL SECURITY;


-- ── Browser read policies (anon key) ─────────────────────────────────────────
-- Drop first to avoid "policy already exists" errors on re-run

DROP POLICY IF EXISTS "dashboard_read_system_status"  ON crave_system_status;
DROP POLICY IF EXISTS "dashboard_read_positions"      ON crave_open_positions;
DROP POLICY IF EXISTS "dashboard_read_trades"         ON crave_trades;
DROP POLICY IF EXISTS "dashboard_read_daily_pnl"      ON crave_daily_pnl;
DROP POLICY IF EXISTS "dashboard_read_paper_equity"   ON crave_paper_equity;
DROP POLICY IF EXISTS "dashboard_read_portfolio_heat" ON crave_portfolio_heat;
DROP POLICY IF EXISTS "dashboard_read_nodes"          ON crave_nodes;
DROP POLICY IF EXISTS "dashboard_read_alerts"         ON crave_alerts;
DROP POLICY IF EXISTS "dashboard_read_ml_backtest"    ON ml_backtest_results;

CREATE POLICY "dashboard_read_system_status"  ON crave_system_status  FOR SELECT USING (true);
CREATE POLICY "dashboard_read_positions"      ON crave_open_positions  FOR SELECT USING (true);
CREATE POLICY "dashboard_read_trades"         ON crave_trades          FOR SELECT USING (true);
CREATE POLICY "dashboard_read_daily_pnl"      ON crave_daily_pnl       FOR SELECT USING (true);
CREATE POLICY "dashboard_read_paper_equity"   ON crave_paper_equity    FOR SELECT USING (true);
CREATE POLICY "dashboard_read_portfolio_heat" ON crave_portfolio_heat  FOR SELECT USING (true);
CREATE POLICY "dashboard_read_nodes"          ON crave_nodes           FOR SELECT USING (true);
CREATE POLICY "dashboard_read_alerts"         ON crave_alerts          FOR SELECT USING (true);
CREATE POLICY "dashboard_read_ml_backtest"    ON ml_backtest_results   FOR SELECT USING (true);


-- ── Block all browser writes (service role bypasses RLS for Python writes) ─────

DROP POLICY IF EXISTS "no_browser_insert_system_status"  ON crave_system_status;
DROP POLICY IF EXISTS "no_browser_insert_positions"      ON crave_open_positions;
DROP POLICY IF EXISTS "no_browser_insert_trades"         ON crave_trades;
DROP POLICY IF EXISTS "no_browser_insert_daily_pnl"      ON crave_daily_pnl;
DROP POLICY IF EXISTS "no_browser_insert_paper_equity"   ON crave_paper_equity;
DROP POLICY IF EXISTS "no_browser_insert_portfolio_heat" ON crave_portfolio_heat;
DROP POLICY IF EXISTS "no_browser_insert_nodes"          ON crave_nodes;
DROP POLICY IF EXISTS "no_browser_insert_alerts"         ON crave_alerts;

CREATE POLICY "no_browser_insert_system_status"  ON crave_system_status  FOR INSERT WITH CHECK (false);
CREATE POLICY "no_browser_insert_positions"      ON crave_open_positions  FOR INSERT WITH CHECK (false);
CREATE POLICY "no_browser_insert_trades"         ON crave_trades          FOR INSERT WITH CHECK (false);
CREATE POLICY "no_browser_insert_daily_pnl"      ON crave_daily_pnl       FOR INSERT WITH CHECK (false);
CREATE POLICY "no_browser_insert_paper_equity"   ON crave_paper_equity    FOR INSERT WITH CHECK (false);
CREATE POLICY "no_browser_insert_portfolio_heat" ON crave_portfolio_heat  FOR INSERT WITH CHECK (false);
CREATE POLICY "no_browser_insert_nodes"          ON crave_nodes           FOR INSERT WITH CHECK (false);
CREATE POLICY "no_browser_insert_alerts"         ON crave_alerts          FOR INSERT WITH CHECK (false);


-- ═══════════════════════════════════════════════════════════════════════════
-- REALTIME  (enable Postgres logical replication for live subscriptions)
-- ═══════════════════════════════════════════════════════════════════════════

DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE crave_system_status;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE crave_open_positions;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE crave_nodes;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE crave_alerts;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
