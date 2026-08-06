"""
CRAVE v11.2 — MetaTrader 5 Broker Agent
==========================================
Connects to MT5 terminal for live/demo trade execution.

CAPABILITIES:
  - Market orders with SL/TP
  - Position modification (trailing SL, breakeven moves)
  - Position close (full or partial)
  - Account info (equity, balance, margin)
  - Symbol mapping (CRAVE format → MT5 format)

REQUIREMENTS:
  - Windows only (MetaTrader5 Python package is Windows-exclusive)
  - MT5 terminal installed and logged in, OR auto-login via credentials
  - pip install MetaTrader5

USAGE:
  from brokers.mt5_agent import get_mt5
  agent = get_mt5()
  agent.connect()
  agent.place_order("XAUUSD", "buy", 0.01, sl=3280.0, tp=3320.0)
"""

import os
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List

logger = logging.getLogger("crave.mt5")

# ── Symbol Mapping ────────────────────────────────────────────────────────────
# CRAVE uses yfinance-style tickers. MT5 uses broker-specific symbols.
# This mapping covers MetaQuotes-Demo server symbols.

SYMBOL_MAP = {
    # Forex
    "EURUSD=X":  "EURUSD",
    "GBPUSD=X":  "GBPUSD",
    "USDJPY=X":  "USDJPY",
    "AUDUSD=X":  "AUDUSD",
    "USDCAD=X":  "USDCAD",
    "USDCHF=X":  "USDCHF",
    "NZDUSD=X":  "NZDUSD",
    "EURJPY=X":  "EURJPY",
    "GBPJPY=X":  "GBPJPY",
    # Gold / Silver
    "XAUUSD=X":  "XAUUSD",
    "GC=F":      "XAUUSD",
    "XAGUSD=X":  "XAGUSD",
    "SI=F":      "XAGUSD",
    # Crypto (MetaQuotes demo)
    "BTCUSDT":   "BTCUSD",
    "ETHUSDT":   "ETHUSD",
    "BTC-USD":   "BTCUSD",
    "ETH-USD":   "ETHUSD",
}

# Reverse map for dashboard display
REVERSE_SYMBOL_MAP = {v: k for k, v in SYMBOL_MAP.items()}


class MT5Agent:
    """MetaTrader 5 broker agent for live/demo execution."""

    def __init__(self):
        self._connected = False
        self._mt5 = None
        self._login = int(os.environ.get("MT5_LOGIN", "0"))
        self._password = self._decrypt_env_password(os.environ.get("MT5_PASSWORD", ""))
        self._server = os.environ.get("MT5_SERVER", "MetaQuotes-Demo")
        self._last_connect_attempt = 0.0  # cooldown to prevent blocking

    @staticmethod
    def _decrypt_env_password(raw: str) -> str:
        """
        MT5_PASSWORD in .env.<profile> is written encrypted (Fernet) by
        account_endpoints_patch.py as of the account-verification fix.
        Fall back to treating it as plaintext if decryption fails, so
        pre-existing .env files with an old-style plaintext password
        don't break — re-saving the account through the UI will migrate
        it to encrypted form on the next verify-and-connect.
        """
        if not raw:
            return ""
        try:
            from core.secrets_vault import decrypt_secret
            return decrypt_secret(raw)
        except Exception:
            logger.warning(
                "[MT5] MT5_PASSWORD did not decrypt as a Fernet token — "
                "using as plaintext. Re-add this account via the UI to "
                "migrate it to encrypted storage."
            )
            return raw

    # ─────────────────────────────────────────────────────────────────────────
    # CONNECTION
    # ─────────────────────────────────────────────────────────────────────────

    def connect(self, login: int = None, password: str = None, server: str = None) -> bool:
        """Initialize MT5 terminal and login."""
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5

            terminal_path = os.environ.get("MT5_TERMINAL_PATH")
            if terminal_path and os.path.exists(terminal_path):
                init_ok = mt5.initialize(path=terminal_path)
            else:
                init_ok = mt5.initialize()

            if not init_ok:
                logger.error(f"[MT5] initialize() failed: {mt5.last_error()}")
                return False

            # Use provided or fallback to init env vars
            use_login = login if login else self._login
            use_password = password if password else self._password
            use_server = server if server else self._server

            # Login if credentials provided
            if use_login and use_password:
                authorized = mt5.login(
                    login=int(use_login),
                    password=str(use_password),
                    server=str(use_server),
                )
                if not authorized:
                    logger.error(f"[MT5] login failed for {use_login}: {mt5.last_error()}")
                    # Don't shutdown completely, just return False so we can try next account
                    return False

            info = mt5.account_info()
            if info is None:
                logger.error(f"[MT5] account_info() returned None: {mt5.last_error()}")
                mt5.shutdown()
                return False

            self._connected = True
            logger.info(
                f"[MT5] Connected ✅ | Account: {info.login} | "
                f"Balance: ${info.balance:.2f} | Equity: ${info.equity:.2f} | "
                f"Server: {info.server}"
            )
            return True

        except ImportError:
            logger.error(
                "[MT5] MetaTrader5 package not installed. "
                "Run: pip install MetaTrader5 (Windows only)"
            )
            return False
        except Exception as e:
            logger.error(f"[MT5] Connection error: {e}")
            return False

    def disconnect(self):
        """Shutdown MT5 connection."""
        if self._mt5 and self._connected:
            self._mt5.shutdown()
            self._connected = False
            logger.info("[MT5] Disconnected")

    def is_connected(self) -> bool:
        """Check if MT5 is connected and responsive."""
        if not self._connected or not self._mt5:
            return False
        try:
            info = self._mt5.account_info()
            return info is not None
        except Exception:
            self._connected = False
            return False

    def ensure_connected(self) -> bool:
        """Reconnect if disconnected — with a 30s cooldown to prevent blocking Flask."""
        if self.is_connected():
            return True
        now = time.time()
        if now - self._last_connect_attempt < 30.0:
            # Still in cooldown from last failed attempt — return False immediately
            return False
        self._last_connect_attempt = now
        logger.info("[MT5] Reconnecting...")
        return self.connect()

    # ─────────────────────────────────────────────────────────────────────────
    # SYMBOL MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    def _map_symbol(self, crave_symbol: str) -> str:
        """Convert CRAVE symbol to MT5 symbol."""
        return SYMBOL_MAP.get(crave_symbol, crave_symbol)

    def _ensure_symbol(self, mt5_symbol: str) -> bool:
        """Make sure the symbol is visible in Market Watch."""
        if not self.ensure_connected():
            return False
        info = self._mt5.symbol_info(mt5_symbol)
        if info is None:
            logger.error(f"[MT5] Symbol {mt5_symbol} not found on server")
            return False
        if not info.visible:
            if not self._mt5.symbol_select(mt5_symbol, True):
                logger.error(f"[MT5] Failed to select {mt5_symbol}")
                return False
        return True

    def get_symbol_info(self, crave_symbol: str) -> Optional[dict]:
        """Get symbol info (pip size, lot constraints, etc.)."""
        mt5_sym = self._map_symbol(crave_symbol)
        if not self._ensure_symbol(mt5_sym):
            return None
        info = self._mt5.symbol_info(mt5_sym)
        if info is None:
            return None
        return {
            "symbol": mt5_sym,
            "point": info.point,
            "digits": info.digits,
            "lot_min": info.volume_min,
            "lot_max": info.volume_max,
            "lot_step": info.volume_step,
            "spread": info.spread,
            "tick_value": info.trade_tick_value,
            "tick_size": info.trade_tick_size,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # ORDER EXECUTION
    # ─────────────────────────────────────────────────────────────────────────

    def place_order(self, crave_symbol: str, direction: str,
                    lot_size: float, sl: float, tp: float,
                    comment: str = "CRAVE") -> Dict:
        """
        Place a market order with SL and TP.

        Args:
            crave_symbol: CRAVE-format symbol (e.g. "EURUSD=X")
            direction: "buy" or "sell"
            lot_size: Volume in lots
            sl: Stop loss price
            tp: Take profit price (use TP2 / final target)
            comment: Order comment

        Returns:
            {"status": "filled"/"failed", "ticket": int, ...}
        """
        if not self.ensure_connected():
            return {"status": "failed", "reason": "MT5 not connected"}

        mt5_sym = self._map_symbol(crave_symbol)
        if not self._ensure_symbol(mt5_sym):
            return {"status": "failed", "reason": f"Symbol {mt5_sym} not available"}

        mt5 = self._mt5
        sym_info = mt5.symbol_info(mt5_sym)
        if sym_info is None:
            return {"status": "failed", "reason": f"Cannot get info for {mt5_sym}"}

        # Normalize lot size to broker constraints
        lot_size = self._normalize_lot(lot_size, sym_info)
        if lot_size <= 0:
            return {"status": "failed", "reason": f"Lot size too small after normalization"}

        # Get current price
        tick = mt5.symbol_info_tick(mt5_sym)
        if tick is None:
            return {"status": "failed", "reason": "Cannot get tick data"}

        if direction in ("buy", "long"):
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        elif direction in ("sell", "short"):
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            return {"status": "failed", "reason": f"Invalid direction: {direction}"}

        # Round SL/TP to symbol's digit precision
        digits = sym_info.digits
        sl = round(sl, digits)
        tp = round(tp, digits)
        price = round(price, digits)

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    mt5_sym,
            "volume":    lot_size,
            "type":      order_type,
            "price":     price,
            "sl":        sl,
            "tp":        tp,
            "deviation": 20,  # max slippage in points
            "magic":     123456,
            "comment":   comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logger.info(
            f"[MT5] Sending order: {direction.upper()} {lot_size} {mt5_sym} "
            f"@ {price} SL={sl} TP={tp}"
        )

        result = mt5.order_send(request)
        if result is None:
            return {"status": "failed", "reason": f"order_send returned None: {mt5.last_error()}"}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            # Try ORDER_FILLING_FOK as fallback
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)

            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                reason = result.comment if result else "Unknown"
                logger.error(f"[MT5] Order failed: retcode={result.retcode if result else '?'} {reason}")
                return {"status": "failed", "reason": reason, "retcode": result.retcode if result else -1}

        logger.info(
            f"[MT5] Order FILLED ✅ | Ticket: {result.order} | "
            f"Price: {result.price} | Volume: {result.volume}"
        )

        return {
            "status":     "filled",
            "ticket":     result.order,
            "fill_price": result.price,
            "volume":     result.volume,
            "symbol":     mt5_sym,
            "direction":  direction,
            "sl":         sl,
            "tp":         tp,
        }

    def _normalize_lot(self, lot_size: float, sym_info) -> float:
        """Round lot size to broker's lot_step and clamp to min/max."""
        lot_min  = sym_info.volume_min
        lot_max  = sym_info.volume_max
        lot_step = sym_info.volume_step

        if lot_size < lot_min:
            lot_size = lot_min
        elif lot_size > lot_max:
            lot_size = lot_max

        # Round to nearest lot_step
        if lot_step > 0:
            lot_size = round(round(lot_size / lot_step) * lot_step, 8)

        return lot_size

    # ─────────────────────────────────────────────────────────────────────────
    # POSITION MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    def modify_sl(self, ticket: int, new_sl: float, new_tp: float = None) -> tuple[bool, float, float]:
        """Modify the SL (and optionally TP) of an open position. Returns (success, actual_sl, actual_tp)."""
        if not self.ensure_connected():
            return False, 0.0, 0.0

        mt5 = self._mt5
        position = mt5.positions_get(ticket=ticket)
        if not position:
            logger.warning(f"[MT5] Position {ticket} not found for SL modification")
            return False, 0.0, 0.0

        pos = position[0]
        sym_info = mt5.symbol_info(pos.symbol)
        digits = sym_info.digits if sym_info else 5

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol":   pos.symbol,
            "sl":       round(new_sl, digits),
            "tp":       round(new_tp, digits) if new_tp else pos.tp,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            # Immediate Verification: query the broker again
            time.sleep(0.1) # Brief pause for broker sync
            verified_pos = mt5.positions_get(ticket=ticket)
            if verified_pos:
                actual_sl = verified_pos[0].sl
                actual_tp = verified_pos[0].tp
                logger.info(f"[MT5] SL modified ✅ | Ticket: {ticket} | Verified SL: {actual_sl}")
                return True, actual_sl, actual_tp
            return True, new_sl, new_tp
        else:
            reason = result.comment if result else "Unknown"
            logger.warning(f"[MT5] SL modify failed: {reason}")
            return False, 0.0, 0.0

    def close_position(self, ticket: int = None, crave_symbol: str = None,
                       volume: float = None) -> bool:
        """
        Close a position by ticket or symbol.
        If volume is specified, partial close.
        """
        if not self.ensure_connected():
            return False

        mt5 = self._mt5

        # Find the position
        if ticket:
            positions = mt5.positions_get(ticket=ticket)
        elif crave_symbol:
            mt5_sym = self._map_symbol(crave_symbol)
            positions = mt5.positions_get(symbol=mt5_sym)
        else:
            return False

        if not positions:
            logger.warning(f"[MT5] No position found to close (ticket={ticket}, symbol={crave_symbol})")
            return False

        pos = positions[0]
        close_volume = volume if volume else pos.volume

        # Close direction is opposite of position direction
        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            tick = mt5.symbol_info_tick(pos.symbol)
            price = tick.bid if tick else 0
        else:
            close_type = mt5.ORDER_TYPE_BUY
            tick = mt5.symbol_info_tick(pos.symbol)
            price = tick.ask if tick else 0

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    pos.symbol,
            "volume":    close_volume,
            "type":      close_type,
            "position":  pos.ticket,
            "price":     price,
            "deviation": 20,
            "magic":     123456,
            "comment":   "CRAVE close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                f"[MT5] Position closed ✅ | Ticket: {pos.ticket} | "
                f"Volume: {close_volume} | Price: {result.price}"
            )
            return True
        else:
            # Try FOK filling
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[MT5] Position closed ✅ (FOK) | Ticket: {pos.ticket}")
                return True
            reason = result.comment if result else "Unknown"
            logger.error(f"[MT5] Close failed: {reason}")
            return False

    def get_positions(self) -> List[dict]:
        """Get all open positions."""
        if not self.ensure_connected():
            return []

        mt5 = self._mt5
        positions = mt5.positions_get()
        if positions is None:
            return []

        result = []
        for pos in positions:
            crave_sym = REVERSE_SYMBOL_MAP.get(pos.symbol, pos.symbol)
            result.append({
                "ticket":      pos.ticket,
                "symbol":      crave_sym,
                "mt5_symbol":  pos.symbol,
                "direction":   "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell",
                "volume":      pos.volume,
                "entry_price": pos.price_open,
                "current_sl":  pos.sl,
                "current_tp":  pos.tp,
                "profit":      pos.profit,
                "swap":        pos.swap,
                "open_time":   datetime.fromtimestamp(pos.time, tz=timezone.utc).isoformat(),
                "magic":       pos.magic,
                "comment":     pos.comment,
            })
        return result

    def get_closed_trades(self, days: int = 30) -> List[dict]:
        """Get history of closed trades directly from MT5 deals."""
        if not self.ensure_connected():
            return []
            
        mt5 = self._mt5
        from datetime import timedelta
        fro = datetime.now(timezone.utc) - timedelta(days=days)
        to = datetime.now(timezone.utc) + timedelta(days=1)
        
        deals = mt5.history_deals_get(fro, to)
        if deals is None:
            return []
            
        result = []
        for d in deals:
            # We look for OUT deals (entry == 1) which represent closed positions
            if d.entry == 1:
                crave_sym = REVERSE_SYMBOL_MAP.get(d.symbol, d.symbol)
                # If an OUT deal is a BUY type, it means it closed a SHORT position.
                trade_dir = "sell" if d.type == mt5.DEAL_TYPE_BUY else "buy"
                
                result.append({
                    "ticket":      d.ticket,
                    "position_id": d.position_id,
                    "symbol":      crave_sym,
                    "mt5_symbol":  d.symbol,
                    "direction":   trade_dir,
                    "volume":      d.volume,
                    "entry_price": 0.0, # Without matching the IN deal, we just store 0
                    "exit_price":  d.price,
                    "profit":      d.profit,
                    "commission":  d.commission,
                    "fee":         d.fee,
                    "swap":        d.swap,
                    "close_time":  datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                    "magic":       d.magic,
                    "comment":     d.comment
                })
                
        # Sort by latest first
        result.sort(key=lambda x: x["close_time"], reverse=True)
        return result

    def get_account_info(self) -> Optional[dict]:
        """Get account balance, equity, margin info."""
        if not self.ensure_connected():
            return None

        info = self._mt5.account_info()
        if info is None:
            return None

        return {
            "login":       info.login,
            "server":      info.server,
            "balance":     info.balance,
            "equity":      info.equity,
            "margin":      info.margin,
            "free_margin": info.margin_free,
            "margin_level": info.margin_level,
            "leverage":    info.leverage,
            "currency":    info.currency,
            "profit":      info.profit,
        }

    def calculate_lot_size(self, crave_symbol: str, equity: float,
                           risk_pct: float, entry: float, sl: float) -> float:
        """
        Calculate position size in lots based on risk percentage.

        risk_pct: e.g. 1.0 for 1% risk
        """
        if not self.ensure_connected():
            return 0.01  # minimum fallback

        mt5_sym = self._map_symbol(crave_symbol)
        sym_info = self._mt5.symbol_info(mt5_sym)
        if sym_info is None:
            return 0.01

        risk_amount = equity * (risk_pct / 100.0)
        sl_distance = abs(entry - sl)

        if sl_distance <= 0:
            return sym_info.volume_min

        # Calculate lots: risk_amount / (sl_distance_points * tick_value)
        # For forex: tick_value is per standard lot per pip
        tick_value = sym_info.trade_tick_value
        tick_size = sym_info.trade_tick_size

        if tick_value <= 0 or tick_size <= 0:
            return sym_info.volume_min

        sl_ticks = sl_distance / tick_size
        risk_per_lot = sl_ticks * tick_value

        if risk_per_lot <= 0:
            return sym_info.volume_min

        lot_size = risk_amount / risk_per_lot
        return self._normalize_lot(lot_size, sym_info)


# ── Singleton ─────────────────────────────────────────────────────────────────
_instance: Optional[MT5Agent] = None

def get_mt5() -> MT5Agent:
    global _instance
    if _instance is None:
        _instance = MT5Agent()
    return _instance
