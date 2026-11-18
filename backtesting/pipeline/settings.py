"""
All tunables for the backtest pipeline in ONE place. Every value can be overridden by an
environment variable of the same name (prefix BT_) where it makes sense.
Nothing in here is imported by the live trading path.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def _f(name, default):  return float(os.environ.get("BT_" + name, default))
def _i(name, default):  return int(float(os.environ.get("BT_" + name, default)))
def _s(name, default):  return os.environ.get("BT_" + name, default)

# ── paths ────────────────────────────────────────────────────────────────────
CACHE_DIR = Path(_s("CACHE_DIR", str(ROOT / "data" / "mt5_cache")))
RUNS_DIR  = Path(_s("RUNS_DIR",  str(ROOT / "State" / "backtests")))
DB_PATH   = Path(_s("DB_PATH",   str(ROOT / "Database" / "backtest_runs.db")))
CANDIDATES_FILE = ROOT / "State" / "strategy_candidates.json"

# ── data ─────────────────────────────────────────────────────────────────────
BAR_MINUTES        = 15          # the harness/adapters are built around 15m bars
DEFAULT_DAYS       = 180
MIN_DAYS, MAX_DAYS = 30, 730
MIN_COVERAGE       = _f("MIN_COVERAGE", 0.90)   # share of expected trading days that must have data
TICK_CHUNK_HOURS   = 6
DOWNLOAD_RETRIES   = 3
# MT5 returns timestamps in BROKER SERVER TIME (labelled as if UTC). We convert with a rule:
#   winter offset hours + DST rule ("us" = switches with US DST like most FX brokers incl. XM,
#   "eu" = EET/EEST, "none" = server is UTC).  VERIFY with:  python -m backtesting.pipeline.mt5_downloader --probe
SERVER_UTC_OFFSET_WINTER = _f("MT5_SERVER_UTC_OFFSET", 2)
SERVER_DST_RULE          = _s("MT5_SERVER_DST_RULE", "us")
SYMBOL_OVERRIDES_JSON    = os.environ.get("BT_SYMBOL_OVERRIDES", "")   # {"XAUUSD=X": "GOLD#"}

# ── execution realism ────────────────────────────────────────────────────────
RISK_PER_TRADE     = _f("RISK_PER_TRADE", 0.01)   # additive (non-compounding) sizing for reporting
MAX_OPEN_TRADES    = _i("MAX_OPEN_TRADES", 1)    # per symbol. Legacy harness allowed 23 concurrent.
LOOKAHEAD_BARS     = 200
SLIPPAGE_SPREAD_FRAC = _f("SLIPPAGE_SPREAD_FRAC", 0.25)  # slippage = fraction of the real spread
COMMISSION_PER_LOT_RT = _f("COMMISSION_PER_LOT_RT", 0.0) # account currency per 1.0 lot round turn
MAX_COST_R         = _f("MAX_COST_R", 0.35)      # skip trades whose friction exceeds this share of 1R
ENFORCE_KILL_ZONES = False
USE_MTF_GATE       = True
USE_BIAS_GATE      = True

# ── gates ────────────────────────────────────────────────────────────────────
MIN_TRADES = _i("MIN_TRADES", 30)
MC = dict(N=5000, P_NEG_MAX=0.10, DD95_MAX_PCT=25.0, RUIN_DD_PCT=30.0, P_RUIN_MAX=0.05,
          COST_MULT=2.0, DROP_FRAC=0.15, SEED=12345)
WFO = dict(IS_DAYS=60, OOS_DAYS=20, MIN_WINDOWS=3, MAX_TRIALS=30, MIN_IS_TRADES=20,
           MIN_OOS_TRADES=20, WFE_MIN=0.5, POS_WINDOWS_MIN=0.6, SEED=777)
SENS = dict(STEPS=(-2, -1, 1, 2), SPIKE_RET=0.30, PLATEAU_RET=0.50, JOINT_DRAWS=40,
            JOINT_POS_MIN=0.70, PLATEAU_FRAC_MIN=0.70, REGIME_MIN_TRADES=15,
            REGIME_BLOWUP=-0.25, REGIME_PASS_FRAC=0.50, SEED=4242)

# ── notifications / council ──────────────────────────────────────────────────
COUNCIL_URL       = os.environ.get("COUNCIL_URL", f"http://127.0.0.1:{os.environ.get('COUNCIL_PORT', '8766')}")
COUNCIL_SECRET    = os.environ.get("COUNCIL_API_SECRET", "")
COUNCIL_ON_PHASE_FAILURE = _s("COUNCIL_ON_PHASE_FAILURE", "false").lower() == "true"
COUNCIL_MAX_LINEAGE_ITER = _i("COUNCIL_MAX_LINEAGE_ITER", 3)
COUNCIL_CREATE_CHILD     = _s("COUNCIL_CREATE_CHILD", "true").lower() == "true"
HISTORY_LIMIT = 15
