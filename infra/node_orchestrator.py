"""
CRAVE v10.0 — Node Orchestrator
=================================
Manages the three-node failover system.
Decides which node is active and triggers handoffs.

PRIORITY:
  Laptop (primary) > Phone (secondary) > AWS (tertiary)

FAILOVER TRIGGERS:
  Laptop goes offline    → phone takes over
  Phone overheats        → AWS takes over
  All nodes unavailable  → Telegram alert, positions stay open with broker SL

HEARTBEAT:
  Each node sends a heartbeat to the State file every 30 seconds.
  If no heartbeat for 2 minutes, that node is considered offline.
  AWS t3.micro watches heartbeats and starts t3.small if needed.

USAGE:
  from infra.node_orchestrator import orchestrator

  orchestrator.start()
  orchestrator.get_active_node()    # → "laptop" / "phone" / "aws"
  orchestrator.is_active()          # → True if THIS node is active
  orchestrator.trigger_failover(from_node, reason)
  orchestrator.request_switch("phone")
"""

import os
import json
import socket
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crave.orchestrator")


class NodeOrchestrator:

    HEARTBEAT_INTERVAL_SECS = 30
    HEARTBEAT_TIMEOUT_SECS  = 120   # node considered offline after 2 min silence

    def __init__(self):
        from config.config import NODES, STATE_DIR
        self._nodes_cfg    = NODES
        self._heartbeat_file = STATE_DIR / "crave_heartbeats.json"
        self._active_file    = STATE_DIR / "crave_active_node.json"

        self._my_node        = self._detect_node()
        self._is_active      = False
        self._running        = False
        self._switch_request: Optional[str] = None

        self._heartbeats: dict = {}
        self._load_heartbeats()

        logger.info(f"[Orchestrator] This node: {self._my_node}")

    # ─────────────────────────────────────────────────────────────────────────
    # NODE DETECTION
    # ─────────────────────────────────────────────────────────────────────────

    def _detect_node(self) -> str:
        from config.config import NODES
        hostname = socket.gethostname().upper()
        for name, cfg in NODES.items():
            if any(p.upper() in hostname for p in cfg.get("hostname_patterns", [])):
                return name
        return "aws"

    # ─────────────────────────────────────────────────────────────────────────
    # HEARTBEAT MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    def _load_heartbeats(self):
        if self._heartbeat_file.exists():
            try:
                with open(self._heartbeat_file) as f:
                    self._heartbeats = json.load(f)
            except Exception as e:
                logger.debug(f"[Orchestrator] Heartbeat read failed, keeping in-memory state. Error: {e}")
                if not self._heartbeats:
                    self._heartbeats = {}

    def _save_heartbeats(self):
        try:
            with open(self._heartbeat_file, "w") as f:
                json.dump(self._heartbeats, f, indent=2)
        except Exception as e:
            logger.warning(f"[Orchestrator] Heartbeat save failed: {e}")

    def _send_heartbeat(self):
        """Register this node's heartbeat."""
        now = datetime.now(timezone.utc).isoformat()
        self._heartbeats[self._my_node] = {
            "last_seen":  now,
            "is_active":  self._is_active,
            "hostname":   socket.gethostname(),
        }
        self._save_heartbeats()

    def _is_node_alive(self, node_name: str) -> bool:
        """Check if a node has sent a heartbeat recently."""
        hb = self._heartbeats.get(node_name)
        if not hb:
            return False
        try:
            last = datetime.fromisoformat(hb["last_seen"])
            age  = (datetime.now(timezone.utc) - last).total_seconds()
            return age < self.HEARTBEAT_TIMEOUT_SECS
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # ACTIVE NODE ELECTION
    # ─────────────────────────────────────────────────────────────────────────

    def _elect_active_node(self) -> str:
        """
        Elect the highest-priority available node.
        Priority: laptop > phone > aws
        Also respects thermal state for phone.
        """
        priority_order = ["laptop", "phone", "aws"]

        for node_name in priority_order:
            # Check if node is alive
            if not self._is_node_alive(node_name):
                continue

            # Extra check for phone: thermal state
            if node_name == "phone":
                try:
                    from infra.thermal_monitor import thermal
                    if not thermal.is_safe():
                        logger.info(
                            "[Orchestrator] Phone alive but too hot. Skipping."
                        )
                        continue
                except Exception:
                    pass   # thermal module may not be loaded on this node

            return node_name

        # No node available
        return "none"

    def get_active_node(self) -> str:
        """Return the currently elected active node."""
        self._load_heartbeats()
        return self._elect_active_node()

    def is_active(self) -> bool:
        """Is THIS node the currently active node?"""
        return self.get_active_node() == self._my_node

    # ─────────────────────────────────────────────────────────────────────────
    # FAILOVER
    # ─────────────────────────────────────────────────────────────────────────

    # How long to keep rechecking for a node (e.g. AWS finishing its boot)
    # before declaring a real crisis. AWS heartbeats can't possibly exist
    # yet at the instant trigger_failover() is first called -- HEARTBEAT_
    # TIMEOUT_SECS alone guarantees that. This window gives the instance a
    # realistic chance to come up before we page anyone.
    FAILOVER_GRACE_SECS = 150
    FAILOVER_POLL_INTERVAL_SECS = 10

    def trigger_failover(self, from_node: str, reason: str):
        """
        Trigger an immediate failover away from from_node.
        Called by thermal_monitor or detected by heartbeat timeout.

        IMPORTANT: laptop_sleep_watcher.py calls this synchronously from a
        Windows suspend-event hook with a very small time budget -- it must
        return almost instantly. All work below is either a fast local
        heartbeat-file check or delegated to a background daemon thread;
        nothing here blocks waiting on a network call or a sleep().
        """
        # Mark the failing node as stale by zeroing its heartbeat
        if from_node in self._heartbeats:
            self._heartbeats[from_node]["last_seen"] = "2000-01-01T00:00:00+00:00"
            self._save_heartbeats()

        new_active = self._elect_active_node()

        # FIX: log the actual elected node (or "none"), not a literal "?"
        logger.warning(
            f"[Orchestrator] FAILOVER: {from_node} → {new_active} | Reason: {reason}"
        )

        # Notify
        try:
            from interfaces.telegram_interface import tg
            tg.send_node_failover(from_node, new_active, reason)
        except Exception:
            pass

        # FIX: don't fire the CRITICAL "manual intervention required" alert
        # off the very first election -- an AWS instance just told to boot
        # cannot have a heartbeat yet, so this branch previously fired on
        # essentially every laptop sleep/reboot, even ones that self-
        # resolved within a couple minutes with no real gap in coverage.
        # Hand the escalation off to a bounded background poll instead.
        if new_active == "none":
            logger.warning(
                "[Orchestrator] No node active immediately after failover "
                f"from {from_node} — watching for up to "
                f"{self.FAILOVER_GRACE_SECS}s before escalating."
            )
            import threading
            threading.Thread(
                target=self._watch_for_recovery,
                args=(from_node, reason),
                daemon=True,
            ).start()

        return new_active

    def _watch_for_recovery(self, from_node: str, reason: str):
        """
        Background poll after a failed immediate election. Runs off the
        time-critical suspend-hook path. If a node comes up within the
        grace window, log it and stop quietly (no false alarm was ever
        sent). If the window elapses with still no node, THEN send the
        CRITICAL alert -- this is now an accurate signal, not a race
        against AWS's own boot time.
        """
        import time
        elapsed = 0
        while elapsed < self.FAILOVER_GRACE_SECS:
            time.sleep(self.FAILOVER_POLL_INTERVAL_SECS)
            elapsed += self.FAILOVER_POLL_INTERVAL_SECS
            if self._elect_active_node() != "none":
                logger.info(
                    f"[Orchestrator] Recovered {elapsed}s after failover "
                    f"from {from_node} — node now active, no alert needed."
                )
                return

        msg = (
            "🚨 <b>ALL NODES OFFLINE</b>\n"
            f"No active node for {self.FAILOVER_GRACE_SECS}s after "
            f"failover from {from_node} ({reason}).\n"
            "Open positions protected by broker SL only.\n"
            "Manual intervention required."
        )
        logger.critical(msg.replace("<b>", "").replace("</b>", ""))
        try:
            from interfaces.telegram_interface import tg
            tg.send(msg)
        except Exception:
            pass

    def request_switch(self, target_node: str):
        """Request a manual node switch (from Telegram /switch command)."""
        valid = list(self._nodes_cfg.keys())
        if target_node not in valid:
            logger.warning(f"[Orchestrator] Invalid node: {target_node}")
            return
        self._switch_request = target_node
        logger.info(f"[Orchestrator] Manual switch to {target_node} requested.")

    def request_aws_standby(self):
        """
        Tell AWS to spin up t3.small instance and warm up.
        Called when phone temperature enters HOT zone.
        """
        try:
            from infra.aws_manager import aws
            aws.start_instance()
            logger.info("[Orchestrator] AWS standby requested due to phone temp.")
        except Exception as e:
            logger.warning(f"[Orchestrator] AWS standby request failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        """Start orchestrator in background thread."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._orchestrator_loop,
            daemon=True,
            name="CRAVEOrchestrator"
        )
        self._thread.start()

        # Start state sync
        try:
            from infra.state_sync import sync
            sync.start(is_active=self.is_active())
        except Exception as e:
            logger.warning(f"[Orchestrator] State sync start failed: {e}")

        logger.info(
            f"[Orchestrator] Started. Node={self._my_node} | "
            f"Active={'YES' if self.is_active() else 'NO (standby)'}"
        )

    def stop(self):
        self._running = False

    def _orchestrator_loop(self):
        """
        Main orchestration loop:
        1. Send heartbeat every 30s
        2. Check if we should become active (primary just went offline)
        3. Check if we should step down (higher-priority node came online)
        4. Handle manual switch requests
        """
        last_active_check = 0
        last_notify_time  = 0   # FIX: cooldown to prevent rapid notifications

        # FIX: Initialize was_active from actual election to avoid false
        # state-change notification on first iteration
        self._send_heartbeat()
        self._load_heartbeats()
        elected = self._elect_active_node()
        self._is_active = (elected == self._my_node)
        was_active      = self._is_active

        while self._running:
            now = time.time()

            # Always send heartbeat
            self._send_heartbeat()

            # Check active node status every 30s
            if now - last_active_check >= 30:
                last_active_check = now
                self._load_heartbeats()

                # Handle manual switch request
                if self._switch_request:
                    if self._switch_request == self._my_node:
                        # Force ourselves active
                        self._is_active = True
                        self._switch_request = None
                        logger.info(f"[Orchestrator] Manual switch: now active.")
                    else:
                        # Force ourselves inactive
                        self._is_active = False
                        self._switch_request = None
                        logger.info(f"[Orchestrator] Manual switch: stepped down.")

                else:
                    # Normal election
                    elected = self._elect_active_node()
                    should_be_active = (elected == self._my_node)

                    if should_be_active and not self._is_active:
                        self._is_active = True
                        logger.info(f"[Orchestrator] This node elected as ACTIVE.")
                        try:
                            from infra.state_sync import sync
                            sync.set_active(True)
                        except Exception:
                            pass
                        
                        # Auto-stop AWS when laptop takes back control
                        if self._my_node == "laptop":
                            try:
                                from infra.aws_manager import get_aws
                                get_aws().stop_instance()
                                logger.info("[Orchestrator] Automatically stopped AWS instance.")
                                from interfaces.telegram_interface import tg
                                tg.send("☁️ AWS Instance Stopped\nPrimary node has resumed.\nCredits saved. ✅")
                            except Exception as e:
                                logger.warning(f"[Orchestrator] Failed to stop AWS automatically: {e}")

                    elif not should_be_active and self._is_active:
                        self._is_active = False
                        logger.info(
                            f"[Orchestrator] Stepping down. "
                            f"Higher node active: {elected}"
                        )
                        try:
                            from infra.state_sync import sync
                            sync.set_active(False)
                        except Exception:
                            pass

                # Notify on state change (with 60s cooldown to prevent spam)
                if was_active != self._is_active and (now - last_notify_time) > 60:
                    was_active = self._is_active
                    last_notify_time = now
                    state = "ACTIVE" if self._is_active else "STANDBY"
                    logger.info(f"[Orchestrator] Node state → {state}")
                    try:
                        from interfaces.telegram_interface import tg
                        tg.send(
                            f"📡 Node {self._my_node} → {state}"
                        )
                    except Exception:
                        pass
                elif was_active != self._is_active:
                    # Update was_active even if we skip notification
                    was_active = self._is_active

            time.sleep(self.HEARTBEAT_INTERVAL_SECS)

    # ─────────────────────────────────────────────────────────────────────────
    # STATUS
    # ─────────────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        self._load_heartbeats()
        nodes_status = {}
        for node_name in self._nodes_cfg:
            hb    = self._heartbeats.get(node_name, {})
            alive = self._is_node_alive(node_name)
            nodes_status[node_name] = {
                "alive":     alive,
                "last_seen": hb.get("last_seen", "never"),
                "is_active": hb.get("is_active", False),
            }
        return {
            "my_node":    self._my_node,
            "is_active":  self._is_active,
            "elected":    self._elect_active_node(),
            "nodes":      nodes_status,
        }

    def get_status_message(self) -> str:
        s = self.get_status()
        lines = [
            "📡 <b>NODE STATUS</b>",
            f"This node : {s['my_node']} ({'ACTIVE' if s['is_active'] else 'standby'})",
            f"Elected   : {s['elected']}",
            "━━━━━━━━━━━━━━━",
        ]
        for name, info in s["nodes"].items():
            alive  = "✅" if info["alive"] else "❌"
            active = " ← ACTIVE" if info["is_active"] else ""
            seen   = info["last_seen"][:16] if info["last_seen"] != "never" else "never"
            lines.append(f"{alive} {name}{active} | last: {seen}")

        # Add phone temp if available
        try:
            from infra.thermal_monitor import thermal
            lines.append(f"\n{thermal.get_status_line()}")
        except Exception:
            pass

        return "\n".join(lines)


# ── Singleton ─────────────────────────────────────────────────────────────────
orchestrator = NodeOrchestrator()
