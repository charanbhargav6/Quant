"""
CRAVE — ML Backtest Runner
===========================
Downloads top-tier public datasets, trains the XGBoost regime classifier,
runs walk-forward backtests on each instrument, and writes results to:
  1. SQLite (ml_backtest_results table)  — for the local dashboard
  2. Supabase  (ml_backtest_results table) — for the live Next.js dashboard

Run in background:
  python Sub_Projects/Trading/ml/backtest_runner.py

Or silently (Windows):
  pythonw Sub_Projects/Trading/ml/backtest_runner.py

Results auto-posted to dashboard when complete.

DATASETS USED (all free, no API key needed):
  • yfinance   — XAUUSD (Gold), EURUSD, NIFTY50, SPY (10yr daily + 2yr hourly)
  • Binance    — BTCUSDT, ETHUSDT (5yr 1H via public REST, no key)
  • Kraken     — XBTUSD 1H historical (public download)
  • CryptoDataDownload — BTCUSDT Gemini 1-min aggregated to 1H (free CSV)
"""

import os, sys, time, json, logging, sqlite3, threading, traceback, io
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Force UTF-8 on standard streams to avoid Windows charmap crashes with emojis
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── Path bootstrap ─────────────────────────────────────────────────────────
CRAVE_ROOT = Path(os.environ.get("CRAVE_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(CRAVE_ROOT))

# ── Logging ────────────────────────────────────────────────────────────────
LOG_PATH = CRAVE_ROOT / "data" / "logs" / "backtest_runner.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("crave.ml.backtest_runner")

DB_PATH     = CRAVE_ROOT / "data" / "trades.db"
MODELS_DIR  = CRAVE_ROOT / "Sub_Projects" / "Trading" / "ml" / "models"
CACHE_DIR   = CRAVE_ROOT / "data" / "dataset_cache"
RESULTS_KEY = "ml_backtest_results"

MODELS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── Status file (dashboard polls this) ────────────────────────────────────
STATUS_PATH = CRAVE_ROOT / "data" / "backtest_status.json"

def _write_status(status: str, progress: int, message: str, results=None):
    data = {
        "status":    status,       # "idle" | "running" | "done" | "error"
        "progress":  progress,     # 0-100
        "message":   message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "results":   results or [],
    }
    STATUS_PATH.write_text(json.dumps(data, indent=2))
    log.info(f"[{status.upper()}] {progress}% — {message}")


# ═══════════════════════════════════════════════════════════════════════════
# 1. DATASET DOWNLOADERS
# ═══════════════════════════════════════════════════════════════════════════

def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{name}.parquet"


def download_yfinance(symbol: str, period: str = "5y",
                      interval: str = "1h", name: str = None) -> "pd.DataFrame | None":
    """Download OHLCV from Yahoo Finance via yfinance (free, no API key)."""
    import pandas as pd
    import yfinance as yf

    cname = name or f"yf_{symbol}_{interval}"
    cache = _cache_path(cname)
    if cache.exists():
        age_h = (time.time() - cache.stat().st_mtime) / 3600
        if age_h < 24:
            log.info(f"  Cache hit: {cname}")
            return pd.read_parquet(cache)

    log.info(f"  Downloading {symbol} ({interval}, {period}) from Yahoo Finance…")
    try:
        df = yf.download(symbol, period=period, interval=interval,
                         auto_adjust=True, progress=False)
        if df is None or len(df) < 100:
            log.warning(f"  yfinance returned no data for {symbol}")
            return None
        df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
        df.index   = pd.to_datetime(df.index, utc=True)
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        df.to_parquet(cache)
        log.info(f"  {symbol}: {len(df)} rows cached")
        return df
    except Exception as e:
        log.warning(f"  yfinance error for {symbol}: {e}")
        return None


def download_binance_public(symbol: str = "BTCUSDT",
                             interval: str = "1h",
                             years: int = 4) -> "pd.DataFrame | None":
    """
    Download from Binance public REST API — no API key needed.
    Max 1000 bars per request, loops to get full history.
    """
    import pandas as pd
    import requests

    cname = f"binance_{symbol}_{interval}_{years}y"
    cache = _cache_path(cname)
    if cache.exists():
        age_h = (time.time() - cache.stat().st_mtime) / 3600
        if age_h < 24:
            log.info(f"  Cache hit: {cname}")
            return pd.read_parquet(cache)

    log.info(f"  Downloading {symbol} ({interval}, {years}yr) from Binance…")
    base_url = "https://api.binance.com/api/v3/klines"
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=365*years)).timestamp() * 1000)

    all_rows = []
    current  = start_ms
    limit    = 1000

    while current < end_ms:
        try:
            resp = requests.get(base_url, params={
                "symbol":    symbol,
                "interval":  interval,
                "startTime": current,
                "endTime":   end_ms,
                "limit":     limit,
            }, timeout=15)
            rows = resp.json()
            if not rows or isinstance(rows, dict):
                break
            all_rows.extend(rows)
            current = rows[-1][0] + 1
            if len(rows) < limit:
                break
            time.sleep(0.1)   # be nice to public API
        except Exception as e:
            log.warning(f"  Binance chunk error: {e}")
            break

    if not all_rows:
        log.warning(f"  Binance returned no data for {symbol}")
        return None

    df = pd.DataFrame(all_rows, columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df = df[["open","high","low","close","volume"]].astype(float)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df.to_parquet(cache)
    log.info(f"  Binance {symbol}: {len(df)} rows cached")
    return df


# ═══════════════════════════════════════════════════════════════════════════
# 2. FEATURE EXTRACTION ON HISTORICAL DATA
# ═══════════════════════════════════════════════════════════════════════════

def _compute_features_on_df(df: "pd.DataFrame", symbol: str) -> "pd.DataFrame":
    """
    Compute CRAVE's 26 ML features on every row of a historical OHLCV dataframe.
    Returns a DataFrame with features + regime_label for training.
    """
    import numpy as np
    import pandas as pd

    result = []
    min_lookback = 220   # need 200 bars for EMA200

    for i in range(min_lookback, len(df)):
        window = df.iloc[max(0, i-500):i+1]
        row    = df.iloc[i]
        close  = float(row["close"])
        feats  = {}

        # Time
        ts = window.index[-1]
        feats["utc_hour"]    = ts.hour
        feats["day_of_week"] = ts.weekday()

        # ATR
        tr = pd.concat([
            window["high"] - window["low"],
            (window["high"] - window["close"].shift()).abs(),
            (window["low"]  - window["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1])
        feats["atr_pct"] = round(atr / close * 100, 4) if close > 0 else 0

        # ATR expansion — independent historical baseline (not sampled from same EWM)
        if len(window) >= 480:
            baseline_end   = max(0, len(window) - 50)
            baseline_start = max(0, baseline_end - 480)
            hist = window.iloc[baseline_start:baseline_end]
            if len(hist) >= 14:
                hist_tr = pd.concat([
                    hist["high"] - hist["low"],
                    (hist["high"] - hist["close"].shift()).abs(),
                    (hist["low"]  - hist["close"].shift()).abs(),
                ], axis=1).max(axis=1)
                atr_20d = float(hist_tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1])
            else:
                atr_20d = atr
            feats["atr_expansion"] = round(atr / atr_20d, 3) if atr_20d > 0 else 1.0
        else:
            feats["atr_expansion"] = 1.0

        # EMAs
        ema21  = float(window["close"].ewm(span=21, adjust=False).mean().iloc[-1])
        ema50  = float(window["close"].rolling(50).mean().iloc[-1]) if len(window)>=50 else close
        ema200 = float(window["close"].rolling(200).mean().iloc[-1]) if len(window)>=200 else close

        feats["above_ema21"]        = int(close > ema21)
        feats["above_ema50"]        = int(close > ema50)
        feats["above_ema200"]       = int(close > ema200)
        feats["ema21_above_ema50"]  = int(ema21 > ema50)
        feats["ema21"]              = float(ema21)
        feats["ema50"]              = float(ema50)
        feats["ema200"]             = float(ema200)

        # Swing proximity
        w = 5
        highs = [window["high"].iloc[j] for j in range(w, len(window)-w)
                 if window["high"].iloc[j] == window["high"].iloc[j-w:j+w+1].max()]
        lows  = [window["low"].iloc[j]  for j in range(w, len(window)-w)
                 if window["low"].iloc[j]  == window["low"].iloc[j-w:j+w+1].min()]

        feats["dist_to_swing_high_atr"] = (
            round(abs(close - min(highs, key=lambda h: abs(h-close))) / atr, 2)
            if highs and atr > 0 else 10.0
        )
        feats["dist_to_swing_low_atr"] = (
            round(abs(close - min(lows, key=lambda l: abs(l-close))) / atr, 2)
            if lows  and atr > 0 else 10.0
        )

        # P/D position (50-bar range)
        recent_high = float(window["high"].tail(50).max())
        recent_low  = float(window["low"].tail(50).min())
        rng = recent_high - recent_low
        feats["pd_position"] = round((close - recent_low) / rng, 3) if rng > 0 else 0.5

        # Volume signal
        vol_ma = float(window["volume"].rolling(20).mean().iloc[-1]) if len(window)>=20 else 1
        feats["volume_signal"]    = int(float(row["volume"]) > vol_ma)
        feats["volume_expanding"] = int(float(row["volume"]) > vol_ma * 1.5)

        # Momentum / structure (simplified)
        if len(window) >= 3:
            feats["structure_event"] = (
                2 if window["close"].iloc[-1] > window["high"].iloc[-3:-1].max() else
               -2 if window["close"].iloc[-1] < window["low"].iloc[-3:-1].min() else 0
            )
        else:
            feats["structure_event"] = 0

        # Placeholders for signal features (n/a in historical mode)
        feats["confidence"]           = 50
        feats["fvg_hit"]              = 0
        feats["ob_hit"]               = 0
        feats["sweep_detected"]       = 0
        feats["rsi_divergence_type"]  = 0
        feats["macro_trend"]          = 1 if close > ema200 else -1
        feats["funding_rate"]         = 0.0
        feats["session"]              = feats["utc_hour"] // 8   # rough session encode
        try:
            from config.config import get_asset_class as _gac
            feats["asset_class"] = {
                "crypto": 0, "gold": 1, "silver": 2,
                "forex": 3, "stocks": 4, "indices": 5,
            }.get(_gac(symbol), -1)
        except Exception:
            feats["asset_class"] = -1

        # ── GROUND-TRUTH REGIME LABEL ──────────────────────────────────────
        # Look 10 bars FORWARD to label what regime this candle was in
        # (this is the "session-tagged ground truth" from the implementation plan)
        if i + 10 < len(df):
            future_close = float(df.iloc[i+10]["close"])
            r_proxy      = (future_close - close) / (atr * 2) if atr > 0 else 0

            if feats["atr_expansion"] > 1.4:
                label = 3   # VOLATILE
            elif ema21 > ema50 > ema200 and r_proxy > 0:
                label = 0   # TRENDING_UP
            elif ema21 < ema50 < ema200 and r_proxy < 0:
                label = 1   # TRENDING_DOWN
            else:
                label = 2   # RANGING

            feats["regime_label"] = label
            feats["r_multiple"]   = round(r_proxy, 3)
            feats["outcome"]      = "win" if r_proxy > 0.5 else "loss"
            result.append(feats)

    import pandas as pd
    return pd.DataFrame(result)


# ═══════════════════════════════════════════════════════════════════════════
# 3. WALK-FORWARD BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════

def walk_forward_test(df_features: "pd.DataFrame",
                      symbol: str,
                      n_splits: int = 6) -> dict:
    """
    Time-series walk-forward cross-validation.
    Splits data into train (80%) + test (20%) across n_splits windows.
    Returns per-window accuracy + aggregate stats.
    """
    import numpy as np
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import accuracy_score, classification_report
    from xgboost import XGBClassifier

    feature_cols = [c for c in df_features.columns
                    if c not in ("regime_label", "r_multiple", "outcome",
                                 "outcome_class", "close_price")]
    X = df_features[feature_cols].fillna(0).values
    y = df_features["regime_label"].values

    tscv    = TimeSeriesSplit(n_splits=n_splits)
    windows = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        scaler  = StandardScaler()
        X_tr_s  = scaler.fit_transform(X_train)
        X_te_s  = scaler.transform(X_test)

        model = XGBClassifier(
            n_estimators=150,
            max_depth=5,
            learning_rate=0.08,
            subsample=0.8,
            colsample_bytree=0.8,
            n_jobs=-1,
            random_state=42,
            eval_metric="mlogloss",
            verbosity=0,
        )
        model.fit(X_tr_s, y_train,
                  eval_set=[(X_te_s, y_test)],
                  verbose=False)

        preds    = model.predict(X_te_s)
        acc      = float(accuracy_score(y_test, preds))
        report   = classification_report(
            y_test, preds,
            labels=[0,1,2,3],
            target_names=["TRENDING_UP","TRENDING_DOWN","RANGING","VOLATILE"],
            output_dict=True, zero_division=0
        )

        # Per-regime win rates on TEST data
        regime_acc = {}
        for rid, rname in enumerate(["TRENDING_UP","TRENDING_DOWN","RANGING","VOLATILE"]):
            mask = y_test == rid
            if mask.sum() > 5:
                regime_acc[rname] = float(accuracy_score(y_test[mask], preds[mask]))
            else:
                regime_acc[rname] = None

        windows.append({
            "fold":       fold + 1,
            "train_rows": len(train_idx),
            "test_rows":  len(test_idx),
            "accuracy":   round(acc, 4),
            "regime_acc": regime_acc,
            "report":     report,
        })
        log.info(f"  {symbol} fold {fold+1}/{n_splits}: acc={acc:.1%}")

    accs = [w["accuracy"] for w in windows]
    return {
        "symbol":         symbol,
        "n_folds":        n_splits,
        "windows":        windows,
        "mean_accuracy":  round(float(np.mean(accs)), 4),
        "std_accuracy":   round(float(np.std(accs)), 4),
        "min_accuracy":   round(float(np.min(accs)), 4),
        "max_accuracy":   round(float(np.max(accs)), 4),
        "degradation":    round(float(accs[-1] - accs[0]), 4),  # negative = degrading
        "total_rows":     len(df_features),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 4. RESULTS PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════

def _save_to_sqlite(results: list):
    """Write backtest results to local SQLite."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_backtest_results (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       TEXT,
            symbol       TEXT,
            dataset      TEXT,
            rows         INTEGER,
            mean_acc     REAL,
            std_acc      REAL,
            min_acc      REAL,
            max_acc      REAL,
            degradation  REAL,
            details_json TEXT,
            created_at   TEXT
        )
    """)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    for r in results:
        conn.execute("""
            INSERT INTO ml_backtest_results
              (run_id, symbol, dataset, rows, mean_acc, std_acc, min_acc,
               max_acc, degradation, details_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            run_id, r["symbol"], r.get("dataset",""),
            r["total_rows"], r["mean_accuracy"], r["std_accuracy"],
            r["min_accuracy"], r["max_accuracy"], r["degradation"],
            json.dumps(r), datetime.now(timezone.utc).isoformat()
        ))
    conn.commit()
    conn.close()
    log.info(f"Results saved to SQLite ({len(results)} rows)")


def _push_to_supabase(results: list):
    """Push results to Supabase for live dashboard."""
    try:
        from supabase import create_client
        url  = os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
        key  = os.environ.get("SUPABASE_SERVICE_ROLE_KEY",
               os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY", ""))
        if not url or not key or "YOUR_PROJECT" in url:
            log.warning("Supabase env vars not set — skipping push")
            return

        sb   = create_client(url, key)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        rows = [{
            "run_id":      run_id,
            "symbol":      r["symbol"],
            "dataset":     r.get("dataset",""),
            "rows":        r["total_rows"],
            "mean_acc":    r["mean_accuracy"],
            "std_acc":     r["std_accuracy"],
            "min_acc":     r["min_accuracy"],
            "max_acc":     r["max_accuracy"],
            "degradation": r["degradation"],
            "details":     r,
            "created_at":  datetime.now(timezone.utc).isoformat(),
        } for r in results]
        sb.table("ml_backtest_results").insert(rows).execute()
        log.info(f"Results pushed to Supabase ({len(rows)} rows)")
    except Exception as e:
        log.warning(f"Supabase push failed (non-fatal): {e}")


def _notify_telegram(results: list, duration_min: float):
    """Send Telegram summary when backtest completes."""
    try:
        sys.path.insert(0, str(CRAVE_ROOT))
        from interfaces.telegram_interface import tg
        lines = [f"🤖 <b>ML Backtest Complete</b> ({duration_min:.0f} min)\n"]
        for r in results:
            ok   = "✅" if r["mean_accuracy"] >= 0.65 else "⚠️" if r["mean_accuracy"] >= 0.55 else "❌"
            deg  = "📉" if r["degradation"] < -0.05 else ""
            lines.append(
                f"{ok} <b>{r['symbol']}</b>: {r['mean_accuracy']:.1%} acc "
                f"(±{r['std_accuracy']:.1%}) {deg}"
            )
        lines.append(f"\nResults live on dashboard 🖥")
        tg.send("\n".join(lines))
    except Exception as e:
        log.debug(f"Telegram notify failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# 5. MAIN RUNNER
# ═══════════════════════════════════════════════════════════════════════════

INSTRUMENTS = [
    # (symbol for download, display_name, downloader, kwargs)
    ("BTCUSDT",  "BTC/USDT",  "binance",  {"interval":"1h","years":4}),
    ("ETHUSDT",  "ETH/USDT",  "binance",  {"interval":"1h","years":3}),
    ("GC=F",     "Gold",      "yfinance", {"period":"2y","interval":"1h"}),
    ("EURUSD=X", "EUR/USD",   "yfinance", {"period":"2y","interval":"1h"}),
    ("^NSEI",    "NIFTY50",   "yfinance", {"period":"max","interval":"1d"}),
    ("SPY",      "SPY/S&P",   "yfinance", {"period":"max","interval":"1d"}),
]


def run_all(background: bool = False):
    """
    Main entry point. Downloads datasets, computes features,
    runs walk-forward tests, saves results.
    background=True: runs in a daemon thread and returns immediately.
    """
    if background:
        t = threading.Thread(target=_run_blocking, daemon=True, name="crave-backtest")
        t.start()
        log.info("Backtest started in background thread — results posted when done")
        return t
    else:
        _run_blocking()


def _run_blocking():
    start_ts  = time.time()
    all_results = []
    n = len(INSTRUMENTS)

    _write_status("running", 2, "Starting ML backtest pipeline…")

    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        _write_status("error", 0, "Missing numpy/pandas — run: pip install numpy pandas")
        return

    try:
        from xgboost import XGBClassifier
    except ImportError:
        _write_status("error", 0, "Missing xgboost — run: pip install xgboost")
        return

    for idx, (sym, display, downloader, kwargs) in enumerate(INSTRUMENTS):
        pct_base = int((idx / n) * 85)
        _write_status("running", pct_base + 5,
                      f"[{idx+1}/{n}] Downloading {display}…")

        # Download
        try:
            if downloader == "binance":
                df = download_binance_public(sym, **kwargs)
            else:
                df = download_yfinance(sym, **kwargs)

            if df is None or len(df) < 500:
                log.warning(f"  Skipping {display}: insufficient data")
                continue
        except Exception as e:
            log.warning(f"  Download failed for {display}: {e}")
            continue

        _write_status("running", pct_base + 10,
                      f"[{idx+1}/{n}] Computing features for {display}…")

        # Feature extraction
        try:
            df_feat = _compute_features_on_df(df, sym)
            if len(df_feat) < 200:
                log.warning(f"  {display}: too few feature rows ({len(df_feat)})")
                continue
            log.info(f"  {display}: {len(df_feat)} feature rows extracted")
        except Exception as e:
            log.error(f"  Feature extraction failed for {display}: {e}\n{traceback.format_exc()}")
            continue

        _write_status("running", pct_base + 20,
                      f"[{idx+1}/{n}] Running walk-forward backtest for {display}…")

        # Walk-forward test
        try:
            result = walk_forward_test(df_feat, display, n_splits=6)
            result["dataset"] = downloader
            result["symbol"]  = display
            all_results.append(result)
            log.info(
                f"  {display}: mean_acc={result['mean_accuracy']:.1%} "
                f"deg={result['degradation']:+.3f}"
            )
        except Exception as e:
            log.error(f"  Backtest failed for {display}: {e}\n{traceback.format_exc()}")
            continue

    if not all_results:
        _write_status("error", 100, "All instruments failed — check logs")
        return

    _write_status("running", 88, "Saving results to database…")

    # Save
    try:
        _save_to_sqlite(all_results)
    except Exception as e:
        log.error(f"SQLite save failed: {e}")

    _write_status("running", 93, "Pushing results to dashboard (Supabase)…")

    try:
        _push_to_supabase(all_results)
    except Exception as e:
        log.warning(f"Supabase push failed: {e}")

    duration_min = (time.time() - start_ts) / 60
    _write_status("done", 100,
                  f"Complete — {len(all_results)} instruments tested in {duration_min:.1f} min",
                  results=all_results)

    try:
        _notify_telegram(all_results, duration_min)
    except Exception:
        pass

    log.info(f"\n{'='*60}")
    log.info(f"BACKTEST COMPLETE — {len(all_results)} instruments")
    log.info(f"Duration: {duration_min:.1f} min")
    for r in all_results:
        status = "✅ PASS" if r["mean_accuracy"] >= 0.65 else "⚠️  MARGINAL" if r["mean_accuracy"] >= 0.55 else "❌ FAIL"
        log.info(f"  {status}  {r['symbol']:12s}  acc={r['mean_accuracy']:.1%} ±{r['std_accuracy']:.1%}  deg={r['degradation']:+.3f}  rows={r['total_rows']}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    # Check for --background flag
    if "--background" in sys.argv:
        t = run_all(background=True)
        log.info("Backtest thread started. This process will stay alive until complete.")
        t.join()   # keep process alive until thread finishes
    else:
        run_all(background=False)
