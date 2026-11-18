"""
Robustness phases. Each returns {"status": PASS|FAIL|INCONCLUSIVE, "checks": [...], "metrics": {...}, ...}.
A 'check' = {"name","value","op","threshold","ok","note"}.  Status rule: any failed check -> FAIL;
too few trades -> INCONCLUSIVE (never silently PASS).
"""
import numpy as np
import pandas as pd
from . import settings as S
from .engine import BacktestContext, summarize, UNIVERSAL
from .strategies import ParamSpec

def check(name, value, op, thr, note=""):
    ok = {"<=": value <= thr, ">=": value >= thr, ">": value > thr, "<": value < thr, "==": value == thr}[op]
    return {"name": name, "value": None if value is None else round(float(value), 4), "op": op,
            "threshold": thr, "ok": bool(ok), "note": note}

def _status(checks, inconclusive=None):
    if inconclusive: return "INCONCLUSIVE"
    return "PASS" if all(c["ok"] for c in checks) else "FAIL"

# ══ Phase 2 · Monte Carlo ════════════════════════════════════════════════════
def _boot_stats(R, risk):
    n = R.shape[1]; sums = R.sum(1); exp = sums / n
    cum = R.cumsum(1); peak = np.maximum.accumulate(np.maximum(cum, 0), axis=1)
    dd = (peak - cum).max(1) * risk * 100
    return exp, dd

def run_monte_carlo(trades, progress=lambda *a, **k: None, cancel=lambda: None, risk=S.RISK_PER_TRADE):
    G = S.MC; n = len(trades)
    if n < S.MIN_TRADES:
        return {"status": "INCONCLUSIVE", "checks": [], "metrics": {"trades": n},
                "reason": f"only {n} trades (< {S.MIN_TRADES}); extend the period or loosen gates"}
    rng = np.random.default_rng(G["SEED"]); N = G["N"]
    r = np.array([t["r"] for t in trades]); rg = np.array([t["rg"] for t in trades]); cr = np.array([t["c"] for t in trades])
    out = {}
    # 1) iid bootstrap (resample WITH replacement — shuffling would leave terminal equity unchanged)
    progress(0.05, "bootstrap (iid)"); cancel()
    e1, dd1 = _boot_stats(r[rng.integers(0, n, (N, n))], risk)
    out["iid"] = {"p_expectancy_le_0": float((e1 <= 0).mean()), "exp_p5": float(np.percentile(e1, 5)),
                  "exp_p50": float(np.percentile(e1, 50)), "exp_p95": float(np.percentile(e1, 95)),
                  "dd_p50": float(np.percentile(dd1, 50)), "dd_p95": float(np.percentile(dd1, 95)),
                  "p_ruin": float((dd1 >= G["RUIN_DD_PCT"]).mean())}
    # 2) block bootstrap (keeps loss clustering)
    progress(0.35, "bootstrap (block)"); cancel()
    b = max(2, int(np.sqrt(n))); nb = int(np.ceil(n / b))
    starts = rng.integers(0, n, (N, nb)); idx = ((starts[:, :, None] + np.arange(b)[None, None, :]) % n).reshape(N, -1)[:, :n]
    e2, dd2 = _boot_stats(r[idx], risk)
    out["block"] = {"block_len": b, "p_expectancy_le_0": float((e2 <= 0).mean()), "dd_p95": float(np.percentile(dd2, 95)),
                    "p_ruin": float((dd2 >= G["RUIN_DD_PCT"]).mean())}
    # 3) missed fills: randomly drop trades
    progress(0.6, "missed-fill test"); cancel()
    keep = rng.random((N, n)) > G["DROP_FRAC"]; cnt = np.maximum(keep.sum(1), 1)
    e3 = (r[None, :] * keep).sum(1) / cnt
    out["dropped"] = {"drop_frac": G["DROP_FRAC"], "p_expectancy_le_0": float((e3 <= 0).mean())}
    # 4) cost stress
    progress(0.8, "cost stress"); cancel()
    rs = rg - cr * G["COST_MULT"]
    e4, _ = _boot_stats(rs[rng.integers(0, n, (N, n))], risk)
    out["cost_stress"] = {"cost_mult": G["COST_MULT"], "expectancy": float(rs.mean()),
                          "p_expectancy_le_0": float((e4 <= 0).mean())}
    checks = [
        check("P(expectancy ≤ 0) — iid bootstrap", out["iid"]["p_expectancy_le_0"], "<=", G["P_NEG_MAX"]),
        check("P(expectancy ≤ 0) — block bootstrap", out["block"]["p_expectancy_le_0"], "<=", G["P_NEG_MAX"]),
        check(f"P(expectancy ≤ 0) after dropping {int(G['DROP_FRAC']*100)}% of trades", out["dropped"]["p_expectancy_le_0"], "<=", G["P_NEG_MAX"] * 1.5),
        check(f"expectancy with costs ×{G['COST_MULT']:g} (R)", out["cost_stress"]["expectancy"], ">", 0.0),
        check("95th-pct max drawdown (%)", max(out["iid"]["dd_p95"], out["block"]["dd_p95"]), "<=", G["DD95_MAX_PCT"]),
        check(f"P(drawdown ≥ {G['RUIN_DD_PCT']:g}%)", max(out["iid"]["p_ruin"], out["block"]["p_ruin"]), "<=", G["P_RUIN_MAX"]),
    ]
    progress(1.0, "done")
    return {"status": _status(checks), "checks": checks, "metrics": {"trades": n, **out}}

# ══ param spaces ═════════════════════════════════════════════════════════════
def universal_specs(ctx: BacktestContext):
    b = ctx.base_cfg
    return [ParamSpec("sl_mult", b.sl_mult, max(0.5, b.sl_mult * 0.5), b.sl_mult * 2.0, 0.25),
            ParamSpec("min_confidence", b.min_confidence, 25, 75, 5),
            ParamSpec("tp_scale", 1.0, 0.5, 2.0, 0.25)]

def param_space(ctx):
    internal = list(ctx.strategy.PARAMS); return internal, universal_specs(ctx)

def _base(ctx, specs):
    p = dict(ctx.strategy.params)
    for s in specs:
        if s.name not in p: p[s.name] = s.default
    return p

# ══ Phase 3 · Walk-forward (true IS-optimise / OOS-test) ═════════════════════
def run_wfo(ctx: BacktestContext, progress=lambda *a, **k: None, cancel=lambda: None):
    G = S.WFO; day = 86_400 * 10**9
    internal, uni = param_space(ctx); specs = internal + uni; base = _base(ctx, specs)
    warm = min(300, ctx.n // 4)
    t_first, t_last = int(ctx.t[warm]), int(ctx.t[ctx.n - 6])
    wins = []; k = 0
    while True:
        s = t_first + k * G["OOS_DAYS"] * day; e = s + G["IS_DAYS"] * day; oe = e + G["OOS_DAYS"] * day
        if oe > t_last: break
        wins.append((int(np.searchsorted(ctx.t, s)), int(np.searchsorted(ctx.t, e)), int(np.searchsorted(ctx.t, oe)))); k += 1
    if len(wins) < G["MIN_WINDOWS"]:
        return {"status": "INCONCLUSIVE", "checks": [], "windows": [],
                "reason": f"{len(wins)} window(s) possible; need ≥ {G['MIN_WINDOWS']} (IS {G['IS_DAYS']}d + OOS {G['OOS_DAYS']}d). Use more days."}
    rng = np.random.default_rng(G["SEED"])
    trials = [dict(base)]
    while len(trials) < G["MAX_TRIALS"]:
        t = dict(base); changed = False
        for sp in specs:
            if rng.random() < 0.25:
                v = sp.at(base[sp.name], int(rng.choice([-2, -1, 1, 2])))
                if v != base[sp.name]: t[sp.name] = v; changed = True
        if changed: trials.append(t)
    rows, oos_all, oos_def_all, is_w = [], [], [], []
    for wi, (a, b, c) in enumerate(wins):
        progress(wi / len(wins), f"window {wi+1}/{len(wins)}: optimising {len(trials)} trials in-sample"); cancel()
        best, best_score, best_is = None, -1e9, None
        for tr in trials:
            res = ctx.run(tr, start=a, end=b); m = summarize(res["trades"], curve_points=2)
            score = m["expectancy_r"] * np.sqrt(min(m["trades"], 100)) if m["trades"] >= G["MIN_IS_TRADES"] else -1e9
            if score > best_score: best, best_score, best_is = tr, score, m
        if best is None: best, best_is = trials[0], summarize([])
        oos = ctx.run(best, start=b, end=c); mo = summarize(oos["trades"], curve_points=2)
        oos_d = ctx.run(trials[0], start=b, end=c); md = summarize(oos_d["trades"], curve_points=2)
        oos_all += oos["trades"]; oos_def_all += oos_d["trades"]
        is_w.append((best_is["expectancy_r"], best_is["trades"]))
        rows.append({"window": wi + 1, "is_start": pd.Timestamp(ctx.t[a], tz="UTC").strftime("%Y-%m-%d"),
                     "oos_start": pd.Timestamp(ctx.t[min(b, ctx.n-1)], tz="UTC").strftime("%Y-%m-%d"),
                     "oos_end": pd.Timestamp(ctx.t[min(c, ctx.n-1)], tz="UTC").strftime("%Y-%m-%d"),
                     "is_exp": best_is["expectancy_r"], "is_trades": best_is["trades"],
                     "oos_exp": mo["expectancy_r"], "oos_trades": mo["trades"],
                     "oos_default_exp": md["expectancy_r"],
                     "chosen": {k2: v for k2, v in best.items() if base.get(k2) != v}})
    O, D = summarize(oos_all, curve_points=60), summarize(oos_def_all, curve_points=2)
    wn = sum(n for _, n in is_w); is_exp = sum(e * n for e, n in is_w) / wn if wn else 0.0
    wfe = (O["expectancy_r"] / is_exp) if is_exp > 0 else 0.0
    evald = [r for r in rows if r["oos_trades"] > 0]
    pos = (sum(1 for r in evald if r["oos_exp"] > 0) / len(evald)) if evald else 0.0
    checks = [
        check("OOS expectancy (R, net of costs)", O["expectancy_r"], ">", 0.0),
        check("OOS profit factor", O["profit_factor"], ">", 1.0),
        check("walk-forward efficiency (OOS÷IS)", wfe, ">=", G["WFE_MIN"], "OOS edge retained vs the in-sample optimum"),
        check("profitable OOS windows (share)", pos, ">=", G["POS_WINDOWS_MIN"]),
    ]
    inc = O["trades"] < G["MIN_OOS_TRADES"]
    progress(1.0, "done")
    return {"status": _status(checks, inc), "checks": checks, "windows": rows,
            "reason": f"only {O['trades']} OOS trades (< {G['MIN_OOS_TRADES']})" if inc else None,
            "metrics": {"oos": {k2: O[k2] for k2 in ("trades", "expectancy_r", "profit_factor", "win_rate", "return_pct", "max_dd_pct", "equity")},
                        "oos_default_params": {k2: D[k2] for k2 in ("trades", "expectancy_r", "profit_factor")},
                        "is_expectancy_r": round(is_exp, 4), "wfe": round(wfe, 3), "windows": len(rows),
                        "trials_per_window": len(trials), "params_optimised": len(specs),
                        "selection_note": f"{len(trials)} trials x {len(rows)} windows were searched in-sample; OOS numbers are after that selection."}}

# ══ Phase 4 · Sensitivity ════════════════════════════════════════════════════
def classify(e0, em2, em1, ep1, ep2, spike_ret=None, plateau_ret=None):
    """PLATEAU / DECAYING / CLIFF / SPIKE / NO_EDGE. None = neighbour unavailable (range boundary / too few trades)."""
    spike_ret = S.SENS["SPIKE_RET"] if spike_ret is None else spike_ret
    plateau_ret = S.SENS["PLATEAU_RET"] if plateau_ret is None else plateau_ret
    if e0 is None or e0 <= 0: return "NO_EDGE"
    adj = [x for x in (em1, ep1) if x is not None]; far = [x for x in (em2, ep2) if x is not None]
    if not adj: return "UNTESTED"
    collapsed = [(x <= 0 or x < spike_ret * e0) for x in adj]
    if len(adj) == 2 and all(collapsed): return "SPIKE"           # isolated peak: the optimiser's favourite
    if any(collapsed): return "CLIFF"                              # one side falls off
    # plateau: adjacent steps keep >= PLATEAU_RET of the edge AND the +-2 steps still keep half of that (a slow decay is not a plateau)
    if all(x >= plateau_ret * e0 for x in adj) and all(x >= 0.5 * plateau_ret * e0 for x in far): return "PLATEAU"
    return "DECAYING"

def _exp(res, min_trades=10):
    m = summarize(res["trades"], curve_points=2)
    return (m["expectancy_r"], m["trades"]) if m["trades"] >= min_trades else (None, m["trades"])

def run_sensitivity(ctx: BacktestContext, progress=lambda *a, **k: None, cancel=lambda: None):
    G = S.SENS
    internal, uni = param_space(ctx); specs = internal + uni; base = _base(ctx, specs)
    br = ctx.run(base); e0, n0 = _exp(br, 1); regs = len(ctx.regime_names)
    if n0 < S.MIN_TRADES:
        return {"status": "INCONCLUSIVE", "checks": [], "params": [], "reason": f"baseline has only {n0} trades"}
    def by_regime(trades):
        out = {}
        for r in range(regs):
            tt = [t for t in trades if t["rg_"] == r]
            out[r] = (float(np.mean([t["r"] for t in tt])), len(tt)) if tt else (None, 0)
        return out
    base_reg = by_regime(br["trades"]); nb_reg = {r: [] for r in range(regs)}
    rows = []; steps = (-2, -1, 1, 2)
    for si, sp in enumerate(specs):
        cancel(); progress(si / (len(specs) + 1), f"nudging {sp.name}")
        vals = {}; shown = []
        for k in steps:
            v = sp.at(base[sp.name], k)
            if v == base[sp.name]: vals[k] = None; continue
            res = ctx.run({**base, sp.name: v}); ex, nn = _exp(res); vals[k] = ex
            shown.append({"step": k, "value": v, "expectancy_r": None if ex is None else round(ex, 4), "trades": nn})
            if abs(k) == 1:
                for r_, (er, nr) in by_regime(res["trades"]).items():
                    if er is not None and nr >= 5: nb_reg[r_].append(er)
        cls = classify(e0, vals[-2], vals[-1], vals[1], vals[2])
        rows.append({"param": sp.name, "internal": sp in internal, "base": base[sp.name], "class": cls, "neighbours": shown})
    # joint nudge
    rng = np.random.default_rng(G["SEED"]); joint = []
    for di in range(G["JOINT_DRAWS"]):
        cancel(); progress((len(specs) + di / G["JOINT_DRAWS"]) / (len(specs) + 1), "joint nudge of all parameters")
        p = {s.name: s.at(base[s.name], int(rng.choice([-1, 0, 1]))) for s in specs}
        ex, nn = _exp(ctx.run(p)); 
        if ex is not None: joint.append(ex)
    jpos = float(np.mean([x > 0 for x in joint])) if joint else 0.0
    jmed = float(np.median(joint) / e0) if joint and e0 else 0.0
    # regime survival
    regime_rows, surv, evaluable, blow = [], 0, 0, False
    for r in range(regs):
        er, nr = base_reg[r]; nm = float(np.median(nb_reg[r])) if nb_reg[r] else None
        ev = er is not None and nr >= G["REGIME_MIN_TRADES"]
        sv = ev and er > 0 and nm is not None and nm > 0
        if ev:
            evaluable += 1; surv += int(sv); blow = blow or er < G["REGIME_BLOWUP"]
        regime_rows.append({"regime": ctx.regime_names[r], "trades": nr, "expectancy_r": None if er is None else round(er, 4),
                            "neighbour_median_r": None if nm is None else round(nm, 4), "evaluated": bool(ev), "survives": bool(sv)})
    cls_n = {c: sum(1 for r in rows if r["class"] == c) for c in ("PLATEAU", "DECAYING", "CLIFF", "SPIKE", "NO_EDGE", "UNTESTED")}
    testable = max(1, len(rows) - cls_n["UNTESTED"])
    plateau_frac = cls_n["PLATEAU"] / testable
    checks = [
        check("baseline expectancy (R)", e0, ">", 0.0),
        check("SPIKE parameters (isolated optimum)", cls_n["SPIKE"], "==", 0, "a spike dies: neighbours collapse"),
        check("share of parameters on a PLATEAU", plateau_frac, ">=", G["PLATEAU_FRAC_MIN"]),
        check("joint ±1-step nudge: share profitable", jpos, ">=", G["JOINT_POS_MIN"]),
        check("regimes evaluated (≥2 needed)", evaluable, ">=", 2),
        check("regimes where plateau survives (share)", (surv / evaluable) if evaluable else 0.0, ">=", G["REGIME_PASS_FRAC"]),
        check(f"worst-regime expectancy ≥ {G['REGIME_BLOWUP']}R (no blow-up)", 0.0 if not blow else -1.0, ">=", 0.0),
    ]
    inc = evaluable < 2
    coverage = "full" if internal else "universal_only"
    progress(1.0, "done")
    return {"status": _status(checks, inc), "checks": checks, "params": rows, "regimes": regime_rows,
            "reason": "fewer than 2 regimes had enough trades" if inc else None,
            "metrics": {"baseline_expectancy_r": round(e0, 4), "class_counts": cls_n, "joint_positive_share": round(jpos, 3),
                        "joint_median_retention": round(jmed, 3), "coverage": coverage, "internal_params": len(internal),
                        "universal_params": len(uni),
                        "coverage_note": None if internal else
                        "Strategy exposes no internal parameters (legacy adapter): only sl_mult / min_confidence / tp_scale were nudged. "
                        "A PASS here does not prove the entry logic is robust."}}
