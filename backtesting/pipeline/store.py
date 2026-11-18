"""
Persistence + live state for backtest jobs.

  Database/backtest_runs.db     history table (own SQLite file: never contends with crave.db)
  State/backtests/<job>/events.jsonl   append-only, numbered events  -> SSE tails this (resumable via ?since=seq)
  State/backtests/<job>/snapshot.json  full current state (atomic replace) -> polling fallback / page reload
  State/backtests/<job>/CANCEL         flag file checked by the runner
Only the runner process writes events/snapshot; the Flask server only reads them.
"""
import json, os, sqlite3, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from . import settings as S
from . import PHASES

WEIGHTS = {"data": 15, "backtest": 15, "monte_carlo": 10, "wfo": 35, "sensitivity": 25}
TERMINAL = {"completed", "negative", "inconclusive", "failed", "cancelled"}

def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def job_dir(job_id) -> Path:
    d = S.RUNS_DIR / job_id; d.mkdir(parents=True, exist_ok=True); return d

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, created_at TEXT, started_at TEXT, finished_at TEXT, strategy_ref TEXT, strategy_name TEXT,
  symbol TEXT, days INTEGER, status TEXT, verdict TEXT, phase_status TEXT, summary TEXT, params TEXT, error TEXT,
  pid INTEGER, lineage_id TEXT, council_status TEXT);
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at DESC);
"""
_lock = threading.Lock()
def conn():
    S.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(S.DB_PATH), timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=15000")
    c.executescript(_SCHEMA); return c

def create_job(strategy_ref, symbol, days, params=None, name=None, lineage_id=None) -> str:
    jid = "bt_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    ps = {p: "pending" for p in PHASES}
    with _lock, conn() as c:
        c.execute("INSERT INTO jobs(id,created_at,strategy_ref,strategy_name,symbol,days,status,phase_status,params,lineage_id,council_status) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (jid, now(), strategy_ref, name or strategy_ref, symbol, days, "queued", json.dumps(ps),
                   json.dumps(params or {}), lineage_id or strategy_ref, "not_needed"))
    job_dir(jid); return jid

def update_job(jid, **cols):
    for k in ("phase_status", "summary", "params"):
        if k in cols and not isinstance(cols[k], str): cols[k] = json.dumps(cols[k], default=str)
    with _lock, conn() as c:
        c.execute(f"UPDATE jobs SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), jid))

def get_job(jid):
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return _row(r) if r else None

def list_jobs(limit=S.HISTORY_LIMIT):
    with conn() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]

def claim_job(jid) -> bool:
    """Atomically move queued -> running iff NO other job is running. Safe across processes (bot + server managers)."""
    with _lock, conn() as c:
        cur = c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued' "
                        "AND NOT EXISTS (SELECT 1 FROM jobs WHERE status='running')", (now(), jid))
        return cur.rowcount == 1

def job_age_s(j) -> float:
    """Seconds since the runner last proved it is alive (heartbeat, else since it was claimed)."""
    hb = heartbeat_age(j["id"])
    if hb < 1e8: return hb
    try: return time.time() - datetime.strptime(j["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception: return 1e9

def lineage_count(lineage_id) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM jobs WHERE lineage_id=? AND status IN ('completed','negative','inconclusive')",
                         (lineage_id,)).fetchone()[0]

def _row(r):
    d = dict(r)
    for k in ("phase_status", "summary", "params"):
        try: d[k] = json.loads(d[k]) if d.get(k) else {}
        except Exception: d[k] = {}
    return d

# ── live state ───────────────────────────────────────────────────────────────
class JobState:
    def __init__(self, jid, meta):
        self.jid = jid; self.dir = job_dir(jid); self.seq = 0; self._t_ev = {}; self._t_snap = 0.0
        self.snap = {"job_id": jid, "status": "running", "created_at": meta.get("created_at"), "started_at": now(),
                     "finished_at": None, "updated_at": now(), "current_phase": None, "overall_progress": 0.0,
                     "strategy_ref": meta["strategy_ref"], "strategy_name": meta["strategy_name"], "symbol": meta["symbol"],
                     "days": meta["days"], "phases": {p: {"status": "pending", "progress": 0.0, "message": "", "checks": [],
                                                           "metrics": {}, "started_at": None, "finished_at": None} for p in PHASES},
                     "live": {"trades": 0, "cum_r": 0.0, "progress": 0.0}, "metrics": None, "verdict": None,
                     "verdict_detail": None, "council": {"status": "not_needed"}, "log": [], "error": None, "pid": os.getpid()}
        self.events = open(self.dir / "events.jsonl", "a", encoding="utf-8", buffering=1)
        self._alive = True
        threading.Thread(target=self._beat, daemon=True).start()
        self.flush(force=True)

    def _beat(self):
        hb = self.dir / "heartbeat"
        while self._alive:
            try: hb.write_text(str(time.time()))
            except Exception: pass
            time.sleep(4)

    def emit(self, type_, throttle=0.0, **data):
        t = time.time()
        if throttle and t - self._t_ev.get(type_, 0) < throttle: return
        self._t_ev[type_] = t; self.seq += 1
        self.events.write(json.dumps({"seq": self.seq, "ts": now(), "type": type_, "data": data}, default=str) + "\n")

    def flush(self, force=False):
        t = time.time()
        if not force and t - self._t_snap < 0.5: return
        self._t_snap = t; self.snap["updated_at"] = now()
        p = self.dir / "snapshot.json"; tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.snap, default=str), encoding="utf-8"); os.replace(tmp, p)

    def _overall(self):
        tot = 0.0
        for p, w in WEIGHTS.items():
            ph = self.snap["phases"][p]
            tot += w * (1.0 if ph["status"] in ("pass", "fail", "inconclusive", "skipped", "done") else ph["progress"])
        self.snap["overall_progress"] = round(tot, 1)

    def log(self, msg, level="info"):
        self.snap["log"] = (self.snap["log"] + [{"t": now(), "level": level, "msg": msg}])[-60:]
        self.emit("log", level=level, msg=msg); self.flush()

    def phase_start(self, p, message=""):
        ph = self.snap["phases"][p]; ph.update(status="running", progress=0.0, message=message, started_at=now())
        self.snap["current_phase"] = p; self._overall()
        self.emit("phase_start", phase=p, message=message); self.flush(True); self.sync_db()

    def phase_progress(self, p, frac, message="", **extra):
        ph = self.snap["phases"][p]; ph["progress"] = round(max(0.0, min(1.0, frac)), 3)
        if message: ph["message"] = message
        ph.update({k: v for k, v in extra.items() if v is not None}); self._overall()
        self.emit("phase_progress", throttle=0.25, phase=p, progress=ph["progress"], message=ph["message"], **extra)
        self.flush()

    def phase_done(self, p, status, **result):
        ph = self.snap["phases"][p]
        ph.update(status=status, progress=1.0, finished_at=now(), **result); self._overall()
        self.emit("phase_done", phase=p, status=status, checks=result.get("checks", []), metrics=result.get("metrics", {}))
        self.flush(True); self.sync_db()

    def set_live(self, **kv):
        self.snap["live"].update(kv); self.emit("live", throttle=0.4, **self.snap["live"]); self.flush()

    def sync_db(self):
        update_job(self.jid, phase_status={p: v["status"] for p, v in self.snap["phases"].items()})

    def finish(self, status, verdict, detail, summary=None, error=None):
        self.snap.update(status=status, verdict=verdict, verdict_detail=detail, finished_at=now(), error=error,
                         current_phase=None, metrics=summary)
        self.snap["overall_progress"] = 100.0 if status in ("completed", "negative", "inconclusive") else self.snap["overall_progress"]
        self.emit("finished", status=status, verdict=verdict, detail=detail); self.flush(True)
        update_job(self.jid, status=status, verdict=verdict, finished_at=now(), summary=summary or {}, error=error,
                   phase_status={p: v["status"] for p, v in self.snap["phases"].items()})

    def close(self):
        self._alive = False
        try: self.events.close()
        except Exception: pass

class Cancelled(RuntimeError): ...
def cancel_requested(jid) -> bool: return (job_dir(jid) / "CANCEL").exists()
def request_cancel(jid): (job_dir(jid) / "CANCEL").write_text(now())
def heartbeat_age(jid) -> float:
    try: return time.time() - float((S.RUNS_DIR / jid / "heartbeat").read_text())
    except Exception: return 1e9

def read_snapshot(jid):
    p = S.RUNS_DIR / jid / "snapshot.json"
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return None
