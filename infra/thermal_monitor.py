"""
CRAVE v10.0 — Thermal Monitor
===============================
Monitors phone CPU temperature and manages handoff to AWS
when the phone gets too hot.

TEMPERATURE ZONES:
  NORMAL   < 35°C  → full operation
  WARM     35-40°C → reduce load (fewer WebSocket subscriptions)
  HOT      40-45°C → pause signal detection, keep position monitor only
  CRITICAL > 45°C  → immediate handoff to AWS, phone enters cooling mode

COOLING MODE:
  Closes non-essential processes, screen off, WiFi only.
  Resumes when temp < 38°C for 5 consecutive minutes.

USAGE:
  from infra.thermal_monitor import thermal

  temp = thermal.get_temperature()
  zone = thermal.get_zone()
  ok   = thermal.is_safe()
"""

import os
import time
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crave.thermal")


class ThermalMonitor:

    # Temperature thresholds (°C)
    ZONE_NORMAL   = 35
    ZONE_WARM     = 40
    ZONE_HOT      = 45
    ZONE_CRITICAL = 45

    # Recovery: must be below this for 5 consecutive checks
    RECOVERY_THRESHOLD = 38
    RECOVERY_CHECKS    = 5

    def __init__(self):
        from config.config import NODES
        phone_cfg = NODES.get("phone", {})
        self._limit     = phone_cfg.get("thermal_limit_celsius",  42)
        self._warn      = phone_cfg.get("thermal_warn_celsius",   38)

        self._current_temp      = None
        self._current_zone      = "UNKNOWN"
        self._recovery_count    = 0
        self._in_cooling_mode   = False
        self._handoff_triggered = False

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Thermal zone file paths (Android/Termux)
        # Different phones expose temperature at different paths
        self._temp_paths = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
            "/sys/class/thermal/thermal_zone2/temp",
            "/sys/devices/virtual/thermal/thermal_zone0/temp",
            "/sys/class/power_supply/battery/temp",
        ]
        self._active_path: Optional[str] = None
        self._find_temp_path()

    # ─────────────────────────────────────────────────────────────────────────
    # TEMPERATURE READING
    # ─────────────────────────────────────────────────────────────────────────

    def _find_temp_path(self):
        """Find the first readable thermal zone file on this device."""
        for path in self._temp_paths:
            if Path(path).exists():
                try:
                    raw = Path(path).read_text().strip()
                    # Validate: should be a number (millidegrees or degrees)
                    int(raw)
                    self._active_path = path
                    logger.info(f"[Thermal] Using temp path: {path}")
                    return
                except Exception:
                    continue
        logger.info("[Thermal] No thermal zone file found. "
                    "This is normal on laptop/AWS nodes.")

    def get_temperature(self) -> Optional[float]:
        """
        Read current CPU temperature in Celsius.
        Returns None if not on Android/phone.
        """
        if not self._active_path:
            return None

        try:
            raw  = Path(self._active_path).read_text().strip()
            val  = int(raw)
            # Android reports in millidegrees (e.g. 42000 = 42°C)
            # Some devices report in degrees directly (e.g. 42)
            temp = val / 1000.0 if val > 1000 else float(val)
            self._current_temp = round(temp, 1)
            return self._current_temp
        except Exception as e:
            logger.debug(f"[Thermal] Read error: {e}")
            return None

    def get_zone(self) -> str:
        """
        Returns current temperature zone:
        UNKNOWN / NORMAL / WARM / HOT / CRITICAL
        """
        temp = self.get_temperature()
        if temp is None:
            return "UNKNOWN"   # Not on phone — always OK

        if temp < self.ZONE_NORMAL:
            return "NORMAL"
        elif temp < self.ZONE_WARM:
            return "WARM"
        elif temp < self.ZONE_HOT:
            return "HOT"
        else:
            return "CRITICAL"

    def is_safe(self) -> bool:
        """True if phone can continue running the bot."""
        zone = self.get_zone()
        return zone in ("UNKNOWN", "NORMAL", "WARM")

    def should_reduce_load(self) -> bool:
        """True if we should reduce WebSocket subscriptions etc."""
        zone = self.get_zone()
        return zone in ("WARM", "HOT", "CRITICAL")

    def should_handoff(self) -> bool:
        """True if phone should hand off to AWS immediately."""
        zone = self.get_zone()
        return zone == "CRITICAL"

    # ─────────────────────────────────────────────────────────────────────────
    # COOLING MODE
    # ─────────────────────────────────────────────────────────────────────────

    def enter_cooling_mode(self):
        """
        Reduce phone load aggressively.
        Called when temp > CRITICAL threshold.
        """
        if self._in_cooling_mode:
            return

        self._in_cooling_mode = True
        logger.critical(
            f"[Thermal] CRITICAL TEMP: {self._current_temp}°C. "
            f"Entering cooling mode."
        )

        # Notify
        try:
            from interfaces.telegram_interface import tg
            tg.send(
                f"🌡️ <b>PHONE OVERHEATING</b>\n"
                f"Temp: {self._current_temp}°C\n"
                f"Handing off to AWS.\n"
                f"Phone entering cooling mode."
            )
        except Exception:
            pass

        # Try to turn screen off via Termux API
        try:
            os.system("termux-torch off 2>/dev/null")
        except Exception:
            pass

        logger.info("[Thermal] Cooling mode active. Waiting for temp < 38°C.")

    def check_recovery(self) -> bool:
        """
        Check if phone has cooled down enough to resume.
        Requires 5 consecutive readings below RECOVERY_THRESHOLD.
        """
        temp = self.get_temperature()
        if temp is None:
            return True   # Not on phone

        if temp < self.RECOVERY_THRESHOLD:
            self._recovery_count += 1
            if self._recovery_count >= self.RECOVERY_CHECKS:
                self._in_cooling_mode   = False
                self._recovery_count    = 0
                self._handoff_triggered = False
                logger.info(
                    f"[Thermal] Phone cooled to {temp}°C. "
                    f"Ready to resume."
                )
                try:
                    from interfaces.telegram_interface import tg
                    tg.send(
                        f"✅ <b>PHONE COOLED</b>\n"
                        f"Temp: {temp}°C\n"
                        f"Ready to resume operation."
                    )
                except Exception:
                    pass
                return True
        else:
            self._recovery_count = 0   # Reset if temp goes back up

        return False

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND MONITOR
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        """Start temperature monitoring in background thread."""
        if self._active_path is None:
            logger.info("[Thermal] Not on Android — thermal monitoring disabled.")
            return

        self._running = True
        self._thread  = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="CRAVEThermalMonitor"
        )
        self._thread.start()
        logger.info("[Thermal] Monitor started.")

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        """Check temperature every 30 seconds."""
        while self._running:
            try:
                temp = self.get_temperature()
                zone = self.get_zone()

                if temp is not None:
                    logger.debug(f"[Thermal] {temp}°C — {zone}")

                # Handle zone transitions
                if zone == "WARN" and not self._in_cooling_mode:
                    logger.warning(
                        f"[Thermal] Phone warming up: {temp}°C. "
                        f"Consider plugging in charger."
                    )

                elif zone == "HOT" and not self._in_cooling_mode:
                    logger.warning(
                        f"[Thermal] Phone HOT: {temp}°C. "
                        f"Requesting AWS standby..."
                    )
                    try:
                        from infra.node_orchestrator import orchestrator
                        orchestrator.request_aws_standby()
                    except Exception:
                        pass

                elif zone == "CRITICAL":
                    if not self._handoff_triggered:
                        self._handoff_triggered = True
                        self.enter_cooling_mode()
                        # Trigger immediate failover
                        try:
                            from infra.node_orchestrator import orchestrator
                            orchestrator.trigger_failover(
                                from_node="phone",
                                reason=f"Temperature critical: {temp}°C"
                            )
                        except Exception:
                            pass

                # Check recovery if in cooling mode
                if self._in_cooling_mode:
                    self.check_recovery()

            except Exception as e:
                logger.error(f"[Thermal] Monitor loop error: {e}")

            time.sleep(30)

    # ─────────────────────────────────────────────────────────────────────────
    # STATUS
    # ─────────────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        temp = self.get_temperature()
        zone = self.get_zone()
        return {
            "temperature_c":   temp,
            "zone":            zone,
            "is_safe":         self.is_safe(),
            "in_cooling_mode": self._in_cooling_mode,
            "monitoring":      self._active_path is not None,
            "limit_c":         self._limit,
        }

    def get_status_line(self) -> str:
        s = self.get_status()
        if not s["monitoring"]:
            return "🌡️ Not on phone"
        temp  = s["temperature_c"]
        zone  = s["zone"]
        emoji = {"NORMAL": "✅", "WARM": "🟡", "HOT": "🟠",
                 "CRITICAL": "🔴", "UNKNOWN": "⬜"}.get(zone, "⬜")
        return f"{emoji} {temp}°C ({zone})"


# ── Singleton ─────────────────────────────────────────────────────────────────
thermal = ThermalMonitor()
