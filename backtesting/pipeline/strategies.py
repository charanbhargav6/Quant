"""
Strategy layer for the pipeline.

Contract (what a NEW generated strategy must provide to be backtestable):
    class MyStrategy(Strategy):
        key = "my_strategy"
        PARAMS = (ParamSpec("period", 14, 5, 40, 1, "int"), ...)      # tunable parameters (sensitivity/WFO nudge these)
        def generate(self, df) -> Signals                            # CAUSAL: signal[j] may use bars <= j only
    register_strategy(MyStrategy)
A signal at bar j is decided at the CLOSE of bar j; the engine enters at the OPEN of bar j+1.

Shipped:
  trend_pa       parameterized twin of engines.gold_strategy (verified bar-for-bar equal at defaults)
  vol_breakout   parameterized twin of engines.volatility_breakout_engine (verified equal at defaults)
  hybrid / structure / orderflow   thin wrappers over strategy_adapters.* (no exposed params)
"""
import json
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
from . import settings as S

GRADE_RANK = {"A+": 3, "A": 2, "B+": 1}

@dataclass(frozen=True)
class ParamSpec:
    name: str
    default: float
    lo: float
    hi: float
    step: float
    kind: str = "float"
    def at(self, base, k):
        v = min(self.hi, max(self.lo, base + k * self.step))
        return int(round(v)) if self.kind == "int" else round(float(v), 6)

@dataclass
class Signals:
    direction: np.ndarray            # +1 buy, -1 sell, 0 none
    conf: np.ndarray
    grade: np.ndarray                # rank 3/2/1, 0 = ungraded/B
    risk_dist: np.ndarray            # price units, NaN => engine uses ATR*sl_mult

    @staticmethod
    def empty(n):
        return Signals(np.zeros(n, np.int8), np.zeros(n), np.zeros(n, np.int8), np.full(n, np.nan))

class StrategyNotBacktestable(ValueError): ...

class Strategy:
    key = "base"; label = "base"; PARAMS: tuple = ()
    def __init__(self, params=None, **meta):
        self.params = {p.name: p.default for p in self.PARAMS}
        self.params.update(params or {})
        self.meta = meta
    def with_params(self, **kw):
        return type(self)(params={**self.params, **kw}, **self.meta)
    def spec(self, name):
        return next(p for p in self.PARAMS if p.name == name)
    def generate(self, df) -> Signals:
        raise NotImplementedError

_REG = {}
def register_strategy(cls):
    _REG[cls.key] = cls
    return cls

def _arr(s): return np.asarray(s, dtype="float64")

# ═════════════════════════════════════════════════════════════════════════════
@register_strategy
class TrendPAParam(Strategy):
    key = "trend_pa"; label = "Trend / Price-Action (parameterized)"
    PARAMS = (
        ParamSpec("ema_fast", 9, 5, 15, 1, "int"), ParamSpec("ema_pull", 21, 13, 34, 2, "int"),
        ParamSpec("ema_mid", 50, 34, 89, 5, "int"), ParamSpec("ema_slow", 200, 120, 300, 20, "int"),
        ParamSpec("atr_period", 14, 8, 28, 2, "int"), ParamSpec("rsi_period", 14, 8, 28, 2, "int"),
        ParamSpec("adx_period", 14, 8, 28, 2, "int"), ParamSpec("kc_period", 20, 10, 40, 2, "int"),
        ParamSpec("kc_mult", 2.0, 1.0, 3.5, 0.25), ParamSpec("adx_min", 18, 10, 30, 2),
        ParamSpec("adx_strong", 25, 18, 35, 2.5), ParamSpec("adx_vstrong", 35, 28, 45, 2.5),
        ParamSpec("pb_lookback", 5, 2, 10, 1, "int"), ParamSpec("pb_dist", 1.5, 0.8, 2.5, 0.2),
        ParamSpec("bounce_tol", 0.002, 0.0005, 0.005, 0.0005),
        ParamSpec("rsi_buy_lo", 35, 25, 45, 3), ParamSpec("rsi_buy_hi", 75, 65, 85, 3),
        ParamSpec("rsi_sell_lo", 25, 15, 35, 3), ParamSpec("rsi_sell_hi", 65, 55, 75, 3),
        ParamSpec("body_strong", 0.6, 0.4, 0.8, 0.05), ParamSpec("vol_strong", 1.2, 0.9, 1.8, 0.1),
    )
    WARMUP = 200

    def generate(self, df) -> Signals:
        from engines import gold_strategy as G           # reuse the exact indicator math
        p = self.params; n = len(df)
        c, o, h, l = (df[k] for k in ("close", "open", "high", "low"))
        ema_f = G._ema(c, int(p["ema_fast"])); ema_p = G._ema(c, int(p["ema_pull"]))
        ema_m = G._ema(c, int(p["ema_mid"]));  ema_s = G._ema(c, int(p["ema_slow"]))
        atr = G._wilder_atr(df, int(p["atr_period"])); rsi = G._rsi(c, int(p["rsi_period"]))
        adx = G._adx(df, int(p["adx_period"]))
        kc_mid, kc_up, kc_lo = G._keltner_channel(df, int(p["kc_period"]), float(p["kc_mult"]), int(p["atr_period"]))
        sk, sd = G._stochastic_rsi(rsi)
        dist = (c - ema_p) / atr.replace(0, np.nan)
        body = (c - o).abs() / (h - l).replace(0, np.nan)
        if "volume" in df.columns and df["volume"].sum() > 0:
            vr = df["volume"] / df["volume"].rolling(20).mean().replace(0, np.nan)
        else:
            vr = pd.Series(1.0, index=df.index)
        C, EP, EF, EM, ES = map(_arr, (c, ema_p, ema_f, ema_m, ema_s))
        A, R, X = _arr(atr), _arr(rsi), _arr(adx)
        KM, KU, KL = _arr(kc_mid), _arr(kc_up), _arr(kc_lo)
        SK, SD, D, B, V = _arr(sk), _arr(sd), _arr(dist), _arr(body), _arr(vr)
        sh = lambda a: np.concatenate([[np.nan], a[:-1]])
        pC, pEP, pEF, pEP2, pKM = sh(C), sh(EP), sh(EF), sh(EP), sh(KM)
        with np.errstate(invalid="ignore"):
            valid = (np.arange(n) >= self.WARMUP) & ~(np.isnan(EM) | np.isnan(ES) | np.isnan(A) | np.isnan(R) | np.isnan(X)) \
                    & (A != 0) & (X >= p["adx_min"])
            bull = (EM > ES) & (C > ES); bear = (EM < ES) & (C < ES)
            absd = np.abs(C - EP)
            win = int(p["pb_lookback"]) + 1
            pb = pd.Series(absd).rolling(win, min_periods=win).min().to_numpy() < p["pb_dist"] * A
            tol = p["bounce_tol"]
            # BUY
            b_bounce = (C > EP) & (pC <= pEP * (1 + tol)); b_near = (np.abs(D) < 1.5) & (C > EP)
            b_cross = (EF > EP) & (pEF <= pEP2); b_kc = (C > KM) & (pC <= pKM)
            b_ok = pb & (b_bounce | b_near | b_cross | b_kc) & (R >= p["rsi_buy_lo"]) & (R <= p["rsi_buy_hi"])
            # SELL
            s_bounce = (C < EP) & (pC >= pEP * (1 - tol)); s_near = (np.abs(D) < 1.5) & (C < EP)
            s_cross = (EF < EP) & (pEF >= pEP2); s_kc = (C < KM) & (pC >= pKM)
            s_ok = pb & (s_bounce | s_near | s_cross | s_kc) & (R >= p["rsi_sell_lo"]) & (R <= p["rsi_sell_hi"])
            common = 30 + 10 * (X > p["adx_strong"]) + 5 * (X > p["adx_vstrong"]) + 5 * (B > p["body_strong"]) + 5 * (V > p["vol_strong"])
            conf_b = common + np.where(b_bounce, 15, np.where(b_near, 10, 0)) + 10 * b_cross + 10 * b_kc \
                     + 5 * ((SK > SD) & (SK < 80)) + 5 * (C > KU)
            conf_s = common + np.where(s_bounce, 15, np.where(s_near, 10, 0)) + 10 * s_cross + 10 * s_kc \
                     + 5 * ((SK < SD) & (SK > 20)) + 5 * (C < KL)
        buy = valid & bull & b_ok; sell = valid & bear & s_ok & ~buy
        out = Signals.empty(n)
        out.direction[buy] = 1; out.direction[sell] = -1
        conf = np.where(buy, conf_b, np.where(sell, conf_s, 0.0))
        out.conf = np.minimum(conf, 100.0)
        out.grade = np.where(conf >= 70, 3, np.where(conf >= 55, 2, np.where(conf >= 40, 1, 0))).astype(np.int8)
        out.grade[out.direction == 0] = 0
        return out

# ═════════════════════════════════════════════════════════════════════════════
@register_strategy
class VolBreakoutParam(Strategy):
    key = "vol_breakout"; label = "Volatility Breakout (parameterized)"
    PARAMS = (
        ParamSpec("bb_period", 20, 10, 40, 2, "int"), ParamSpec("bb_std", 2.0, 1.0, 3.5, 0.25),
        ParamSpec("atr_period", 14, 8, 28, 2, "int"), ParamSpec("adx_period", 14, 8, 28, 2, "int"),
        ParamSpec("min_adx", 25, 15, 40, 2.5), ParamSpec("min_body_atr", 0.5, 0.2, 1.0, 0.1),
        ParamSpec("stop_atr", 1.2, 0.6, 2.4, 0.2), ParamSpec("rsi_long", 55, 50, 65, 2.5),
        ParamSpec("rsi_short", 45, 35, 50, 2.5),
    )
    WARMUP = 219

    def generate(self, df) -> Signals:
        p = self.params; n = len(df)
        close, open_, high, low = (pd.to_numeric(df[k], errors="coerce") for k in ("close", "open", "high", "low"))
        bb = int(p["bb_period"]); mid = close.rolling(bb).mean(); std = close.rolling(bb).std()
        upper = mid + p["bb_std"] * std; lower = mid - p["bb_std"] * std
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / int(p["atr_period"]), adjust=False).mean()
        pdm = high.diff().clip(lower=0); mdm = (-low.diff()).clip(lower=0)
        m = pdm > mdm; pdm = pdm.where(m, 0.0); mdm = mdm.where(~m, 0.0)
        a = 1 / int(p["adx_period"])
        pdi = 100 * pdm.ewm(alpha=a, adjust=False).mean() / atr.replace(0, np.nan)
        mdi = 100 * mdm.ewm(alpha=a, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
        adx = dx.ewm(alpha=a, adjust=False).mean()
        dlt = close.diff()
        gain = dlt.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-dlt.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        C, O, U, L, A, X, R = map(_arr, (close, open_, upper, lower, atr, adx, rsi))
        sh = lambda v: np.concatenate([[np.nan], v[:-1]])
        pC, pU, pL = sh(C), sh(U), sh(L)
        with np.errstate(invalid="ignore"):
            finite = np.isfinite(C) & np.isfinite(O) & np.isfinite(U) & np.isfinite(L) & np.isfinite(pU) & np.isfinite(pL) \
                     & np.isfinite(A) & np.isfinite(X) & np.isfinite(R)
            body = np.abs(C - O)
            lb = (pC <= pU) & (C > U) & (C > O) & (R >= p["rsi_long"])
            sb = (pC >= pL) & (C < L) & (C < O) & (R <= p["rsi_short"])
            ok = finite & (np.arange(n) >= self.WARMUP) & (X >= p["min_adx"]) & (body >= p["min_body_atr"] * A) & (lb | sb)
            conf = np.minimum(95, np.floor(55 + np.minimum(25, np.maximum(0, X - p["min_adx"]))
                                           + np.minimum(15, np.maximum(0, body / A * 5))))
            rd = np.maximum(p["stop_atr"] * A, (U - L) * 0.25)
        out = Signals.empty(n)
        out.direction[ok & lb] = 1; out.direction[ok & sb & ~lb] = -1
        d = out.direction != 0
        out.conf = np.where(d, conf, 0.0); out.grade = np.where(d, 3, 0).astype(np.int8)
        out.risk_dist = np.where(d, rd, np.nan)
        return out

# ═════════════════════════════════════════════════════════════════════════════
class LegacyAdapterStrategy(Strategy):
    """Wraps strategy_adapters.* unchanged. No exposed internal params -> sensitivity covers universal params only."""
    key = "legacy"; label = "Legacy adapter"
    def __init__(self, params=None, adapter_key="hybrid", symbol="EURUSD=X", **meta):
        super().__init__(params, adapter_key=adapter_key, symbol=symbol, **meta)
        self.adapter_key, self.symbol = adapter_key, symbol
    def generate(self, df) -> Signals:
        from backtesting.backtest_agent import BacktestAgent, _wilder_atr
        from strategy_adapters import STRATEGY_ADAPTERS
        d = df.copy().reset_index(drop=True)
        d["atr"] = _wilder_atr(d, 14)
        d = BacktestAgent()._attach_indicators(d)
        ad = STRATEGY_ADAPTERS[self.adapter_key]()
        extra = ad.prepare(d)
        ctx = {"ticker": self.symbol, "df": extra.get("df", d), **{k: v for k, v in extra.items() if k != "df"}}
        n = len(d); out = Signals.empty(n)
        # range(200, n): include the last bar so the causality guard (which
        # truncates df to j+1 bars, making j == n-1) sees the same signal
        # as the full run. The engine never enters on the final bar anyway.
        for j in range(200, n):
            s = ad.analyze(ctx, j)
            dr = s.get("direction")
            if dr is None: continue
            out.direction[j] = 1 if dr == "buy" else -1
            out.conf[j] = s.get("confidence", 0) or 0
            out.grade[j] = GRADE_RANK.get(s.get("grade"), 0)
        return out

for _k in ("hybrid", "structure", "orderflow"):
    pass

# ═════════════════════════════════════════════════════════════════════════════
REGISTRY_MAP = {                       # strategy_registry id -> (kind, key)
    "orderflow_btc": ("legacy", "orderflow"), "structure_silver": ("legacy", "structure"),
    "trend_pa_gold": ("native", "trend_pa"), "trend_pa_forex": ("native", "trend_pa"),
    "trend_pa_forex_gbp": ("native", "trend_pa"), "trend_pa_forex_jpy": ("native", "trend_pa"),
    "trend_pa_forex_aud": ("native", "trend_pa"), "trend_pa_forex_untested": ("native", "trend_pa"),
    "volatility_breakout_xau": ("native", "vol_breakout"),
}
CANDIDATE_ADAPTERS = {"trend_pa", "vol_breakout", "hybrid", "structure", "orderflow"}

def _registry_def(sid):
    try:
        from core.strategy_registry import get_strategy
        return get_strategy(sid) or {}
    except Exception:
        return {}

def default_symbol(sdef: dict) -> str:
    try:
        from brokers.mt5_agent import SYMBOL_MAP
    except Exception:
        SYMBOL_MAP = {}
    ins = sdef.get("instruments") or []
    for i in ins:
        if i in SYMBOL_MAP: return i
    return ins[0] if ins else "EURUSD=X"

def _load_candidates():
    try:
        return json.loads(S.CANDIDATES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def build(kind, key, symbol, params=None):
    if kind == "native":
        return _REG[key](params=params)
    return LegacyAdapterStrategy(params=params, adapter_key=key, symbol=symbol)

def resolve(ref: str, symbol: str = None):
    """ref: registry id | 'candidate:<id>' | 'adapter:<key>'  ->  (strategy, info)"""
    if ref.startswith("adapter:"):
        key = ref.split(":", 1)[1]
        if key not in CANDIDATE_ADAPTERS: raise StrategyNotBacktestable(f"unknown adapter '{key}'")
        kind = "native" if key in _REG else "legacy"
        return build(kind, key, symbol or "EURUSD=X"), {"name": f"adapter:{key}", "symbol": symbol or "EURUSD=X", "kind": "adapter"}
    if ref.startswith("candidate:") or ref.startswith("strat_"):
        cid = ref.split(":", 1)[-1]
        c = next((x for x in _load_candidates() if x.get("id") == cid), None)
        if not c: raise StrategyNotBacktestable(f"candidate '{cid}' not found")
        ad = c.get("adapter")
        if ad not in CANDIDATE_ADAPTERS:
            raise StrategyNotBacktestable(
                f"candidate '{c.get('name')}' has status '{c.get('status')}' and no executable adapter. "
                "The strategy creator must attach spec['adapter'] (one of "
                f"{sorted(CANDIDATE_ADAPTERS)}) and optional spec['params'] before it can be backtested.")
        sym = symbol or (c.get("instruments") or ["EURUSD=X"])[0]
        kind = "native" if ad in _REG else "legacy"
        return build(kind, ad, sym, c.get("params")), {"name": c.get("name", cid), "symbol": sym, "kind": "candidate",
                                                         "lineage_id": c.get("lineage_id") or c.get("parent_id") or cid, "candidate": c}
    if ref in REGISTRY_MAP:
        kind, key = REGISTRY_MAP[ref]; sdef = _registry_def(ref)
        sym = symbol or default_symbol(sdef)
        return build(kind, key, sym), {"name": sdef.get("name", ref), "symbol": sym, "kind": "registry", "lineage_id": ref,
                                       "live_ready_flag": sdef.get("live_ready")}
    raise StrategyNotBacktestable(f"strategy '{ref}' has no backtest adapter yet (mean_reversion_ranging needs one).")

def list_backtestable():
    out = []
    try:
        from core.strategy_registry import STRATEGY_DEFS
    except Exception:
        STRATEGY_DEFS = []
    for s in STRATEGY_DEFS:
        ok = s["id"] in REGISTRY_MAP
        out.append({"ref": s["id"], "name": s.get("name", s["id"]), "kind": "registry", "backtestable": ok,
                    "symbol": default_symbol(s), "live_ready": s.get("live_ready"),
                    "reason": None if ok else "no adapter"})
    for c in _load_candidates():
        ok = c.get("adapter") in CANDIDATE_ADAPTERS
        out.append({"ref": "candidate:" + str(c.get("id")), "name": c.get("name"), "kind": "candidate", "backtestable": ok,
                    "symbol": (c.get("instruments") or ["EURUSD=X"])[0], "live_ready": False,
                    "reason": None if ok else f"status={c.get('status')}: no executable adapter attached"})
    return out

# ═════════════════════════════════════════════════════════════════════════════
def causality_check(strategy: Strategy, df: pd.DataFrame, n_samples=8, seed=1):
    """Look-ahead guard: signal at bar j computed from df[:j+1] must equal the one computed from the full df."""
    n = len(df); rng = np.random.default_rng(seed)
    lo = min(260, n // 3)
    idx = sorted(set(rng.integers(lo, n - 2, n_samples).tolist()))
    full = strategy.generate(df); bad = []
    for j in idx:
        part = strategy.generate(df.iloc[: j + 1].reset_index(drop=True))
        a = (int(full.direction[j]), round(float(full.conf[j]), 4), int(full.grade[j]))
        b = (int(part.direction[j]), round(float(part.conf[j]), 4), int(part.grade[j]))
        if a != b: bad.append({"bar": int(j), "full": a, "causal": b})
    return {"samples": len(idx), "mismatches": len(bad), "details": bad[:5], "ok": not bad}
