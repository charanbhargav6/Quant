"""
Flask routes + job manager for the Backtest tab.

Register in quant_server.py (same pattern as register_supervisor_routes):
    from core.backtest_endpoints import register_backtest_routes
    register_backtest_routes(app, get_db_agent, log)

Security: this module does NOT add CRAVE-wide auth, but its mutating routes (run / cancel) are protected by
  * optional shared token  : env CRAVE_API_TOKEN  -> header  X-CRAVE-Token
  * Origin check           : cross-site browser POSTs are refused (quant_server's CORS(app) is wildcard);
                             extra origins via CRAVE_ALLOWED_ORIGINS="http://host:port,...";
                             a page opened from file:// sends Origin 'null' -> set CRAVE_ALLOW_NULL_ORIGIN=1 to allow.
The heavy work runs in a SEPARATE PROCESS (backtesting.pipeline.runner) so it can never block Flask,
the GIL, the trading loop, or the live MT5 session.
"""
import json, logging, os, re, subprocess, sys, threading, time
from urllib.parse import urlparse
from flask import Response, abort, jsonify, request
from backtesting.pipeline import settings as S, store
from backtesting.pipeline import strategies as strat_mod

_mgr = None

# Security: validate job IDs before any filesystem operation to prevent path traversal.
# Format: bt_YYYYMMDD_HHMMSS_xxxx  (4 lowercase hex chars)
_JOB_RE = re.compile(r'^bt_\d{8}_\d{6}_[0-9a-f]{4,8}$')

def _validate_job_id(job_id: str):
    """Abort with 404 if job_id doesn't match the expected format."""
    if not _JOB_RE.match(str(job_id or "")):
        abort(404)

class JobManager:
    def __init__(self, log):
        self.log = log; self.procs = {}; self.lock = threading.Lock(); self._started = False

    def start(self):
        if self._started: return
        self._started = True
        for j in store.list_jobs(100):                         # orphans from a previous server run
            if j["status"] == "running" and store.job_age_s(j) > 120:
                store.update_job(j["id"], status="failed", verdict="ERROR", error="runner stopped unexpectedly", finished_at=store.now())
        threading.Thread(target=self._loop, daemon=True, name="bt-jobmgr").start()

    def _loop(self):
        while True:
            try: self.tick()
            except Exception as e: self.log.warning("backtest job manager: %s", e)
            time.sleep(2)

    def tick(self):
        with self.lock:
            for jid, p in list(self.procs.items()):
                rc = p.poll()
                if rc is None: continue
                self.procs.pop(jid)
                j = store.get_job(jid)
                if j and j["status"] in ("running", "queued"):
                    store.update_job(jid, status="failed", verdict="ERROR", finished_at=store.now(), error=f"runner exited with code {rc} (see State/backtests/{jid}/runner.log)")
            for j in store.list_jobs(50):                      # runners spawned by another process that died
                if j["status"] == "running" and j["id"] not in self.procs and store.job_age_s(j) > 120:
                    store.update_job(j["id"], status="failed", verdict="ERROR", finished_at=store.now(), error="runner stopped unexpectedly")
            queued = sorted([j for j in store.list_jobs(50) if j["status"] == "queued"], key=lambda j: j["created_at"])
            if queued and store.claim_job(queued[0]["id"]):    # atomic: at most one runner system-wide
                self._spawn(queued[0]["id"])

    def _spawn(self, jid):
        logf = open(store.job_dir(jid) / "runner.log", "a", encoding="utf-8")
        kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(S.ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        p = subprocess.Popen([sys.executable, "-m", "backtesting.pipeline.runner", "--job-id", jid], cwd=str(S.ROOT),
                             stdout=logf, stderr=subprocess.STDOUT, env=env, **kw)
        self.procs[jid] = p; store.update_job(jid, pid=p.pid)
        self.log.info("backtest job %s started (pid %s)", jid, p.pid)

    def cancel(self, jid):
        j = store.get_job(jid)
        if not j: return False
        if j["status"] == "queued":
            store.update_job(jid, status="cancelled", verdict="CANCELLED", finished_at=store.now()); return True
        store.request_cancel(jid)
        def _kill():
            time.sleep(30)
            p = self.procs.get(jid)
            if p and p.poll() is None:
                p.terminate(); store.update_job(jid, status="cancelled", verdict="CANCELLED", finished_at=store.now())
        threading.Thread(target=_kill, daemon=True).start(); return True

def get_manager(log=None):
    global _mgr
    if _mgr is None: _mgr = JobManager(log or logging.getLogger("backtest")); _mgr.start()
    return _mgr

def submit_backtest(strategy_ref, symbol=None, days=None, **opts):
    """Programmatic entry for the strategy creator / Telegram: returns job_id. Raises StrategyNotBacktestable."""
    strat, info = strat_mod.resolve(strategy_ref, symbol)
    days = int(days or S.DEFAULT_DAYS)
    if not (S.MIN_DAYS <= days <= S.MAX_DAYS): raise ValueError(f"days must be {S.MIN_DAYS}-{S.MAX_DAYS}")
    jid = store.create_job(strategy_ref, symbol or info["symbol"], days, opts, info["name"], info.get("lineage_id"))
    get_manager()
    return jid

def _guard():
    tok = os.environ.get("CRAVE_API_TOKEN", "")
    if tok and request.headers.get("X-CRAVE-Token", "") != tok: abort(401)
    origin = request.headers.get("Origin")
    if origin:
        if origin == "null":
            if os.environ.get("CRAVE_ALLOW_NULL_ORIGIN") != "1": abort(403)
            return
        host = (urlparse(origin).hostname or "").lower()
        allowed = {"127.0.0.1", "localhost", "::1", request.host.split(":")[0].lower()}
        extra = {urlparse(o.strip()).hostname for o in os.environ.get("CRAVE_ALLOWED_ORIGINS", "").split(",") if o.strip()}
        if host not in allowed | extra: abort(403)

def _public(j):
    return {"job_id": j["id"], "created_at": j["created_at"], "started_at": j["started_at"], "finished_at": j["finished_at"],
            "strategy_ref": j["strategy_ref"], "strategy_name": j["strategy_name"], "symbol": j["symbol"], "days": j["days"],
            "status": j["status"], "verdict": j["verdict"], "phases": j["phase_status"], "summary": j["summary"],
            "council": j["council_status"], "error": j["error"]}

def _pending_snapshot(j):
    """Same shape as the runner's snapshot.json, for jobs that have not written one yet (queued / just spawned)."""
    from backtesting.pipeline import PHASES
    return {"job_id": j["id"], "status": j["status"], "created_at": j["created_at"], "started_at": j["started_at"],
            "finished_at": j["finished_at"], "updated_at": j["created_at"], "current_phase": None, "overall_progress": 0.0,
            "strategy_ref": j["strategy_ref"], "strategy_name": j["strategy_name"], "symbol": j["symbol"], "days": j["days"],
            "phases": {p: {"status": (j["phase_status"] or {}).get(p, "pending"), "progress": 0.0, "message": "", "checks": [], "metrics": {}} for p in PHASES},
            "live": {"trades": 0, "cum_r": 0.0, "progress": 0.0}, "metrics": j["summary"] or None, "verdict": j["verdict"],
            "verdict_detail": None, "council": {"status": j["council_status"]}, "log": [], "error": j["error"]}

def _cache_coverage():
    out = []
    try:
        for d in sorted(S.CACHE_DIR.iterdir()):
            m = d / "meta.json"
            if m.exists():
                meta = json.loads(m.read_text(encoding="utf-8")); days = sorted(k for k, v in meta.get("days", {}).items() if v.get("status") == "ok")
                out.append({"symbol": meta.get("symbol", d.name), "days": len(days), "first": days[0] if days else None,
                            "last": days[-1] if days else None, "volume_kind": meta.get("volume_kind"), "mt5_symbol": meta.get("mt5_symbol")})
    except Exception: pass
    return out

def register_backtest_routes(app, get_db_agent=None, log=None):
    log = log or logging.getLogger("backtest"); mgr = get_manager(log)

    @app.route("/api/backtests/capabilities")
    def bt_capabilities():
        return jsonify({"strategies": strat_mod.list_backtestable(), "cache": _cache_coverage(),
                        "defaults": {"days": S.DEFAULT_DAYS, "min_days": S.MIN_DAYS, "max_days": S.MAX_DAYS, "bar_minutes": S.BAR_MINUTES,
                                     "max_open_trades": S.MAX_OPEN_TRADES, "risk_per_trade": S.RISK_PER_TRADE, "min_trades": S.MIN_TRADES},
                        "gates": {"monte_carlo": S.MC, "wfo": S.WFO, "sensitivity": {k: v for k, v in S.SENS.items() if k != "SEED"}}})

    @app.route("/api/backtests", methods=["GET"])
    def bt_list():
        limit = max(1, min(100, int(request.args.get("limit", S.HISTORY_LIMIT))))
        return jsonify({"jobs": [_public(j) for j in store.list_jobs(limit)]})

    @app.route("/api/backtests/run", methods=["POST"])
    def bt_run():
        _guard(); b = request.get_json(silent=True) or {}
        ref = str(b.get("strategy_ref", "")).strip()
        if not ref: return jsonify({"error": "strategy_ref required"}), 400
        try:
            days = int(b.get("days") or S.DEFAULT_DAYS)
            if not (S.MIN_DAYS <= days <= S.MAX_DAYS): return jsonify({"error": f"days must be {S.MIN_DAYS}-{S.MAX_DAYS}"}), 400
            mode = b.get("data_mode", "ticks")
            if mode not in ("ticks", "m1"): return jsonify({"error": "data_mode must be ticks or m1"}), 400
            jid = submit_backtest(ref, (b.get("symbol") or None), days, data_mode=mode, cache_only=bool(b.get("cache_only")))
        except strat_mod.StrategyNotBacktestable as e:
            return jsonify({"error": str(e)}), 422
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"job_id": jid, "status": "queued"}), 202

    @app.route("/api/backtests/<job_id>")
    def bt_get(job_id):
        _validate_job_id(job_id)
        snap = store.read_snapshot(job_id); j = store.get_job(job_id)
        if not j: return jsonify({"error": "not found"}), 404
        if snap:
            if j["status"] in store.TERMINAL and snap.get("status") not in store.TERMINAL:   # runner died / cancelled by manager
                snap.update(status=j["status"], verdict=j["verdict"], error=j["error"])
            return jsonify(snap)
        return jsonify(_pending_snapshot(j))

    @app.route("/api/backtests/<job_id>/stream")
    def bt_stream(job_id):
        _validate_job_id(job_id)
        if not store.get_job(job_id): return jsonify({"error": "not found"}), 404
        def gen():
            last_m, last_hb, deadline = 0.0, time.time(), time.time() + 24 * 3600
            while time.time() < deadline:
                p = S.RUNS_DIR / job_id / "snapshot.json"
                try: m = p.stat().st_mtime
                except OSError: m = 0.0
                j = store.get_job(job_id)
                if m and m != last_m:
                    last_m = m; snap = store.read_snapshot(job_id)
                    if snap: yield f"event: snapshot\ndata: {json.dumps(snap, default=str)}\n\n"
                elif not m and last_m == 0.0 and j:
                    last_m = -1; yield f"event: snapshot\ndata: {json.dumps(_pending_snapshot(j), default=str)}\n\n"
                if j and j["status"] in store.TERMINAL:
                    snap = store.read_snapshot(job_id) or _pending_snapshot(j)
                    yield f"event: snapshot\ndata: {json.dumps(snap, default=str)}\n\nevent: end\ndata: {{}}\n\n"; return
                if time.time() - last_hb > 15: last_hb = time.time(); yield ": hb\n\n"
                time.sleep(0.4)
        return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/backtests/<job_id>/cancel", methods=["POST"])
    def bt_cancel(job_id):
        _guard()          # auth check first (don't leak job existence to unauthenticated callers)
        _validate_job_id(job_id)
        return (jsonify({"ok": True}) if mgr.cancel(job_id) else (jsonify({"error": "not found"}), 404))
