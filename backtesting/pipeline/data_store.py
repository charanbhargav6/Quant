"""
On-disk MT5 history cache (one file per UTC day of M1 bars) + M15 builder + quality report.

M1 columns (all UTC, BID-based like MT5 bars):
  time, open, high, low, close, tick_count, spread_mean, spread_max (PRICE units), real_volume, source
"""
import json, os, re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from . import settings as S

try:
    import pyarrow  # noqa: F401
    _PARQUET = True
except Exception:       # pragma: no cover
    _PARQUET = False

M1_COLS = ["time", "open", "high", "low", "close", "tick_count",
           "spread_mean", "spread_max", "real_volume", "source"]

def safe_name(sym: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sym)

def is_crypto(sym: str) -> bool:
    s = sym.upper()
    return s.endswith("USDT") or s.startswith(("BTC", "ETH", "SOL", "XRP", "DOGE")) or s.endswith("-USD")

def sym_dir(sym: str, cache_dir: Path = None) -> Path:
    d = Path(cache_dir or S.CACHE_DIR) / safe_name(sym)
    (d / "M1").mkdir(parents=True, exist_ok=True)
    return d

def _atomic_write(path: Path, writer):
    tmp = path.with_suffix(path.suffix + ".tmp")
    writer(tmp)
    os.replace(tmp, path)

def write_day(sym: str, day: date, df: pd.DataFrame, cache_dir=None):
    d = sym_dir(sym, cache_dir) / "M1"
    if _PARQUET:
        _atomic_write(d / f"{day.isoformat()}.parquet", lambda p: df.to_parquet(p, index=False))
    else:
        _atomic_write(d / f"{day.isoformat()}.csv.gz", lambda p: df.to_csv(p, index=False, compression="gzip"))

def _read_file(p: Path) -> pd.DataFrame:
    if p.suffix == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p, compression="gzip")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df

def read_days(sym: str, start: date, end: date, cache_dir=None) -> pd.DataFrame:
    d = sym_dir(sym, cache_dir) / "M1"
    parts, cur = [], start
    while cur <= end:
        for ext in ("parquet", "csv.gz"):
            p = d / f"{cur.isoformat()}.{ext}"
            if p.exists():
                parts.append(_read_file(p)); break
        cur += timedelta(days=1)
    if not parts:
        return pd.DataFrame(columns=M1_COLS)
    out = pd.concat(parts, ignore_index=True).drop_duplicates("time").sort_values("time")
    return out.reset_index(drop=True)

# ── manifest (per-symbol meta.json) ──────────────────────────────────────────
def load_meta(sym: str, cache_dir=None) -> dict:
    p = sym_dir(sym, cache_dir) / "meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"symbol": sym, "days": {}}

def save_meta(sym: str, meta: dict, cache_dir=None):
    p = sym_dir(sym, cache_dir) / "meta.json"
    _atomic_write(p, lambda t: t.write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8"))

# ── M15 bars ─────────────────────────────────────────────────────────────────
def build_bars(m1: pd.DataFrame, minutes: int = None) -> pd.DataFrame:
    """M1 -> N-minute bars. volume = tick count (MT5 'volume' for FX/CFD is tick volume)."""
    minutes = minutes or S.BAR_MINUTES
    if m1.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume",
                                     "spread", "spread_max", "real_volume"])
    x = m1.set_index("time")
    w = x["tick_count"].astype(float)
    g = x.resample(f"{minutes}min", closed="left", label="left")
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["tick_count"].sum(),
        "spread_max": g["spread_max"].max(), "real_volume": g["real_volume"].sum(),
    })
    sw = (x["spread_mean"].fillna(0) * w).resample(f"{minutes}min", closed="left", label="left").sum()
    ws = w.resample(f"{minutes}min", closed="left", label="left").sum()
    out["spread"] = (sw / ws.replace(0, np.nan)).fillna(x["spread_mean"].resample(f"{minutes}min").mean())
    out = out.dropna(subset=["open", "high", "low", "close"]).reset_index()
    out["spread"] = out["spread"].fillna(out["spread"].median() if out["spread"].notna().any() else 0.0)
    return out

def quality_report(bars: pd.DataFrame, sym: str, start: date, end: date) -> dict:
    """Honest data-quality summary. `coverage` = share of expected trading days with a usable session."""
    crypto = is_crypto(sym)
    exp_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if not crypto:
        exp_days = [d for d in exp_days if d.weekday() < 5]
    if bars.empty:
        return {"ok": False, "coverage": 0.0, "bars": 0, "expected_days": len(exp_days), "reason": "no data"}
    per_day = bars.groupby(bars["time"].dt.date).size()
    min_bars = 24 if crypto else 12
    have = {d for d, n in per_day.items() if n >= min_bars}
    cov = len([d for d in exp_days if d in have]) / max(1, len(exp_days))
    t = bars["time"]
    gaps = t.diff().dt.total_seconds().div(60)
    # a gap is suspicious if it is >2h and NOT a weekend/holiday closure (fri 21:00 -> sun 21:00 UTC)
    wk = (t.dt.dayofweek.isin([4, 5, 6]) | (t.shift(1).dt.dayofweek == 4))
    bad = gaps[(gaps > 120) & ~wk] if not crypto else gaps[gaps > 120]
    sp = bars["spread"]
    med = float(sp[sp > 0].median()) if (sp > 0).any() else 0.0
    vol_kind = "real" if bars["real_volume"].sum() > 0 else "tick_count"
    return {
        "ok": cov >= S.MIN_COVERAGE and len(bars) > 600,
        "coverage": round(float(cov), 4), "bars": int(len(bars)),
        "first": t.iloc[0].isoformat(), "last": t.iloc[-1].isoformat(),
        "expected_days": len(exp_days), "days_with_data": len(have & set(exp_days)),
        "suspicious_gaps": int(len(bad)),
        "dup_times": int(t.duplicated().sum()),
        "zero_spread_pct": round(float((sp <= 0).mean() * 100), 2),
        "spread_median_price": med,
        "spread_outliers": int((sp > 20 * med).sum()) if med > 0 else 0,
        "volume_kind": vol_kind,
        "ohlc_violations": int(((bars["high"] < bars[["open", "close"]].max(axis=1) - 1e-12) |
                                (bars["low"] > bars[["open", "close"]].min(axis=1) + 1e-12)).sum()),
    }

def load_bars(sym: str, days: int, end: date = None, cache_dir=None):
    """Return (bars15, quality, meta) for the last `days` days ending at `end` (default today UTC)."""
    end = end or datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    m1 = read_days(sym, start, end, cache_dir)
    bars = build_bars(m1)
    return bars, quality_report(bars, sym, start, end), load_meta(sym, cache_dir)
