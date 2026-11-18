"""
Pipeline runner. ALWAYS executed as its own process:
    python -m backtesting.pipeline.runner --job-id bt_...        (spawned by core/backtest_endpoints.py)
    python -m backtesting.pipeline.runner --run --strategy trend_pa_gold --symbol XAUUSD=X --days 180   (CLI)

State machine
  Phase 0 data  -> Phase 1 backtest --(negative)--> Council + Telegram                         [status=negative]
                                    --(<MIN_TRADES)-> Telegram                                  [status=inconclusive]
                                    --(positive)--> Phase 2 Monte Carlo -> Phase 3 WFO -> Phase 4 Sensitivity
                                                    -> verdict ROBUST | FAILED_<phase> | INCONCLUSIVE  [status=completed]
Nothing here ever edits core/strategy_registry.py: promotion to live_ready stays a human decision.
"""
import argparse, json, logging, os, sys, time, traceback
from datetime import datetime, timezone
import numpy as np
from . import settings as S, store, notify
from . import PHASES
from .store import JobState, Cancelled
from .strategies import resolve, causality_check, StrategyNotBacktestable, ParamSpec
from .engine import BacktestContext, summarize
from . import phases as P
from . import mt5_downloader as dl, data_store as ds

log = logging.getLogger("crave.bt.runner")

def _gate_backtest(m):
    from .phases import check
    checks = [check("trades ≥ minimum", m["trades"], ">=", S.MIN_TRADES),
              check("net expectancy (R)", m["expectancy_r"], ">", 0.0),
              check("net total return (R)", m["total_r"], ">", 0.0),
              check("profit factor", m["profit_factor"], ">", 1.0)]
    return checks

def _diagnostics(ctx, res, m):
    tr = res["trades"]; out = {}
    out["skipped_signals"] = res["skipped"]
    out["exit_mix"] = {k: sum(1 for t in tr if t["o"] == k) for k in ("sl", "trailing_sl", "full_target", "timeout")}
    for name, f in (("buy", lambda t: t["d"] > 0), ("sell", lambda t: t["d"] < 0)):
        xs = [t["r"] for t in tr if f(t)]
        out[f"{name}_expectancy_r"] = round(float(np.mean(xs)), 4) if xs else None; out[f"{name}_trades"] = len(xs)
    out["by_regime"] = {ctx.regime_names[r]: {"trades": sum(1 for t in tr if t["rg_"] == r),
                                               "expectancy_r": round(float(np.mean([t["r"] for t in tr if t["rg_"] == r])), 4)}
                        for r in range(len(ctx.regime_names)) if any(t["rg_"] == r for t in tr)}
    out["cost_share_of_gross"] = round(m["avg_cost_r"] / abs(m["expectancy_gross_r"]), 3) if m["expectancy_gross_r"] else None
    out["gross_expectancy_r"] = m["expectancy_gross_r"]; out["avg_cost_r"] = m["avg_cost_r"]
    return out

def _child_candidate(job, strat, info, suggestions, council_resp):
    """Create an UNAPPROVED child candidate from validated council suggestions. Never runs automatically."""
    params, applied = dict(strat.params), []
    specs = {p.name: p for p in strat.PARAMS}
    for s in suggestions:
        sp = specs.get(s["param"])
        try: v = float(s["value"])
        except Exception: continue
        if sp is None: continue
        v = min(sp.hi, max(sp.lo, v)); v = int(round(v)) if sp.kind == "int" else v
        params[sp.name] = v; applied.append({**s, "value": v})
    if not applied or not S.COUNCIL_CREATE_CHILD: return None
    path = S.CANDIDATES_FILE
    try: cands = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception: cands = []
    cid = "strat_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "_c"
    cands.append({"id": cid, "name": f"{info['name']} (council v{store.lineage_count(job['lineage_id']) + 1})",
                  "status": "council_suggested", "adapter": getattr(strat, "adapter_key", strat.key), "params": params,
                  "instruments": [job["symbol"]], "parent_id": job["strategy_ref"], "lineage_id": job["lineage_id"],
                  "suggested_changes": applied, "source_job": job["id"], "created_at": store.now(),
                  "note": "Advisory council suggestion. NOT approved. Must pass the full pipeline on data it has not seen."})
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(cands, indent=2), encoding="utf-8"); os.replace(tmp, path)
    return cid

def run_job(jid):
    job = store.get_job(jid)
    if not job: raise SystemExit(f"job {jid} not found")
    store.update_job(jid, status="running", started_at=store.now(), pid=os.getpid())
    st = JobState(jid, {"created_at": job["created_at"], "strategy_ref": job["strategy_ref"],
                        "strategy_name": job["strategy_name"], "symbol": job["symbol"], "days": job["days"]})
    cancel = lambda: (_ for _ in ()).throw(Cancelled()) if store.cancel_requested(jid) else None
    opts = job.get("params") or {}
    try:
        strat, info = resolve(job["strategy_ref"], job["symbol"])
        symbol, days = job["symbol"], int(job["days"])
        st.snap["strategy_name"] = info["name"]; store.update_job(jid, strategy_name=info["name"], lineage_id=info.get("lineage_id", jid))
        job["strategy_name"] = info["name"]; job["lineage_id"] = info.get("lineage_id", jid)

        # ── Phase 0 · data ────────────────────────────────────────────────
        st.phase_start("data", "connecting to MT5")
        dsum = {"mt5_used": False}
        if not opts.get("cache_only"):
            try:
                dsum = dl.download(symbol, days, mode=opts.get("data_mode", "ticks"), cancel=cancel,
                                   progress=lambda **k: (st.phase_progress("data", (k.get("done", 0) / max(1, k.get("total", 1))) * 0.9,
                                                                           k.get("message", ""), day=k.get("day"), ticks=k.get("ticks")),
                                                         st.set_live(download_day=k.get("day"))))
            except dl.MT5Unavailable as ex:
                st.log(f"MT5 not available ({ex}) - using cached data if present", "warn"); dsum = {"mt5_used": False, "warning": str(ex)}
        bars, q, meta = ds.load_bars(symbol, days)
        st.phase_progress("data", 0.93, "quality checks")
        if not q.get("ok"):
            st.phase_done("data", "fail", metrics={"quality": q, "download": dsum},
                          checks=[{"name": "data coverage", "value": q.get("coverage"), "op": ">=", "threshold": S.MIN_COVERAGE, "ok": False, "note": q.get("reason", "")}])
            for p in PHASES[1:]: st.phase_done(p, "skipped")
            st.finish("failed", "NO_DATA", f"Insufficient MT5 data for {symbol}: coverage {q.get('coverage')}. {dsum.get('warning','')}".strip(), error="insufficient data")
            notify.send_telegram(f"❌ <b>Backtest could not run</b>\n{notify.e(info['name'])} · {notify.e(symbol)}\nData coverage {q.get('coverage')} (need ≥ {S.MIN_COVERAGE}).")
            return
        ctx = BacktestContext(bars, symbol, strat, contract_size=meta.get("contract_size"))
        st.phase_progress("data", 0.96, "look-ahead guard (signals must not change with future data)")
        cz = causality_check(strat, bars.iloc[-3000:].reset_index(drop=True), n_samples=6)
        dchecks = [{"name": "look-ahead guard mismatches", "value": cz["mismatches"], "op": "==", "threshold": 0, "ok": cz["ok"], "note": f"{cz['samples']} bars sampled"},
                   {"name": "data coverage", "value": q["coverage"], "op": ">=", "threshold": S.MIN_COVERAGE, "ok": True, "note": f"volume = {q['volume_kind']}"}]
        st.phase_done("data", "pass" if cz["ok"] else "fail", metrics={"quality": q, "download": dsum, "causality": cz,
                                                                        "contract_size": meta.get("contract_size")}, checks=dchecks)
        if not cz["ok"]:
            for p in PHASES[1:]: st.phase_done(p, "skipped")
            st.finish("failed", "LOOKAHEAD_DETECTED", "Strategy signals change when future bars are added - look-ahead bias. Fix the strategy before backtesting.",
                      error=json.dumps(cz["details"])); notify.send_telegram(f"🚨 <b>Look-ahead detected</b> in {notify.e(info['name'])}. Backtest aborted."); return

        # ── Phase 1 · backtest ────────────────────────────────────────────
        cancel(); st.phase_start("backtest", f"{len(bars):,} bars · real spread costs")
        def onp(frac, ntr, cum): st.phase_progress("backtest", frac, f"{ntr} trades so far"); st.set_live(trades=ntr, cum_r=round(cum, 2), progress=round(frac, 3))
        res = ctx.run(on_progress=onp, cancel=cancel); m = summarize(res["trades"])
        checks = _gate_backtest(m); low = m["trades"] < S.MIN_TRADES
        positive = (not low) and all(c["ok"] for c in checks)
        st.set_live(trades=m["trades"], cum_r=m["total_r"], equity=m["equity"], progress=1.0)
        bt_status = "inconclusive" if low else ("pass" if positive else "fail")
        st.phase_done("backtest", bt_status, checks=checks, metrics=m, detail={"skipped": res["skipped"], "data_period": [q["first"], q["last"]]})
        summary = {k: m[k] for k in ("trades", "return_pct", "max_dd_pct", "profit_factor", "expectancy_r", "expectancy_gross_r",
                                     "avg_cost_r", "win_rate", "sharpe", "t_stat", "total_r")}
        summary.update(period_first=q["first"][:10], period_last=q["last"][:10], volume_kind=q["volume_kind"])
        diag = _diagnostics(ctx, res, m)

        if low:
            for p in PHASES[2:]: st.phase_done(p, "skipped")
            st.finish("inconclusive", "INCONCLUSIVE_LOW_SAMPLE", f"Only {m['trades']} trades (< {S.MIN_TRADES}). Use a longer period or a more active configuration.", summary)
            notify.send_telegram(notify.fmt_result(info["name"], symbol, m, "⚪ <b>Backtest inconclusive</b> (too few trades)")); return
        if not positive:
            for p in PHASES[2:]: st.phase_done(p, "skipped")
            st.finish("negative", "NEGATIVE", "Net of real spread the strategy does not make money. Forwarded to the AI Council.", summary)
            _negative_flow(st, job, info, strat, summary, diag, m); return

        # ── Phases 2-4 ────────────────────────────────────────────────────
        notify_progress = lambda ph: (lambda f, msg="": st.phase_progress(ph, f, msg))
        cancel(); st.phase_start("monte_carlo", "bootstrap · block bootstrap · missed fills · cost stress")
        mc = P.run_monte_carlo(res["trades"], notify_progress("monte_carlo"), cancel)
        st.phase_done("monte_carlo", mc["status"].lower(), checks=mc["checks"], metrics=mc.get("metrics", {}), reason=mc.get("reason"))
        cancel(); st.phase_start("wfo", "rolling in-sample optimisation → out-of-sample test")
        wf = P.run_wfo(ctx, notify_progress("wfo"), cancel)
        st.phase_done("wfo", wf["status"].lower(), checks=wf["checks"], metrics=wf.get("metrics", {}), reason=wf.get("reason"), detail={"windows": wf.get("windows", [])})
        cancel(); st.phase_start("sensitivity", "nudging every parameter · plateau vs spike · regimes")
        se = P.run_sensitivity(ctx, notify_progress("sensitivity"), cancel)
        st.phase_done("sensitivity", se["status"].lower(), checks=se["checks"], metrics=se.get("metrics", {}), reason=se.get("reason"),
                      detail={"params": se.get("params", []), "regimes": se.get("regimes", [])})
        stat = {"monte_carlo": mc["status"], "wfo": wf["status"], "sensitivity": se["status"]}
        failed = [k for k, v in stat.items() if v == "FAIL"]; inc = [k for k, v in stat.items() if v == "INCONCLUSIVE"]
        if not failed and not inc: verdict, text = "ROBUST", "All four phases passed. Eligible for PAPER trading review - this does not change live_ready."
        elif failed: verdict, text = "FAILED_" + "+".join(x.upper() for x in failed), "Failed: " + ", ".join(failed)
        else: verdict, text = "INCONCLUSIVE", "Not enough data to judge: " + ", ".join(inc)
        summary.update(mc_p_neg=mc.get("metrics", {}).get("iid", {}).get("p_expectancy_le_0"),
                       wfe=wf.get("metrics", {}).get("wfe"), sens_spikes=se.get("metrics", {}).get("class_counts", {}).get("SPIKE"),
                       sens_coverage=se.get("metrics", {}).get("coverage"))
        st.finish("completed", verdict, text, summary)
        head = {"ROBUST": "✅ <b>Backtest ROBUST - all 4 phases passed</b>"}.get(verdict, "⚠️ <b>Backtest finished: " + notify.e(verdict) + "</b>")
        notify.send_telegram(notify.fmt_result(info["name"], symbol, m, head) + f"\nMC {mc['status']} · WFO {wf['status']} · Sens {se['status']}" +
                             ("\n<i>Sensitivity covered universal parameters only.</i>" if se.get("metrics", {}).get("coverage") == "universal_only" else ""))
        if failed and S.COUNCIL_ON_PHASE_FAILURE:
            pf = {k: [c for c in r["checks"] if not c["ok"]] for k, r in (("monte_carlo", mc), ("wfo", wf), ("sensitivity", se)) if k in failed}
            _council_flow(st, job, info, strat, summary, {**diag, "phase_failures": pf}, m)
    except Cancelled:
        st.log("cancelled by user", "warn")
        for p in PHASES:
            if st.snap["phases"][p]["status"] in ("pending", "running"): st.phase_done(p, "skipped")
        st.finish("cancelled", "CANCELLED", "Cancelled by user")
    except StrategyNotBacktestable as ex:
        st.finish("failed", "NOT_BACKTESTABLE", str(ex), error=str(ex))
    except Exception as ex:
        log.exception("runner crashed")
        st.finish("failed", "ERROR", f"{type(ex).__name__}: {ex}", error=traceback.format_exc()[-1500:])
        notify.send_telegram(f"❌ <b>Backtest crashed</b>: {notify.e(type(ex).__name__)}: {notify.e(str(ex)[:200])}")
    finally:
        st.close()

def _negative_flow(st, job, info, strat, summary, diag, m):
    notify.send_telegram(notify.fmt_result(info["name"], job["symbol"], m, "📉 <b>Backtest NEGATIVE</b>") +
                         f"\nCosts took {summary['avg_cost_r']:.3f}R/trade of {summary['expectancy_gross_r']:+.3f}R gross.\n→ forwarding to AI Council.")
    _council_flow(st, job, info, strat, summary, diag, m, notified=True)

def _council_flow(st, job, info, strat, summary, diag, m, notified=False):
    jid = job["id"]
    if store.lineage_count(job["lineage_id"]) > S.COUNCIL_MAX_LINEAGE_ITER:
        st.snap["council"] = {"status": "lineage_exhausted"}; store.update_job(jid, council_status="lineage_exhausted"); st.flush(True)
        notify.send_telegram(f"🛑 {notify.e(info['name'])}: {S.COUNCIL_MAX_LINEAGE_ITER} council iterations already used on this strategy line. "
                             "Further tuning on the same data is curve-fitting; needs new data or a new idea."); return
    st.snap["council"] = {"status": "pending"}; store.update_job(jid, council_status="pending"); st.flush(True)
    out = notify.forward_to_council(job, summary, diag)
    council = {"status": out["status"], "endpoint": out.get("endpoint"), "error": out.get("error")}
    if out["status"] == "received":
        txt = notify.summarize_council(out["response"]); sugg = notify.extract_suggestions(out["response"])
        child = _child_candidate(job, strat, info, sugg, out["response"]) if sugg else None
        council.update(summary=txt, suggestions=sugg, child_candidate=child)
        notify.send_telegram(f"🧠 <b>AI Council review</b> - {notify.e(info['name'])}\n{notify.e(txt)}" +
                             (f"\n\nSuggested changes saved as unapproved candidate <code>{notify.e(child)}</code>." if child else ""))
    else:
        notify.send_telegram(f"🧠 Council review for {notify.e(info['name'])} is {notify.e(out['status'])}: {notify.e(out.get('error',''))}")
    (store.job_dir(jid) / "council_inbox.json").write_text(json.dumps({"job": jid, "payload": notify.council_payload(job, summary, diag), "result": out}, default=str, indent=1), encoding="utf-8")
    st.snap["council"] = council; store.update_job(jid, council_status=out["status"]); st.emit("council", **council); st.flush(True)

def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--job-id"); ap.add_argument("--run", action="store_true")
    ap.add_argument("--strategy"); ap.add_argument("--symbol"); ap.add_argument("--days", type=int, default=S.DEFAULT_DAYS)
    ap.add_argument("--cache-only", action="store_true"); ap.add_argument("--data-mode", default="ticks")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    jid = a.job_id
    if a.run:
        strat, info = resolve(a.strategy, a.symbol)
        jid = store.create_job(a.strategy, a.symbol or info["symbol"], a.days, {"cache_only": a.cache_only, "data_mode": a.data_mode}, info["name"], info.get("lineage_id"))
        print("job:", jid)
    if not jid: ap.error("--job-id or --run required")
    run_job(jid); return 0

if __name__ == "__main__":
    sys.exit(main())
