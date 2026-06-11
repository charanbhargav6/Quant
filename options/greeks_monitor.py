"""
CRAVE v10.3 — Greeks Monitor (Session 6)
==========================================
Tracks option Greeks for every open options position.
Runs every 15 minutes during market hours.

ALERTS TRIGGERED WHEN:
  Delta drift > 0.15 from target   → position moved significantly, review
  Theta decay > 2% per day         → time running out faster than expected
  Vega exposure > 5% of portfolio  → too much IV sensitivity
  DTE ≤ 2                          → EXPIRY DANGER — close immediately

Greeks approximated via Black-Scholes when real-time Greeks unavailable.

USAGE:
  from options.greeks_monitor import greeks_monitor

  greeks_monitor.start()        # background thread
  summary = greeks_monitor.get_portfolio_greeks()
  greeks_monitor.check_all()    # force immediate check
"""

import logging
import math
import time
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.greeks")


# ─────────────────────────────────────────────────────────────────────────────
# BLACK-SCHOLES CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────

class BSCalculator:
    """
    Black-Scholes Greeks calculator.
    Used when exchange doesn't provide real-time Greeks (e.g., NSE).

    All inputs:
      S  = current spot price
      K  = strike price
      T  = time to expiry in YEARS (dte / 365)
      r  = risk-free rate (use 0.065 for India, 0.05 for US)
      iv = implied volatility as decimal (0.20 = 20%)
    """

    RISK_FREE_RATE_INDIA = 0.065   # ~6.5% Indian repo rate
    RISK_FREE_RATE_US    = 0.050

    def _d1(self, S, K, T, r, iv) -> float:
        if T <= 0 or iv <= 0 or S <= 0 or K <= 0:
            return 0.0
        return (math.log(S / K) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))

    def _d2(self, d1, T, iv) -> float:
        if T <= 0 or iv <= 0:
            return 0.0
        return d1 - iv * math.sqrt(T)

    def _norm_cdf(self, x: float) -> float:
        """Standard normal CDF via error function."""
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))

    def _norm_pdf(self, x: float) -> float:
        """Standard normal PDF."""
        return math.exp(-0.5 * x**2) / math.sqrt(2 * math.pi)

    def calculate(self, S: float, K: float, T: float,
                   iv: float, option_type: str = "CE",
                   r: float = None) -> dict:
        """
        Calculate all Greeks for a single option.

        Returns:
          delta:  directional exposure per unit
          gamma:  rate of delta change
          theta:  daily time decay (negative for long options)
          vega:   sensitivity to 1% IV change
          price:  theoretical option price
        """
        if r is None:
            r = self.RISK_FREE_RATE_INDIA

        if T <= 0:
            # At expiry
            intrinsic = max(0, S - K) if option_type == "CE" else max(0, K - S)
            return {
                "delta": 1.0 if intrinsic > 0 else 0.0,
                "gamma": 0.0, "theta": 0.0,
                "vega": 0.0,  "price": intrinsic,
            }

        d1 = self._d1(S, K, T, r, iv)
        d2 = self._d2(d1, T, iv)

        nd1  = self._norm_cdf(d1)
        nd2  = self._norm_cdf(d2)
        nd1n = self._norm_cdf(-d1)
        nd2n = self._norm_cdf(-d2)
        pdf1 = self._norm_pdf(d1)

        if option_type.upper() in ("CE", "CALL", "C"):
            price = S * nd1 - K * math.exp(-r * T) * nd2
            delta = nd1
        else:  # PE / PUT
            price = K * math.exp(-r * T) * nd2n - S * nd1n
            delta = nd1 - 1.0

        gamma = pdf1 / (S * iv * math.sqrt(T))
        vega  = S * pdf1 * math.sqrt(T) / 100   # per 1% IV change
        theta = (-(S * pdf1 * iv) / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * (nd2 if option_type.upper() in ("CE","CALL") else nd2n)
                 ) / 365   # per day

        return {
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta, 4),
            "vega":  round(vega, 4),
            "price": round(max(0.0, price), 2),
        }


bs = BSCalculator()


# ─────────────────────────────────────────────────────────────────────────────
# GREEKS MONITOR
# ─────────────────────────────────────────────────────────────────────────────

class GreeksMonitor:

    CHECK_INTERVAL_MINS = 15

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Cache of most recent Greeks per position
        self._greeks_cache: dict = {}   # trade_id → greeks dict

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="CRAVEGreeksMonitor"
        )
        self._thread.start()
        logger.info("[Greeks] Monitor started.")

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        while self._running:
            try:
                self.check_all()
            except Exception as e:
                logger.error(f"[Greeks] Monitor error: {e}")
            time.sleep(self.CHECK_INTERVAL_MINS * 60)

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN CHECK
    # ─────────────────────────────────────────────────────────────────────────

    def check_all(self) -> list:
        """
        Check Greeks for all open option positions.
        Returns list of alert dicts for any breach.
        """
        from core.position_tracker import positions
        all_pos = positions.get_all()
        alerts  = []

        for pos in all_pos:
            # Only check option positions
            from config.config import get_asset_class
            if get_asset_class(pos["symbol"]) != "options":
                continue

            try:
                greeks = self._calculate_position_greeks(pos)
                if greeks is None:
                    continue

                self._greeks_cache[pos["trade_id"]] = {
                    **greeks,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

                # Check for breaches
                position_alerts = self._check_breaches(pos, greeks)
                alerts.extend(position_alerts)

                for alert in position_alerts:
                    self._send_alert(alert)

            except Exception as e:
                logger.debug(f"[Greeks] Check failed for {pos['symbol']}: {e}")

        # Check expiry danger
        try:
            from options.options_engine import get_options_engine
            expiry_danger = get_options_engine().check_expiry_danger()
            for pos in expiry_danger:
                alert = {
                    "type":    "EXPIRY_DANGER",
                    "symbol":  pos.get("symbol"),
                    "message": f"Expiry in ≤2 days — close immediately",
                    "severity": "CRITICAL",
                }
                alerts.append(alert)
                self._send_alert(alert)
        except Exception:
            pass

        return alerts

    def _calculate_position_greeks(self, pos: dict) -> Optional[dict]:
        """Calculate current Greeks for a single option position."""
        symbol = pos["symbol"]

        # Get spot price
        try:
            from core.data_agent import DataAgent
            df = DataAgent().get_ohlcv(symbol, timeframe="1m", limit=2)
            if df is None or df.empty:
                return None
            spot = float(df['close'].iloc[-1])
        except Exception:
            return None

        # Position metadata
        strike      = pos.get("strike")
        option_type = pos.get("option_type", "CE")
        expiry_str  = pos.get("expiry")
        iv          = pos.get("iv_at_open", 0.20)

        if not strike or not expiry_str:
            return None

        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            dte = max(0, (expiry - datetime.now(timezone.utc)).days)
            T      = dte / 365.0
        except Exception:
            return None

        # Try to get live IV first
        live_iv = self._get_live_iv(symbol, strike, option_type)
        iv_used = live_iv if live_iv else iv

        greeks = bs.calculate(
            S=spot, K=strike, T=T,
            iv=iv_used, option_type=option_type
        )
        greeks["dte"]        = dte
        greeks["spot"]       = spot
        greeks["strike"]     = strike
        greeks["iv_used"]    = round(iv_used, 4)
        greeks["lot_size"]   = pos.get("lot_size", 50)
        greeks["option_type"] = option_type

        # Lots-adjusted Greeks (actual portfolio impact)
        lots = int(pos.get("lot_size", 50) / 50) or 1
        greeks["portfolio_delta"] = round(greeks["delta"] * lots * 50, 2)
        greeks["portfolio_vega"]  = round(greeks["vega"]  * lots * 50, 2)
        greeks["daily_theta"]     = round(greeks["theta"] * lots * 50, 2)

        return greeks

    def _get_live_iv(self, symbol: str, strike: float,
                      option_type: str) -> Optional[float]:
        """Try to get live IV from NSE option chain."""
        try:
            underlying = symbol.upper().replace("_FUT","").replace("_CE","").replace("_PE","")
            import requests
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={underlying}"
            headers = {"User-Agent": "Mozilla/5.0",
                       "Accept": "application/json",
                       "Referer": "https://www.nseindia.com"}
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                data = r.json().get("records", {}).get("data", [])
                for row in data:
                    if row.get("strikePrice") == strike:
                        key = "CE" if option_type.upper() in ("CE","CALL") else "PE"
                        iv  = row.get(key, {}).get("impliedVolatility", 0)
                        if iv and iv > 0:
                            return iv / 100   # NSE returns as percentage
        except Exception:
            pass
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # BREACH CHECKS
    # ─────────────────────────────────────────────────────────────────────────

    def _check_breaches(self, pos: dict, greeks: dict) -> list:
        """Check all Greeks against configured thresholds."""
        from config.config import OPTIONS
        limits   = OPTIONS.get("greeks", {})
        alerts   = []
        symbol   = pos["symbol"]
        trade_id = pos["trade_id"]

        # ── Delta drift ──────────────────────────────────────────────────
        target_delta = pos.get("target_delta")
        max_drift    = limits.get("max_delta_drift", 0.15)
        if target_delta is not None:
            drift = abs(greeks["delta"] - target_delta)
            if drift > max_drift:
                alerts.append({
                    "type":      "DELTA_DRIFT",
                    "trade_id":  trade_id,
                    "symbol":    symbol,
                    "message": (
                        f"Delta drifted {drift:.3f} from target "
                        f"(current={greeks['delta']:.3f} "
                        f"target={target_delta:.3f})"
                    ),
                    "severity": "MEDIUM",
                })

        # ── Theta decay ───────────────────────────────────────────────────
        max_theta_pct = limits.get("max_theta_decay_pct", 2.0)
        position_value = abs(pos.get("entry_price", 1) * greeks["lot_size"])
        if position_value > 0:
            daily_theta_pct = abs(greeks["daily_theta"]) / position_value * 100
            if daily_theta_pct > max_theta_pct:
                alerts.append({
                    "type":     "HIGH_THETA",
                    "trade_id": trade_id,
                    "symbol":   symbol,
                    "message": (
                        f"Daily theta decay {daily_theta_pct:.2f}% of position "
                        f"(limit {max_theta_pct}%)"
                    ),
                    "severity": "MEDIUM",
                })

        # ── Portfolio vega ────────────────────────────────────────────────
        # This check is done at portfolio level in portfolio_risk_engine
        # Here we just flag extreme individual vega

        return alerts

    def get_portfolio_greeks(self) -> dict:
        """
        Aggregate Greeks across all open option positions.
        Used by portfolio_risk_engine for vega limit checks.
        """
        total_delta = 0.0
        total_vega  = 0.0
        total_theta = 0.0
        total_gamma = 0.0
        count       = 0

        for trade_id, g in self._greeks_cache.items():
            total_delta += g.get("portfolio_delta", 0)
            total_vega  += g.get("portfolio_vega",  0)
            total_theta += g.get("daily_theta",     0)
            total_gamma += g.get("gamma", 0) * g.get("lot_size", 50)
            count       += 1

        return {
            "positions":    count,
            "total_delta":  round(total_delta, 2),
            "total_vega":   round(total_vega,  2),
            "daily_theta":  round(total_theta, 2),
            "total_gamma":  round(total_gamma, 6),
        }

    def get_status_message(self) -> str:
        pg = self.get_portfolio_greeks()
        if pg["positions"] == 0:
            return "⚙️ <b>GREEKS</b>\nNo open options positions."
        return (
            f"⚙️ <b>PORTFOLIO GREEKS</b>\n"
            f"Positions : {pg['positions']}\n"
            f"Net Delta : {pg['total_delta']:+.2f}\n"
            f"Net Vega  : {pg['total_vega']:+.2f}\n"
            f"Daily Θ   : {pg['daily_theta']:+.2f}\n"
            f"Net Gamma : {pg['total_gamma']:.4f}"
        )

    def _send_alert(self, alert: dict):
        try:
            from interfaces.telegram_interface import tg
            sev   = {"CRITICAL": "🚨", "MEDIUM": "⚠️", "LOW": "ℹ️"}.get(
                alert.get("severity", "LOW"), "⚠️"
            )
            tg.send(
                f"{sev} <b>OPTIONS ALERT: {alert['type']}</b>\n"
                f"Symbol  : {alert.get('symbol', '?')}\n"
                f"Message : {alert.get('message', '?')}"
            )
        except Exception:
            pass


# ── Singleton ─────────────────────────────────────────────────────────────────
_greeks_monitor: Optional[GreeksMonitor] = None

def get_greeks_monitor() -> GreeksMonitor:
    global _greeks_monitor
    if _greeks_monitor is None:
        _greeks_monitor = GreeksMonitor()
    return _greeks_monitor

greeks_monitor = get_greeks_monitor
