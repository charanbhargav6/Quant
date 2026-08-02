"""
CRAVE v12 — News Trader Engine
================================
Two strategies that activate on high-impact news events:

STRATEGY A — DIRECTIONAL (Sentiment-Driven)
  When a red-folder event fires AND sentiment is clear:
  - NFP beats forecast → USD bullish → SHORT EURUSD
  - NFP misses forecast → USD bearish → LONG EURUSD + LONG GOLD
  - Fed hawkish surprise → SHORT EURUSD, SHORT GOLD, SHORT BTC
  - Risk: 1.5% (higher than normal), RR: 1:3 (targets spike extension)

STRATEGY B — STRADDLE (Pure Volatility Capture)
  When a red-folder event fires AND sentiment is unclear:
  - Place BUY LIMIT 15 pips above current price
  - Place SELL LIMIT 15 pips below current price
  - First one to trigger cancels the other (OCO)
  - Risk: 1% per leg, TP: 45 pips (3x the trigger distance)
  - Captures the directional spike regardless of direction

STRATEGY C — X SIGNAL TRADE
  When Trump/Musk/major figure posts about a specific asset:
  - Signal strength >= 0.5 → trade in direction of sentiment
  - Smaller size (0.5% risk), tight TP (quick scalp)
"""

import os
import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List

logger = logging.getLogger("crave.news_trader")

# ─────────────────────────────────────────────────────────────────────────────
# EVENT → ASSET + DIRECTION MAP
# (What to trade when a specific event fires + direction)
# ─────────────────────────────────────────────────────────────────────────────

EVENT_TRADE_MAP = {
    # US Economic Data
    "Non-Farm Payrolls": {
        "beat":  [("EURUSD=X", "sell"), ("XAUUSD=X", "sell"), ("USDJPY=X", "buy")],
        "miss":  [("EURUSD=X", "buy"),  ("XAUUSD=X", "buy"),  ("USDJPY=X", "sell")],
        "straddle_symbol": "EURUSD=X",
        "straddle_pips": 20,
    },
    "CPI": {
        "beat":  [("EURUSD=X", "sell"), ("XAUUSD=X", "buy")],   # hot CPI = hawkish = USD up, gold up
        "miss":  [("EURUSD=X", "buy"),  ("XAUUSD=X", "sell")],
        "straddle_symbol": "EURUSD=X",
        "straddle_pips": 15,
    },
    "FOMC": {
        "beat":  [("EURUSD=X", "sell"), ("XAUUSD=X", "sell"), ("BTCUSDT", "sell")],
        "miss":  [("EURUSD=X", "buy"),  ("XAUUSD=X", "buy"),  ("BTCUSDT", "buy")],
        "straddle_symbol": "XAUUSD=X",
        "straddle_pips": 25,
    },
    "GDP": {
        "beat":  [("EURUSD=X", "sell"), ("USDJPY=X", "buy")],
        "miss":  [("EURUSD=X", "buy"),  ("USDJPY=X", "sell")],
        "straddle_symbol": "EURUSD=X",
        "straddle_pips": 15,
    },
    "Retail Sales": {
        "beat":  [("EURUSD=X", "sell")],
        "miss":  [("EURUSD=X", "buy")],
        "straddle_symbol": "EURUSD=X",
        "straddle_pips": 12,
    },
    # ECB / European
    "ECB": {
        "beat":  [("EURUSD=X", "buy"),  ("GBPUSD=X", "buy")],
        "miss":  [("EURUSD=X", "sell"), ("GBPUSD=X", "sell")],
        "straddle_symbol": "EURUSD=X",
        "straddle_pips": 20,
    },
    # BOE
    "BOE": {
        "beat":  [("GBPUSD=X", "buy")],
        "miss":  [("GBPUSD=X", "sell")],
        "straddle_symbol": "GBPUSD=X",
        "straddle_pips": 20,
    },
}

# Pip sizes per symbol
PIP_SIZE = {
    "EURUSD=X": 0.0001,
    "GBPUSD=X": 0.0001,
    "USDJPY=X": 0.01,
    "AUDUSD=X": 0.0001,
    "XAUUSD=X": 0.10,
    "BTCUSDT":  10.0,
    "ETHUSDT":  1.0,
}

# ─────────────────────────────────────────────────────────────────────────────
# BLACKOUT WINDOW — don't trade N minutes before events (spreads widen)
# ─────────────────────────────────────────────────────────────────────────────

PRE_EVENT_BLACKOUT_MINS = 5
POST_EVENT_ENTRY_WINDOW_MINS = 3   # enter within 3 min of event


class NewsTrader:
    """
    Autonomous news-driven trading engine.
    Monitors upcoming events and fires trades on release.
    """

    def __init__(self):
        self._running = False
        self._active_straddles: Dict[str, dict] = {}   # symbol → straddle state
        self._fired_events: set = set()                  # prevent duplicate fires
        self._lock = threading.Lock()

    # ─────────────────────────────────────────────────────────────────────────
    # CONTROL
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        t = threading.Thread(
            target=self._monitor_loop, daemon=True, name="NewsTrader"
        )
        t.start()
        logger.info("[NewsTrader] Started — monitoring red-folder events")

    def stop(self):
        self._running = False

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def _monitor_loop(self):
        while self._running:
            try:
                self._check_events()
                self._check_x_signals()
                self._manage_active_straddles()
            except Exception as e:
                logger.error(f"[NewsTrader] Loop error: {e}")
            time.sleep(30)  # check every 30 seconds

    def _check_events(self):
        """Check ForexFactory calendar for upcoming and just-fired events."""
        try:
            from intelligence.news_sentinel import get_sentinel
            sentinel = get_sentinel()

            now = datetime.now(timezone.utc)
            upcoming = sentinel.get_upcoming_events(hours_ahead=0.2)  # next 12 min

            for event in upcoming:
                event_time = self._parse_ts(event.get("time_utc", ""))
                event_name = event.get("event", "")
                currency   = event.get("currency", "")
                impact     = event.get("impact", "")
                event_id   = f"{event_name}_{event.get('time_utc', '')}"

                if impact != "high" or event_id in self._fired_events:
                    continue

                mins_to_event = (event_time - now).total_seconds() / 60

                # PRE-EVENT BLACKOUT: 5 min before — warn main loop to halt
                if 0 < mins_to_event <= PRE_EVENT_BLACKOUT_MINS:
                    logger.info(
                        f"[NewsTrader] 🚨 RED FOLDER in {mins_to_event:.1f}min: "
                        f"{event_name} ({currency}) — BLACKOUT ACTIVE"
                    )
                    self._activate_blackout(event)

                # EVENT JUST FIRED: within 3 min after event time
                elif -POST_EVENT_ENTRY_WINDOW_MINS <= mins_to_event <= 0:
                    actual   = event.get("actual", "")
                    forecast = event.get("forecast", "")

                    if actual:  # event has released actual data
                        logger.info(
                            f"[NewsTrader] 🔥 EVENT FIRED: {event_name} "
                            f"Actual={actual} Forecast={forecast}"
                        )
                        self._fire_news_trade(event, actual, forecast)
                        self._fired_events.add(event_id)
                    else:
                        # No actual yet — deploy straddle now
                        self._deploy_straddle(event)

        except Exception as e:
            logger.error(f"[NewsTrader] Event check error: {e}")

    def _check_x_signals(self):
        """Check for high-urgency X/Twitter signals."""
        try:
            from intelligence.x_scraper import get_x_scraper
            scraper = get_x_scraper()
            signals = scraper.get_recent_signals(max_age_mins=5)

            for sig in signals:
                sig_id = sig.get("id", "")
                if sig_id in self._fired_events:
                    continue
                if sig.get("urgency") != "immediate":
                    continue
                if sig.get("signal_strength", 0) < 0.5:
                    continue

                self._fire_x_trade(sig)
                self._fired_events.add(sig_id)

        except Exception as e:
            logger.debug(f"[NewsTrader] X signal check error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # STRATEGY A — DIRECTIONAL
    # ─────────────────────────────────────────────────────────────────────────

    def _fire_news_trade(self, event: dict, actual: str, forecast: str):
        """
        Fire directional trade based on actual vs forecast comparison.
        """
        event_name = event.get("event", "")

        # Find matching event config
        trade_config = None
        for key in EVENT_TRADE_MAP:
            if key.lower() in event_name.lower():
                trade_config = EVENT_TRADE_MAP[key]
                break

        if not trade_config:
            # Unknown event — fall back to straddle
            logger.info(f"[NewsTrader] No directional config for {event_name} — using straddle")
            self._deploy_straddle(event)
            return

        direction = self._compare_actual_forecast(actual, forecast, event.get("currency", ""))
        trades = trade_config.get(direction, [])

        if not trades:
            logger.info(f"[NewsTrader] No trade defined for {event_name} direction={direction}")
            self._deploy_straddle(event)
            return

        for symbol, trade_dir in trades:
            try:
                self._execute_news_trade(
                    symbol=symbol,
                    direction=trade_dir,
                    risk_pct=1.5,          # higher risk on news trades
                    rr=3.0,                # 1:3 RR to capture spike extension
                    reason=f"{event_name} {direction} surprise",
                    trade_type="directional_news",
                )
            except Exception as e:
                logger.error(f"[NewsTrader] Directional fire failed for {symbol}: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # STRATEGY B — STRADDLE
    # ─────────────────────────────────────────────────────────────────────────

    def _deploy_straddle(self, event: dict):
        """
        Deploy a buy+sell limit straddle around the current price.
        First leg to fill cancels the other.
        """
        event_name = event.get("event", "")

        # Find straddle config
        straddle_symbol = None
        straddle_pips   = 15
        for key, cfg in EVENT_TRADE_MAP.items():
            if key.lower() in event_name.lower():
                straddle_symbol = cfg.get("straddle_symbol")
                straddle_pips   = cfg.get("straddle_pips", 15)
                break

        if not straddle_symbol:
            straddle_symbol = "EURUSD=X"  # default

        straddle_key = f"straddle_{event_name}_{straddle_symbol}"
        with self._lock:
            if straddle_key in self._active_straddles:
                return  # already deployed

        try:
            # Get current price
            price = self._get_current_price(straddle_symbol)
            if not price:
                return

            pip_size = PIP_SIZE.get(straddle_symbol, 0.0001)
            pip_dist = straddle_pips * pip_size

            buy_entry  = round(price + pip_dist, 5)
            sell_entry = round(price - pip_dist, 5)

            # SL = same distance on the wrong side
            buy_sl  = round(buy_entry  - pip_dist * 2, 5)
            sell_sl = round(sell_entry + pip_dist * 2, 5)

            # TP = 3x the trigger distance
            buy_tp  = round(buy_entry  + pip_dist * 3, 5)
            sell_tp = round(sell_entry - pip_dist * 3, 5)

            logger.info(
                f"[NewsTrader] 📐 STRADDLE deployed: {straddle_symbol} "
                f"| BUY@{buy_entry} SELL@{sell_entry} | Event: {event_name}"
            )

            straddle_data = {
                "symbol":      straddle_symbol,
                "event":       event_name,
                "price":       price,
                "buy_entry":   buy_entry,
                "sell_entry":  sell_entry,
                "buy_sl":      buy_sl,
                "sell_sl":     sell_sl,
                "buy_tp":      buy_tp,
                "sell_tp":     sell_tp,
                "pip_dist":    pip_dist,
                "deployed_at": datetime.now(timezone.utc).isoformat(),
                "buy_filled":  False,
                "sell_filled": False,
            }

            with self._lock:
                self._active_straddles[straddle_key] = straddle_data

            # Execute both limit orders via MT5
            self._place_straddle_orders(straddle_data)

        except Exception as e:
            logger.error(f"[NewsTrader] Straddle deploy failed: {e}")

    def _manage_active_straddles(self):
        """
        Monitor active straddles:
        - If one leg fills, cancel the other
        - If event passes without fill, cancel both
        """
        now = datetime.now(timezone.utc)
        keys_to_remove = []

        with self._lock:
            straddles = dict(self._active_straddles)

        for key, straddle in straddles.items():
            deployed_at = self._parse_ts(straddle.get("deployed_at", ""))
            age_mins = (now - deployed_at).total_seconds() / 60

            # Auto-cancel after 15 minutes if neither leg filled
            if age_mins > 15 and not straddle.get("buy_filled") and not straddle.get("sell_filled"):
                logger.info(f"[NewsTrader] Straddle expired: {straddle['event']} — cancelling")
                self._cancel_straddle(straddle)
                keys_to_remove.append(key)

        with self._lock:
            for key in keys_to_remove:
                self._active_straddles.pop(key, None)

    # ─────────────────────────────────────────────────────────────────────────
    # STRATEGY C — X SIGNAL TRADE
    # ─────────────────────────────────────────────────────────────────────────

    def _fire_x_trade(self, signal: dict):
        """Fire a quick scalp trade based on an influential account post."""
        assets    = signal.get("assets", [])
        sentiment = signal.get("sentiment", "neutral")
        strength  = signal.get("signal_strength", 0)
        account   = signal.get("account", "")

        if sentiment == "neutral" or not assets:
            return

        # Pick most relevant asset
        priority_assets = ["BTCUSDT", "XAUUSD=X", "EURUSD=X"]
        symbol = None
        for pa in priority_assets:
            if pa in assets:
                symbol = pa
                break
        if not symbol:
            symbol = assets[0]

        direction = "buy" if sentiment == "bullish" else "sell"
        risk_pct  = round(0.5 * strength, 2)  # Scale risk to signal strength

        logger.info(
            f"[NewsTrader] 🐦 X SIGNAL: {account} → {direction.upper()} "
            f"{symbol} | strength={strength} risk={risk_pct}%"
        )

        self._execute_news_trade(
            symbol=symbol,
            direction=direction,
            risk_pct=risk_pct,
            rr=2.0,
            reason=f"X signal: {account}",
            trade_type="x_signal",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # EXECUTION HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _execute_news_trade(self, symbol: str, direction: str,
                             risk_pct: float, rr: float,
                             reason: str, trade_type: str):
        """
        Fire a live trade through the broker router with news-specific params.
        """
        try:
            from brokers.broker_router import get_router
            from brokers.mt5_agent import get_mt5
            import MetaTrader5 as mt5

            agent = get_mt5()
            if not agent.ensure_connected():
                logger.warning("[NewsTrader] MT5 not connected — skipping news trade")
                return

            # Get live price
            price = self._get_current_price(symbol)
            if not price:
                return

            account_info = agent.get_account_info()
            equity = account_info["equity"] if account_info else 10000

            # Calculate SL/TP based on ATR
            atr_sl_pips = {
                "EURUSD=X": 0.0020,  "GBPUSD=X": 0.0025,
                "USDJPY=X": 0.20,    "XAUUSD=X": 3.00,
                "BTCUSDT":  200.0,
            }.get(symbol, 0.0020)

            pip = PIP_SIZE.get(symbol, 0.0001)
            if direction == "buy":
                sl = round(price - atr_sl_pips, 5)
                tp = round(price + atr_sl_pips * rr, 5)
            else:
                sl = round(price + atr_sl_pips, 5)
                tp = round(price - atr_sl_pips * rr, 5)

            lot_size = agent.calculate_lot_size(
                crave_symbol=symbol,
                equity=equity,
                risk_pct=risk_pct,
                entry=price,
                sl=sl,
            )

            result = agent.place_order(
                crave_symbol=symbol,
                direction=direction,
                lot_size=lot_size,
                sl=sl,
                tp=tp,
                comment=f"CRAVE NEWS {trade_type[:8]}",
            )

            if result.get("status") == "filled":
                logger.info(
                    f"[NewsTrader] ✅ {trade_type.upper()} FILLED: "
                    f"{symbol} {direction.upper()} @ {result['fill_price']} "
                    f"| {reason}"
                )
                # Notify Telegram
                try:
                    from interfaces.telegram_interface import tg
                    tg.send(
                        f"📰 *NEWS TRADE FIRED*\n"
                        f"Pair: `{symbol}`\n"
                        f"Direction: `{direction.upper()}`\n"
                        f"Entry: `{result['fill_price']}`\n"
                        f"SL: `{sl}` | TP: `{tp}`\n"
                        f"Reason: _{reason}_"
                    )
                except Exception:
                    pass
            else:
                logger.warning(f"[NewsTrader] Order failed: {result.get('reason')}")

        except Exception as e:
            logger.error(f"[NewsTrader] Execute error: {e}")

    def _place_straddle_orders(self, straddle: dict):
        """Place both buy limit and sell limit orders for a straddle."""
        try:
            from brokers.mt5_agent import get_mt5
            import MetaTrader5 as mt5

            agent = get_mt5()
            if not agent.ensure_connected():
                return

            mt5_sym = agent._map_symbol(straddle["symbol"])
            account = agent.get_account_info()
            equity  = account["equity"] if account else 10000

            lot = agent.calculate_lot_size(
                crave_symbol=straddle["symbol"],
                equity=equity,
                risk_pct=1.0,
                entry=straddle["buy_entry"],
                sl=straddle["buy_sl"],
            )

            # BUY LIMIT
            mt5.order_send({
                "action":    mt5.TRADE_ACTION_PENDING,
                "symbol":    mt5_sym,
                "volume":    lot,
                "type":      mt5.ORDER_TYPE_BUY_LIMIT,
                "price":     straddle["buy_entry"],
                "sl":        straddle["buy_sl"],
                "tp":        straddle["buy_tp"],
                "deviation": 20,
                "magic":     654321,
                "comment":   "CRAVE STRADDLE BUY",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            })

            # SELL LIMIT
            mt5.order_send({
                "action":    mt5.TRADE_ACTION_PENDING,
                "symbol":    mt5_sym,
                "volume":    lot,
                "type":      mt5.ORDER_TYPE_SELL_LIMIT,
                "price":     straddle["sell_entry"],
                "sl":        straddle["sell_sl"],
                "tp":        straddle["sell_tp"],
                "deviation": 20,
                "magic":     654321,
                "comment":   "CRAVE STRADDLE SELL",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            })

            logger.info(f"[NewsTrader] Straddle orders placed for {straddle['symbol']}")

        except Exception as e:
            logger.error(f"[NewsTrader] Straddle order placement failed: {e}")

    def _cancel_straddle(self, straddle: dict):
        """Cancel all pending orders for a straddle."""
        try:
            from brokers.mt5_agent import get_mt5
            import MetaTrader5 as mt5

            agent = get_mt5()
            if not agent.ensure_connected():
                return

            mt5_sym = agent._map_symbol(straddle["symbol"])
            orders = mt5.orders_get(symbol=mt5_sym)
            if orders:
                for order in orders:
                    if order.magic == 654321:
                        mt5.order_send({
                            "action": mt5.TRADE_ACTION_REMOVE,
                            "order":  order.ticket,
                        })
        except Exception as e:
            logger.debug(f"[NewsTrader] Cancel straddle error: {e}")

    def _activate_blackout(self, event: dict):
        """Signal the main trading loop to pause before an event."""
        try:
            from core.streak_state import streak
            event_name = event.get("event", "unknown")
            # Use a short manual pause — trading loop will resume after
            streak.set_news_blackout(event_name, duration_mins=PRE_EVENT_BLACKOUT_MINS)
        except Exception:
            pass  # streak_state may not have this method yet

    def _get_current_price(self, crave_symbol: str) -> Optional[float]:
        """Get live mid price."""
        try:
            from brokers.broker_factory import get_broker
            import MetaTrader5 as mt5
            # 1. MT5 agent via get_broker since this logic is generic
            agent = get_broker()
            if not agent.ensure_connected():
                return None
            mt5_sym = agent._map_symbol(crave_symbol)
            tick = mt5.symbol_info_tick(mt5_sym)
            if tick:
                return (tick.ask + tick.bid) / 2
        except Exception:
            pass
        return None

    def _compare_actual_forecast(self, actual: str, forecast: str,
                                  currency: str) -> str:
        """
        Determine if actual beats/misses forecast.
        Returns "beat" | "miss" | "inline"
        """
        try:
            def parse_num(s: str) -> float:
                s = s.replace("%", "").replace("K", "000").replace("M", "000000")
                s = s.replace("+", "").strip()
                return float(s)

            a = parse_num(actual)
            f = parse_num(forecast)
            diff_pct = abs(a - f) / max(abs(f), 0.001)

            if diff_pct < 0.05:
                return "inline"
            return "beat" if a > f else "miss"
        except Exception:
            return "inline"

    def _parse_ts(self, ts: str) -> datetime:
        if not ts:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


# ── Singleton ──────────────────────────────────────────────────────────────
_news_trader: Optional[NewsTrader] = None

def get_news_trader() -> NewsTrader:
    global _news_trader
    if _news_trader is None:
        _news_trader = NewsTrader()
    return _news_trader
