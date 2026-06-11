"""
CRAVE v10.0 — State Sync
==========================
Syncs state files across laptop, phone, and AWS using a private
GitHub repository as the sync layer.

WHY GITHUB:
  Free, always available, gives version history (audit trail),
  no central server needed, works from any network.

HOW IT WORKS:
  Active node:  pushes state every 60 seconds
  Standby nodes: pull every 30 seconds
  On failover: new active node pulls latest, resumes in <30 seconds

SETUP:
  1. Create a private GitHub repo (e.g., "crave-state")
  2. Create a Personal Access Token with "repo" scope
  3. Set environment variables:
       CRAVE_STATE_REPO=https://github.com/USERNAME/crave-state.git
       GITHUB_TOKEN=ghp_xxxxxxxxxxxx
  4. Run: python -c "from infra.state_sync import sync; sync.setup()"

USAGE:
  from infra.state_sync import sync
  
  sync.start()     # Start background sync thread
  sync.push()      # Force immediate push
  sync.pull()      # Force immediate pull
  sync.stop()      # Stop sync thread
"""

import os
import time
import logging
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.sync")


class StateSyncManager:

    def __init__(self):
        from config.config import STATE_SYNC, ROOT_DIR
        self.cfg      = STATE_SYNC
        self.root_dir = ROOT_DIR
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._is_active_node = False  # set by NodeOrchestrator

        self.repo_url = os.environ.get(self.cfg["repo_env_var"], "")
        self.token    = os.environ.get(self.cfg["token_env_var"], "")

    # ─────────────────────────────────────────────────────────────────────────
    # SETUP (run once)
    # ─────────────────────────────────────────────────────────────────────────

    def setup(self):
        """
        One-time setup: initialise git state branch if it doesn't exist.
        Run this manually after setting CRAVE_STATE_REPO and GITHUB_TOKEN.
        """
        if not self.repo_url or not self.token:
            logger.warning(
                "[Sync] CRAVE_STATE_REPO or GITHUB_TOKEN not set. "
                "State sync disabled. Set these in your .env file."
            )
            return False

        try:
            # Check if state branch exists remotely
            result = self._git(["ls-remote", "--heads", "origin", "state"])
            if "state" not in result:
                # Create and push state branch
                self._git(["checkout", "--orphan", "state"])
                self._git(["rm", "-rf", "."])

                # Create minimal README
                readme = self.root_dir / "State" / "README.md"
                readme.write_text("# CRAVE State Files\nAuto-managed. Do not edit manually.")
                self._git(["add", "State/README.md"])
                self._git(["commit", "-m", "init: state branch"])
                self._git(["push", "-u", "origin", "state"])
                self._git(["checkout", "main"])
                logger.info("[Sync] State branch created on GitHub.")
            else:
                logger.info("[Sync] State branch already exists.")

            return True
        except Exception as e:
            logger.error(f"[Sync] Setup failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # PUSH / PULL
    # ─────────────────────────────────────────────────────────────────────────

    def push(self) -> bool:
        """Push current state files to GitHub."""
        if not self._sync_configured():
            return False

        try:
            files_to_add = self.cfg["files_to_sync"] + self.cfg["files_to_sync_slow"]
            existing     = [f for f in files_to_add
                            if (self.root_dir / f).exists()]

            if not existing:
                return True  # Nothing to push yet

            self._git(["add"] + existing)

            # Only commit if there are actual changes
            status = self._git(["status", "--porcelain"])
            if not status.strip():
                return True  # Nothing changed

            node   = self._get_node_name()
            ts     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            commit = f"state: {node} @ {ts}"
            self._git(["commit", "-m", commit])
            self._git(["push", "origin", self.cfg["branch"]])
            logger.debug(f"[Sync] Pushed state: {commit}")
            return True

        except Exception as e:
            logger.warning(f"[Sync] Push failed: {e}")
            return False

    def pull(self) -> bool:
        """Pull latest state from GitHub."""
        if not self._sync_configured():
            return False

        try:
            self._git(["fetch", "origin", self.cfg["branch"]])
            self._git(["checkout", f"origin/{self.cfg['branch']}", "--",
                       "State/"])
            logger.debug("[Sync] Pulled latest state.")
            return True
        except Exception as e:
            logger.warning(f"[Sync] Pull failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND THREAD
    # ─────────────────────────────────────────────────────────────────────────

    def start(self, is_active: bool = False):
        """
        Start background sync thread.
        is_active: True = this node pushes, False = this node pulls only.
        """
        self._is_active_node = is_active
        if self._thread and self._thread.is_alive():
            return

        self._running = True
        self._thread  = threading.Thread(
            target=self._sync_loop,
            daemon=True,
            name="CRAVEStateSync"
        )
        self._thread.start()
        mode = "PUSH (active)" if is_active else "PULL (standby)"
        logger.info(f"[Sync] Started in {mode} mode.")

    def stop(self):
        self._running = False
        logger.info("[Sync] Stopped.")

    def set_active(self, is_active: bool):
        """Switch between push (active) and pull (standby) mode."""
        self._is_active_node = is_active
        mode = "PUSH (active)" if is_active else "PULL (standby)"
        logger.info(f"[Sync] Mode changed to {mode}.")

    def _sync_loop(self):
        push_interval = self.cfg["sync_interval_secs"]     # 60s
        pull_interval = self.cfg["sync_interval_secs"] // 2  # 30s

        last_push = 0
        last_pull = 0

        while self._running:
            now = time.time()

            if self._is_active_node:
                if now - last_push >= push_interval:
                    self.push()
                    last_push = now
            else:
                if now - last_pull >= pull_interval:
                    self.pull()
                    last_pull = now

            time.sleep(5)

    # ─────────────────────────────────────────────────────────────────────────
    # GIT HELPER
    # ─────────────────────────────────────────────────────────────────────────

    def _git(self, args: list) -> str:
        """Run a git command in the project root directory."""
        # Inject token into remote URL for authentication
        env = os.environ.copy()
        if self.token:
            # Git credential helper via environment
            env["GIT_ASKPASS"] = "echo"
            env["GIT_USERNAME"] = "x-token"
            env["GIT_PASSWORD"] = self.token

        result = subprocess.run(
            ["git"] + args,
            cwd=str(self.root_dir),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        if result.returncode != 0 and result.stderr:
            # Not all git operations on clean state are errors
            if "nothing to commit" not in result.stderr.lower():
                logger.debug(f"[Sync] git {' '.join(args)}: {result.stderr.strip()}")

        return result.stdout + result.stderr

    def _sync_configured(self) -> bool:
        if not self.cfg.get("enabled", False):
            return False
        if not self.repo_url:
            logger.debug("[Sync] CRAVE_STATE_REPO not set — sync disabled.")
            return False
        return True

    def _get_node_name(self) -> str:
        import socket
        hostname = socket.gethostname().upper()
        from config.config import NODES
        for name, cfg in NODES.items():
            if any(p in hostname for p in cfg["hostname_patterns"]):
                return name
        return "unknown"

    def get_status(self) -> dict:
        return {
            "enabled":       self.cfg.get("enabled", False),
            "configured":    self._sync_configured(),
            "running":       self._running,
            "is_active_node": self._is_active_node,
            "mode":          "PUSH" if self._is_active_node else "PULL",
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
sync = StateSyncManager()
