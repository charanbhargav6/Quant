"""
CRAVE — MT5 Process Supervisor (Phase 1)
============================================
WHY THIS EXISTS:
  MT5's Python API holds one terminal session per process. Trading N MT5
  accounts concurrently means N processes, not N threads in one process.
  This module is the piece that was missing: something that actually
  launches `quant_server.py --profile <name>`, tracks whether it's alive,
  and restarts it if it dies — instead of requiring you to manually run
  a command per account and remember which port you used.

WHAT IT DOES:
  - launch(account_id):  allocate a free port, ensure .env.<profile> has
    it, spawn the subprocess, poll until it responds, record PID+port+
    status in the accounts table.
  - stop(account_id):    graceful shutdown (SIGTERM, wait, SIGKILL if it
    doesn't exit) instead of an abrupt kill mid-trade.
  - health_check_loop(): background thread, polls every managed process's
    /api/ping. Marks 'crashed' and restarts (bounded, with backoff) if a
    process stops responding.

WHAT IT DELIBERATELY DOES NOT DO:
  - Does not manage Zerodha/Binance accounts — those don't need process
    isolation (see Phase 2, core/multi_tenant_broker_manager.py). Calling
    launch() on a non-MT5 account is a no-op that logs a warning, so a
    mistaken call here can't accidentally spawn a redundant process for
    a broker that doesn't need one.
  - Does not decide WHEN to launch — that's the caller's job (wired into
    the verify-and-connect success path in account_endpoints_patch.py).
"""

import os
import sys
import time
import socket
import signal
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("crave.supervisor")

ENGINE_ROOT = Path(__file__).parent.parent
QUANT_SERVER = ENGINE_ROOT / "quant_server.py"

# Ports handed out to child MT5 processes. Kept away from common dev ports
# and away from the default 8765 the base .env server binds to.
PORT_RANGE_START = 9100
PORT_RANGE_END = 9199

HEALTH_CHECK_INTERVAL_SECS = 15
STARTUP_TIMEOUT_SECS = 30
GRACEFUL_STOP_TIMEOUT_SECS = 10
MAX_RESTART_ATTEMPTS = 5
RESTART_BACKOFF_BASE_SECS = 5   # 5s, 10s, 20s, 40s, 80s


class AccountProcess:
    """In-memory handle for one supervised subprocess. The DB row is the
    source of truth for status across restarts of the supervisor itself;
    this object is just the live Popen handle while it's running."""

    def __init__(self, account_id: int, profile: str, port: int, popen: subprocess.Popen):
        self.account_id = account_id
        self.profile = profile
        self.port = port
        self.popen = popen
        self.consecutive_failures = 0


class ProcessSupervisor:

    def __init__(self, db):
        self.db = db
        self._processes: dict[int, AccountProcess] = {}   # account_id -> AccountProcess
        self._lock = threading.Lock()
        self._health_thread = None
        self._stop_event = threading.Event()

    # ─────────────────────────────────────────────────────────────────────
    # PORT ALLOCATION
    # ─────────────────────────────────────────────────────────────────────

    def _port_is_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) != 0

    def _allocate_port(self) -> Optional[int]:
        taken = set()
        for row in self.db.query("SELECT port FROM accounts WHERE port IS NOT NULL"):
            taken.add(row["port"])
        for p in self._processes.values():
            taken.add(p.port)

        for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
            if port in taken:
                continue
            if self._port_is_free(port):
                return port
        return None

    # ─────────────────────────────────────────────────────────────────────
    # LAUNCH
    # ─────────────────────────────────────────────────────────────────────

    def launch(self, account_id: int) -> dict:
        """Returns {"ok": bool, "reason": str|None, "port": int|None}"""
        acc = self.db.get_account(account_id)
        if not acc:
            return {"ok": False, "reason": "Account not found", "port": None}

        broker = (acc.get("broker") or "mt5").lower()
        if broker != "mt5":
            return {
                "ok": False,
                "reason": f"launch() is only for MT5 accounts (process-per-account). "
                          f"'{broker}' accounts run in the multi-tenant manager instead.",
                "port": None,
            }

        with self._lock:
            if account_id in self._processes and self._processes[account_id].popen.poll() is None:
                return {"ok": True, "reason": "Already running", "port": self._processes[account_id].port}

            profile = acc.get("profile")
            if not profile:
                return {"ok": False, "reason": "Account has no profile name set", "port": None}

            env_file = ENGINE_ROOT / f".env.{profile}"
            if not env_file.exists():
                return {"ok": False, "reason": f".env.{profile} does not exist — verify the account first", "port": None}

            port = acc.get("port") or self._allocate_port()
            if port is None:
                return {"ok": False, "reason": "No free ports available in range", "port": None}

            self._write_port_into_env(env_file, port)

            self.db.execute(
                "UPDATE accounts SET process_status=?, port=? WHERE id=?",
                ("starting", port, account_id),
            )

            try:
                popen = subprocess.Popen(
                    [sys.executable, str(QUANT_SERVER), "--profile", profile],
                    cwd=str(ENGINE_ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,   # own process group -> clean SIGTERM later
                )
            except Exception as e:
                self.db.execute(
                    "UPDATE accounts SET process_status=?, last_error=? WHERE id=?",
                    ("crashed", str(e), account_id),
                )
                return {"ok": False, "reason": f"Failed to spawn process: {e}", "port": None}

            proc = AccountProcess(account_id, profile, port, popen)
            self._processes[account_id] = proc

        ok = self._wait_for_ready(port, timeout=STARTUP_TIMEOUT_SECS)
        if not ok:
            self.stop(account_id, reason="startup_timeout")
            return {
                "ok": False,
                "reason": f"Process started but didn't respond on port {port} within "
                          f"{STARTUP_TIMEOUT_SECS}s — check its logs.",
                "port": port,
            }

        self.db.execute(
            "UPDATE accounts SET status=?, process_status=?, pid=?, port=?, last_health_check=datetime('now') WHERE id=?",
            ("connected", "running", popen.pid, port, account_id),
        )
        logger.info(f"[Supervisor] Account {account_id} ({profile}) launched on port {port}, PID {popen.pid}")
        return {"ok": True, "reason": None, "port": port}

    def _write_port_into_env(self, env_file: Path, port: int):
        lines = env_file.read_text(encoding="utf-8").splitlines(keepends=True) if env_file.exists() else []
        out, replaced = [], False
        for line in lines:
            if line.strip().startswith("SERVER_PORT="):
                out.append(f"SERVER_PORT={port}\n")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"SERVER_PORT={port}\n")
        env_file.write_text("".join(out), encoding="utf-8")

    def _wait_for_ready(self, port: int, timeout: int) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = requests.get(f"http://127.0.0.1:{port}/api/ping", timeout=2)
                if r.status_code == 200:
                    return True
            except requests.exceptions.RequestException:
                pass
            time.sleep(1)
        return False

    # ─────────────────────────────────────────────────────────────────────
    # STOP
    # ─────────────────────────────────────────────────────────────────────

    def stop(self, account_id: int, reason: str = "manual") -> dict:
        with self._lock:
            proc = self._processes.get(account_id)

        if not proc:
            self.db.execute(
                "UPDATE accounts SET process_status='stopped', pid=NULL WHERE id=?",
                (account_id,),
            )
            return {"ok": True, "reason": "Not tracked as running (marked stopped)"}

        self.db.execute("UPDATE accounts SET process_status='stopping' WHERE id=?", (account_id,))

        try:
            proc.popen.terminate()   # cross-platform: SIGTERM on POSIX, WM_CLOSE on Windows
        except (ProcessLookupError, PermissionError):
            pass

        try:
            proc.popen.wait(timeout=GRACEFUL_STOP_TIMEOUT_SECS)
        except subprocess.TimeoutExpired:
            logger.warning(f"[Supervisor] Account {account_id} did not exit gracefully, force-killing")
            try:
                proc.popen.kill()    # cross-platform: SIGKILL on POSIX, TerminateProcess on Windows
            except (ProcessLookupError, PermissionError):
                pass

        with self._lock:
            self._processes.pop(account_id, None)

        self.db.execute(
            "UPDATE accounts SET status='disconnected', process_status='stopped', pid=NULL, last_error=? WHERE id=?",
            (f"stopped ({reason})", account_id),
        )
        logger.info(f"[Supervisor] Account {account_id} stopped ({reason})")
        return {"ok": True, "reason": None}

    # ─────────────────────────────────────────────────────────────────────
    # HEALTH CHECK LOOP
    # ─────────────────────────────────────────────────────────────────────

    def start_health_checks(self):
        if self._health_thread and self._health_thread.is_alive():
            return
        self._stop_event.clear()
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()
        logger.info("[Supervisor] Health check loop started")

    def stop_health_checks(self):
        self._stop_event.set()

    def _health_loop(self):
        while not self._stop_event.is_set():
            with self._lock:
                account_ids = list(self._processes.keys())

            for account_id in account_ids:
                self._check_one(account_id)

            self._stop_event.wait(HEALTH_CHECK_INTERVAL_SECS)

    def _check_one(self, account_id: int):
        with self._lock:
            proc = self._processes.get(account_id)
        if not proc:
            return

        exited = proc.popen.poll() is not None
        healthy = False
        if not exited:
            try:
                r = requests.get(f"http://127.0.0.1:{proc.port}/api/ping", timeout=3)
                healthy = (r.status_code == 200)
            except requests.exceptions.RequestException:
                healthy = False

        if healthy:
            proc.consecutive_failures = 0
            self.db.execute(
                "UPDATE accounts SET last_health_check=datetime('now') WHERE id=?",
                (account_id,),
            )
            return

        proc.consecutive_failures += 1
        logger.warning(
            f"[Supervisor] Account {account_id} unhealthy "
            f"(exited={exited}, consecutive_failures={proc.consecutive_failures})"
        )

        acc = self.db.get_account(account_id)
        restart_count = (acc.get("restart_count") or 0) if acc else 0

        if restart_count >= MAX_RESTART_ATTEMPTS:
            self.db.execute(
                "UPDATE accounts SET process_status='crashed', status='error', "
                "last_error='Max restart attempts exceeded — manual intervention required' WHERE id=?",
                (account_id,),
            )
            logger.error(f"[Supervisor] Account {account_id} exceeded max restarts, giving up")
            with self._lock:
                self._processes.pop(account_id, None)
            return

        # Backoff before restart, proportional to how many times we've retried
        backoff = RESTART_BACKOFF_BASE_SECS * (2 ** restart_count)
        logger.info(f"[Supervisor] Restarting account {account_id} in {backoff}s (attempt {restart_count + 1})")

        self.db.execute(
            "UPDATE accounts SET process_status='crashed', restart_count=restart_count+1 WHERE id=?",
            (account_id,),
        )
        with self._lock:
            self._processes.pop(account_id, None)

        threading.Timer(backoff, self._restart, args=(account_id,)).start()

    def _restart(self, account_id: int):
        acc = self.db.get_account(account_id)
        if not acc or acc.get("process_status") == "stopped":
            return   # was intentionally stopped while waiting on backoff
        self.launch(account_id)


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETON ACCESSOR — mirrors get_db_agent()'s pattern in quant_server.py
# ─────────────────────────────────────────────────────────────────────────────

_supervisor = None


def get_supervisor(db):
    global _supervisor
    if _supervisor is None:
        _supervisor = ProcessSupervisor(db)
        _supervisor.start_health_checks()
    return _supervisor
