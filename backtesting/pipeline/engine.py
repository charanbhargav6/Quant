"""
Causal, cost-aware backtest engine.

Differences from the legacy harness (backtesting/verify_hybrid_backtest.py) — deliberate:
  1. NO LOOK-AHEAD in gates: 1H/4H/Daily bars are visible only after they CLOSE (legacy resamples the whole
     dataset and slices on bar-OPEN time, so the in-progress bar leaks future prices; proven in the report).
  2. Entry at the OPEN of the bar after the signal bar (legacy entered at the signal bar's close).
  3. Costs from the REAL per-bar spread (+ slippage fraction + optional commission), charged in R per trade.
  4. Stops that gap are filled at the gap price, not at the stop price.
  5. Position cap (default 1 open trade/symbol). Legacy allowed up to 23 concurrent trades at full risk.
  6. Trades whose friction exceeds MAX_COST_R of one R are skipped (untradeable in practice).
Signals are produced by Strategy.generate() once per parameter set and cached.
"""
from dataclasses import dataclass, field, replace
import numpy as np
import pandas as pd
from . import settings as S
from .strategies import Strategy, Signals

try:
    from config.config import PARTIAL_BOOKING, get_asset_params, get_effective_min_confidence
except Exception:                                   # standalone use
    PARTIAL_BOOKING = [{"r_level": 1.0, "close_pct": 25, "sl_move_to": "-0.3R"},
                       {"r_level": 2.0, "close_pct": 25, "sl_move_to": "breakeven"},
                       {"r_level": 3.0, "close_pct": 25, "sl_move_to": "+1R"},
                       {"r_level": 4.0, "close_pct": 25, "sl_move_to": "+2R"}]
    get_asset_params = lambda t: {"sl_mult": 1.5, "min_grade": "B+", "label": "default"}
    get_effective_min_confidence = lambda t: 40.0

def _ns(series) -> np.ndarray:
    return series.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy("datetime64[ns]").astype("int64")

def _parse_sl(tok: str) -> float:
    if tok == "breakeven": return 0.0
    return float(tok.replace("R", ""))

@dataclass
class SimConfig:
    sl_mult: float = 1.5
    min_confidence: float = 40.0
    tp_scale: float = 1.0
    min_grade_rank: int = 1
    risk_per_trade: float = S.RISK_PER_TRADE
    enforce_kill_zones: bool = S.ENFORCE_KILL_ZONES
    use_mtf: bool = S.USE_MTF_GATE
    use_bias: bool = S.USE_BIAS_GATE
    lookahead: int = S.LOOKAHEAD_BARS
    max_open: int = S.MAX_OPEN_TRADES
    apply_costs: bool = True
    cost_mult: float = 1.0
    slippage_frac: float = S.SLIPPAGE_SPREAD_FRAC
    commission_px: float = 0.0            # price units, derived from contract size
    max_cost_r: float = S.MAX_COST_R
    gap_fills: bool = True

UNIVERSAL = ("sl_mult", "min_confidence", "tp_scale")

# ── exit model (same semantics as simulate_partial_booking_exit + gap fills) ──
def simulate_exit(o, h, l, c, d, entry, r, stages, gap_fills=True):
    remaining, realized, sl_r = 1.0, 0.0, -1.0
    trig = [False] * len(stages); outcome, used = "timeout", len(h)
    for k in range(len(h)):
        stop = entry + d * sl_r * r
        stopped = (l[k] <= stop) if d > 0 else (h[k] >= stop)
        if stopped:
            gapped = (o[k] <= stop) if d > 0 else (o[k] >= stop)
            fill = o[k] if (gap_fills and gapped) else stop
            realized += remaining * (d * (fill - entry) / r)
            remaining = 0.0; outcome = "sl" if sl_r <= -0.999 else "trailing_sl"; used = k + 1
            break
        for idx, (lv, frac, slt) in enumerate(stages):
            if trig[idx]: continue
            tgt = entry + d * lv * r
            if (h[k] >= tgt) if d > 0 else (l[k] <= tgt):
                trig[idx] = True; realized += frac * lv; remaining -= frac; sl_r = slt
        if remaining <= 1e-9:
            outcome = "full_target"; used = k + 1
            break
    if remaining > 1e-9 and outcome == "timeout":
        realized += remaining * (d * (c[-1] - entry) / r)
    return realized, outcome, used, sum(trig)

def regime_labels(df: pd.DataFrame):
    """Causal 4-way regime: trend/range x high/low vol (rolling medians over ~20 days). -1 = warm-up."""
    c = df["close"]
    ret = np.log(c).diff()
    vol = ret.rolling(96).std()
    hv = vol > vol.rolling(1920, min_periods=480).median()
    er = (c - c.shift(96)).abs() / c.diff().abs().rolling(96).sum()
    tr = er > er.rolling(1920, min_periods=480).median()
    ok = vol.rolling(1920, min_periods=480).median().notna() & er.rolling(1920, min_periods=480).median().notna()
    lab = (tr.astype(int) * 2 + hv.astype(int)).where(ok, -1).to_numpy().astype(int)
    return lab, ["range/low-vol", "range/high-vol", "trend/low-vol", "trend/high-vol"]

class BacktestContext:
    def __init__(self, df: pd.DataFrame, symbol: str, strategy: Strategy, contract_size=None):
        self.df = df.reset_index(drop=True); self.symbol = symbol; self.strategy = strategy
        d = self.df; self.n = len(d)
        self.t = _ns(d["time"]); self.O = d["open"].to_numpy(float); self.H = d["high"].to_numpy(float)
        self.L = d["low"].to_numpy(float); self.C = d["close"].to_numpy(float)
        self.SP = (d["spread"].to_numpy(float) if "spread" in d else np.zeros(self.n))
        self.bar_ns = int(np.median(np.diff(self.t))) if self.n > 2 else S.BAR_MINUTES * 60 * 10**9
        tr = np.maximum.reduce([self.H - self.L, np.abs(self.H - np.r_[np.nan, self.C[:-1]]),
                                np.abs(self.L - np.r_[np.nan, self.C[:-1]])])
        self.ATR = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
        try: ap = get_asset_params(symbol)
        except Exception: ap = {"sl_mult": 1.5, "min_grade": "B+"}
        self.asset = ap
        try: self.eff_min_conf = float(get_effective_min_confidence(symbol))
        except Exception: self.eff_min_conf = 40.0
        self.base_cfg = SimConfig(sl_mult=float(ap.get("sl_mult", 1.5)), min_confidence=self.eff_min_conf,
                                  min_grade_rank=max(1, {"A+": 3, "A": 2, "B+": 1}.get(ap.get("min_grade", "B+"), 1)),
                                  commission_px=(S.COMMISSION_PER_LOT_RT / contract_size) if contract_size else 0.0)
        self.stages = [(float(s["r_level"]), s["close_pct"] / 100.0, _parse_sl(s["sl_move_to"]))
                       for s in sorted(PARTIAL_BOOKING, key=lambda s: s["r_level"])]
        self._sig_cache = {}
        self._build_htf(); self._kz = None
        self.regime, self.regime_names = regime_labels(self.df)

    # ── causal higher-timeframe frames ──────────────────────────────────────
    def _build_htf(self):
        x = self.df.set_index("time")
        agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        self.htf = {}
        for name, per in (("1h", "1h"), ("4h", "4h"), ("1d", "1D")):
            f = x.resample(per).agg(agg).dropna().reset_index()
            f["avail_ns"] = _ns(f["time"]) + int(pd.Timedelta(per).value)       # visible only once CLOSED
            self.htf[name] = f
        d = self.htf["1d"]
        self._d_close = d["close"].to_numpy(float)
        self._d_sma = pd.Series(self._d_close).rolling(20).mean().to_numpy()
        self._struct_cache = {}

    def _k(self, name, T): return int(np.searchsorted(self.htf[name]["avail_ns"].to_numpy(), T, side="right"))

    def _struct(self, name, k):
        key = (name, k)
        if key not in self._struct_cache:
            from backtesting.verify_hybrid_backtest import _get_struct_dir
            self._struct_cache[key] = _get_struct_dir(self.htf[name].iloc[max(0, k - 60):k], name)[0]
        return self._struct_cache[key]

    def mtf_ok(self, direction, T):
        want = "bullish" if direction > 0 else "bearish"
        for nm in ("1h", "4h"):
            dr = self._struct(nm, self._k(nm, T))
            if dr != "unknown" and dr != want: return False
        return True

    def bias_ok(self, direction, T):
        k = self._k("1d", T)
        if k < 21 or np.isnan(self._d_sma[k - 1]): return True              # same skip rule as legacy
        bias = 1 if self._d_close[k - 1] > self._d_sma[k - 1] else -1
        return bias == direction

    def kz_ok(self, j):
        if self._kz is None:
            from backtesting.verify_hybrid_backtest import _in_kill_zone
            self._kz = np.array([_in_kill_zone(pd.Timestamp(v, tz="UTC"), self.symbol) for v in self.t])
        return bool(self._kz[j])

    # ── signals ─────────────────────────────────────────────────────────────
    def signals(self, params=None) -> Signals:
        params = params or {}
        key = tuple(sorted((k, v) for k, v in params.items() if k not in UNIVERSAL))
        if key not in self._sig_cache:
            if len(self._sig_cache) > 400: self._sig_cache.clear()
            self._sig_cache[key] = self.strategy.with_params(**dict(key)).generate(self.df)
        return self._sig_cache[key]

    def config(self, **over) -> SimConfig:
        return replace(self.base_cfg, **{k: v for k, v in over.items() if v is not None})

    # ── the simulation ──────────────────────────────────────────────────────
    def run(self, params=None, start=0, end=None, on_progress=None, cancel=None, **over):
        params = params or {}
        cfg = self.config(**{**{k: params[k] for k in UNIVERSAL if k in params}, **over})
        sig = self.signals(params)
        end = self.n - 6 if end is None else min(end, self.n - 6)
        stages = [(lv * cfg.tp_scale, fr, s * cfg.tp_scale) for lv, fr, s in self.stages] if cfg.tp_scale != 1.0 else self.stages
        idxs = np.flatnonzero(sig.direction != 0); idxs = idxs[(idxs >= max(start, 1)) & (idxs < end)]
        trades, open_exits = [], []
        skipped = dict(kill_zone=0, mtf=0, bias=0, confidence=0, grade=0, position_open=0, cost=0, data_gap=0, bad_atr=0)
        total = max(1, len(idxs)); cum = 0.0
        for q, j in enumerate(idxs):
            if cancel and q % 200 == 0: cancel()
            d = int(sig.direction[j]); e = j + 1
            T = self.t[j] + self.bar_ns
            if self.t[e] - self.t[j] > 2 * self.bar_ns: skipped["data_gap"] += 1; continue
            if cfg.enforce_kill_zones and not self.kz_ok(j): skipped["kill_zone"] += 1; continue
            if cfg.use_mtf and not self.mtf_ok(d, T): skipped["mtf"] += 1; continue
            if cfg.use_bias and not self.bias_ok(d, T): skipped["bias"] += 1; continue
            if sig.conf[j] < cfg.min_confidence: skipped["confidence"] += 1; continue
            if sig.grade[j] < cfg.min_grade_rank: skipped["grade"] += 1; continue
            open_exits = [x for x in open_exits if x >= e]
            if len(open_exits) >= cfg.max_open: skipped["position_open"] += 1; continue
            rd = sig.risk_dist[j]
            r = float(rd) if np.isfinite(rd) else float(self.ATR[j] * cfg.sl_mult)
            if not np.isfinite(r) or r <= 0: skipped["bad_atr"] += 1; continue
            entry = self.O[e]
            fr = min(e + cfg.lookahead, self.n)
            frict = (self.SP[e] * (1 + cfg.slippage_frac) + cfg.commission_px) if cfg.apply_costs else 0.0
            if frict / r > cfg.max_cost_r: skipped["cost"] += 1; continue
            gross, outcome, used, nst = simulate_exit(self.O[e:fr], self.H[e:fr], self.L[e:fr], self.C[e:fr],
                                                      d, entry, r, stages, cfg.gap_fills)
            cost_r = cfg.cost_mult * frict / r
            open_exits.append(e + used - 1)
            net = gross - cost_r; cum += net
            trades.append({"i": int(j), "t": int(self.t[e]), "x": int(self.t[min(e + used - 1, self.n - 1)]),
                           "d": d, "r": round(net, 4), "rg": round(gross, 4), "c": round(cost_r, 4),
                           "o": outcome, "cf": float(sig.conf[j]), "g": int(sig.grade[j]),
                           "rg_": int(self.regime[j]), "bars": int(used)})
            if on_progress and (q % max(1, total // 40) == 0):
                on_progress(q / total, len(trades), cum)
        return {"trades": trades, "skipped": skipped, "cfg": cfg}

# ── metrics ──────────────────────────────────────────────────────────────────
def _iso(ns): return pd.Timestamp(int(ns), tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")

def summarize(trades, risk=S.RISK_PER_TRADE, curve_points=200):
    n = len(trades)
    if n == 0:
        return {"trades": 0, "expectancy_r": 0.0, "total_r": 0.0, "profit_factor": 0.0, "win_rate": 0.0,
                "return_pct": 0.0, "max_dd_pct": 0.0, "max_dd_r": 0.0, "sharpe": 0.0, "t_stat": 0.0,
                "avg_cost_r": 0.0, "expectancy_gross_r": 0.0, "equity": []}
    r = np.array([t["r"] for t in trades]); rg = np.array([t["rg"] for t in trades])
    order = np.argsort([t["x"] for t in trades], kind="stable"); ro = r[order]
    cum = np.cumsum(ro); peak = np.maximum.accumulate(np.maximum(cum, 0)); dd_r = float((peak - cum).max())
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    sd = r.std(ddof=1) if n > 1 else 0.0
    xs = pd.to_datetime([trades[i]["x"] for i in order], unit="ns", utc=True).normalize()
    daily = pd.Series(ro * risk, index=xs).groupby(level=0).sum()
    if len(daily) > 1:
        full = daily.reindex(pd.date_range(daily.index.min(), daily.index.max(), freq="D", tz="UTC"), fill_value=0.0)
        full = full[full.index.dayofweek < 5] if len(full) > 5 else full
        sharpe = float(full.mean() / full.std(ddof=1) * np.sqrt(252)) if full.std(ddof=1) > 0 else 0.0
    else:
        sharpe = 0.0
    step = max(1, len(cum) // curve_points)
    curve = [[_iso(trades[order[i]]["x"]), round(float(cum[i]), 3)] for i in range(0, len(cum), step)]
    if curve[-1][1] != round(float(cum[-1]), 3): curve.append([_iso(trades[order[-1]]["x"]), round(float(cum[-1]), 3)])
    streak = cur = 0
    for v in ro:
        cur = cur + 1 if v <= 0 else 0; streak = max(streak, cur)
    return {"trades": n, "win_rate": round(float((r > 0).mean() * 100), 2), "expectancy_r": round(float(r.mean()), 4),
            "expectancy_gross_r": round(float(rg.mean()), 4), "avg_cost_r": round(float(np.mean([t["c"] for t in trades])), 4),
            "total_r": round(float(r.sum()), 3), "profit_factor": round(float(gp / gl), 3) if gl > 0 else 999.0,
            "return_pct": round(float(r.sum() * risk * 100), 2), "max_dd_r": round(dd_r, 3),
            "max_dd_pct": round(dd_r * risk * 100, 2), "sharpe": round(sharpe, 2),
            "t_stat": round(float(r.mean() / (sd / np.sqrt(n))), 2) if sd > 0 else 0.0,
            "avg_win_r": round(float(r[r > 0].mean()), 3) if (r > 0).any() else 0.0,
            "avg_loss_r": round(float(r[r <= 0].mean()), 3) if (r <= 0).any() else 0.0,
            "longest_losing_streak": int(streak), "equity": curve}
