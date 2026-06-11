"""
CRAVE v10.1 - Trading Loop
============================
FIXES vs v10.0 (audit-driven):

  🔧 FIX 3 - ML features now complete (26 fields not 10)
             _log_signal() now calls feature_engineering.extract_features()
             Old inline dict only had 10 fields; 16 were missing entirely,
             making regime classifier training data useless.

  🔧 FIX 4 - Paper equity now used for position sizing
             _get_current_equity() now calls paper_engine.get_equity()
             Was always returning $10,000 starting equity regardless of
             compounding, making position sizes wrong after early trades.
             (Depends on FIX 1 in position_tracker_v10_1.py)

  🔧 FIX 6 - Regime filter integrated directly (not monkey-patch)
             _regime_checked_analyse() is a first-class method called
             from _run_cycle(). The monkey-patch in run_bot_final.py
             was fragile - if method names changed it silently failed open.

  🔧 M2 - Session detection now covers Asian (22:00-02:00 UTC)
           Was: "london" if 7<=h<16 else "ny" - missed Asian entirely.

  🔧 M3 - WebSocket data tried first before REST API
           ws.get_live_ohlcv() → DataAgent().get_ohlcv() fallback.
           WebSocket cache is now actually used in the signal pipeline.

  🔧 M4 - Slippage owned by paper_engine.simulate_fill() only
           Deleted duplicate slippage logic from _paper_execute().
           paper_trading.simulate_fill() does asset-class-aware slippage.
           Old code used pip_size * 2 for everything (wrong for crypto/gold).

  🔧 Audit #9 (send_trade_open exists) - confirmed tg.send_trade_open()
             call is correct; method exists in telegram_interface_v10_1.py.

  🔧 signal_id now passed into positions.open() so ML backfill works.
"""

import logging
import time
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional, List

logger = logging.getLogger("crave.trading_loop")

from core.prop_firm_guard import PropFirmGuard
from engines.economic_calendar import EconomicCalendar
from engines.mean_reversion_engine import get_mr_engine
from config.config import PROP_FIRM, ACCOUNT_SIZE, CONFIDENCE_GATES


# ── Kill Zone Helper ──────────────────────────────────────────────────────────

def is_in_kill_zone(symbol: str) -> tuple:
    try:
        from config.config import KILL_ZONES
        now_utc  = datetime.now(timezone.utc)
        now_mins = now_utc.hour * 60 + now_utc.minute

        for zone_name, zone_cfg in KILL_ZONES.items():
            instruments = zone_cfg.get("instruments", "all")
            if instruments != "all" and symbol not in instruments:
                continue

            start_h, start_m = map(int, zone_cfg["start_utc"].split(":"))
            end_h,   end_m   = map(int, zone_cfg["end_utc"].split(":"))
            start_mins = start_h * 60 + start_m
            end_mins   = end_h   * 60 + end_m

            # Handle overnight zones (e.g., Asian 23:00–02:00)
            if start_mins > end_mins:
                in_zone = now_mins >= start_mins or now_mins <= end_mins
            else:
                in_zone = start_mins <= now_mins <= end_mins

            if in_zone:
                return True, zone_name

        return False, ""

    except Exception as e:
        logger.debug(f"[Loop] Kill zone check error: {e}")
        return True, "unknown"


def _get_session_name(hour_utc: int) -> str:
    """
    FIX M2: Now includes Asian session.
    Old: "london" if 7<=h<16 else "ny" - missed 22:00-02:00 UTC entirely.
    """
    if hour_utc >= 22 or hour_utc < 2:
        return "asian"
    if 6 <= hour_utc < 12:
        return "london"
    return "ny"


# ── MTF Confluence Gate ───────────────────────────────────────────────────────

def check_mtf_confluence(symbol: str, direction: str, df_1h, df_4h) -> tuple:
    """
    Multi-timeframe gate using 1H and 4H market structure (BOS/CHoCH).
    Returns (is_approved: bool, grade_modifier: Optional[str], reason: str)
    """
    try:
        import pandas as pd
        def _get_struct_dir(df, tf_name):
            if df is None or len(df) < 20:
                return "unknown", f"{tf_name} data unavailable"
            
            window = 3
            highs, lows = [], []
            for i in range(window, len(df) - window):
                if df['high'].iloc[i] == df['high'].iloc[i - window: i + window + 1].max():
                    highs.append((i, df['high'].iloc[i]))
                if df['low'].iloc[i] == df['low'].iloc[i - window: i + window + 1].min():
                    lows.append((i, df['low'].iloc[i]))

            if len(highs) < 2 or len(lows) < 2:
                # EMA21 fallback
                close_p = df['close'].iloc[-1]
                ema21 = df['close'].ewm(span=21, adjust=False).mean().iloc[-1]
                above = close_p > ema21
                d = "bullish" if above else "bearish"
                return d, f"{tf_name} EMA21 {d}"

            last_high = highs[-1][1]
            prev_high = highs[-2][1]
            last_low = lows[-1][1]
            prev_low = lows[-2][1]
            confirmed = df['close'].iloc[-1]

            bullish_bos = confirmed > last_high
            bearish_bos = confirmed < last_low
            choch_bull = confirmed > last_high and prev_high > highs[-1][1]
            choch_bear = confirmed < last_low and prev_low < lows[-1][1]

            hh = last_high > prev_high
            ll = last_low < prev_low
            hl = last_low > prev_low
            lh = last_high < prev_high

            structure_bullish = bullish_bos or choch_bull or (hh and hl)
            structure_bearish = bearish_bos or choch_bear or (ll and lh)

            if structure_bullish and not structure_bearish: return "bullish", f"{tf_name} structure BULLISH"
            if structure_bearish and not structure_bullish: return "bearish", f"{tf_name} structure BEARISH"
            
            # Ranging - fallback to EMA
            ema21 = df['close'].ewm(span=21, adjust=False).mean().iloc[-1]
            above = confirmed > ema21
            d = "bullish" if above else "bearish"
            return d, f"{tf_name} ranging, EMA21 {d}"

        target_dir = "bullish" if direction in ("buy", "long") else "bearish"
        
        dir_1h, reason_1h = _get_struct_dir(df_1h, "1H")
        dir_4h, reason_4h = _get_struct_dir(df_4h, "4H")

        if dir_1h != "unknown" and dir_1h != target_dir:
            return False, None, f"1H structure conflicts ({reason_1h})"
            
        if dir_4h != "unknown" and dir_4h != target_dir:
            return True, "B", f"1H aligns but 4H conflicts ({reason_4h}) — downgraded to Grade B"
            
        return True, None, f"MTF aligns: {reason_1h} + {reason_4h}"

    except Exception as e:
        logger.debug(f"[Loop] MTF gate error: {e}")
        return True, None, "MTF error - passing by default"


def _get_ohlcv_with_ws_fallback(symbol: str,
                                  timeframe: str,
                                  limit: int):
    """
    FIX S8: Use market_data_router as single entry point.
    The router already handles: WS cache → DB cache → primary → fallback.
    No need for a redundant fallback chain here.
    """
    try:
        from data.market_data_router import get_data_router
        df = get_data_router().get_ohlcv(symbol, timeframe, limit=limit)
        if df is not None and len(df) >= 20:
            return df
    except Exception as e:
        logger.debug(f"DataRouter unavailable: {e}")

    # Last resort: direct DataAgent call (only if router module itself fails)
    try:
        from core.data_agent import get_data_agent
        from config.config import get_instrument
        exchange = get_instrument(symbol).get("exchange", "yfinance")
        return get_data_agent().get_ohlcv(
            symbol, exchange=exchange, timeframe=timeframe, limit=limit
        )
    except Exception as e:
        logger.debug(f"[Loop] OHLCV fallback failed {symbol}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────

class TradingLoop:

    SCAN_INTERVAL_SECS      = 300
    MAX_INSTRUMENTS_PER_DAY = 2

    def __init__(self):
        self._running         = False
        self._thread: Optional[threading.Thread] = None
        self._watchlist:      List[dict] = []
        self._trades_today    = 0
        self._last_trade_date = ""
        self._volatile_override = False
        self._is_paper          = True

        try:
            from config.config import PAPER_TRADING
            self._is_paper = PAPER_TRADING.get("enabled", True)
        except Exception:
            self._is_paper = True

        # ── FIX: Ensure PropFirmGuard account_size matches paper equity ────
        # ACCOUNT_SIZE may be set for live (e.g. $100k) while paper starts
        # at $10k. If mismatched, guard thinks there's a massive drawdown.
        guard_account_size = ACCOUNT_SIZE
        if self._is_paper:
            try:
                from config.config import PAPER_TRADING as _pt
                paper_equity = float(_pt.get("starting_equity", 10000))
                if guard_account_size != paper_equity:
                    logger.warning(
                        f"[TradingLoop] ACCOUNT_SIZE (${guard_account_size:,.0f}) != "
                        f"paper starting_equity (${paper_equity:,.0f}). "
                        f"Using paper equity for PropFirmGuard."
                    )
                    guard_account_size = paper_equity
            except Exception:
                pass

        self.prop_firm_guard = PropFirmGuard(firm=PROP_FIRM, account_size=guard_account_size)
        self.calendar = EconomicCalendar()

        logger.info(
            f"[TradingLoop] Initialised. Mode: "
            f"{'📄 PAPER' if self._is_paper else '💰 LIVE'}"
        )

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="CRAVETradingLoop"
        )
        self._thread.start()
        logger.info(f"[TradingLoop] Started. Scanning every {self.SCAN_INTERVAL_SECS}s.")

    def stop(self):
        self._running = False
        logger.info("[TradingLoop] Stopped.")

    def _loop(self):
        while self._running:
            try:
                self._run_cycle()
            except Exception as e:
                logger.error(f"[TradingLoop] Cycle error: {e}")
            time.sleep(self.SCAN_INTERVAL_SECS)

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN CYCLE
    # ─────────────────────────────────────────────────────────────────────────

    def _run_cycle(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_trade_date:
            self._trades_today    = 0
            self._last_trade_date = today

            # ── FIX B1: Run bias engine on startup / new day ───────────────
            # Bias engine normally runs at 06:30 UTC via scheduler.
            # If bot starts after 06:30 (e.g. 10:36) bias was never computed
            # → bias_score = 0 for ALL instruments → zero trades all day.
            # Fix: always run bias analysis on first cycle of each day.
            try:
                from engines.daily_bias_engine import bias_engine
                from engines.instrument_scanner import scanner
                logger.info("[TradingLoop] New day — running startup bias analysis...")
                bias_engine.run_daily_analysis()
                scanner.run_daily_scan()
                logger.info("[TradingLoop] Startup bias analysis complete.")
            except Exception as e:
                logger.warning(f"[TradingLoop] Startup bias run failed (non-fatal): {e}")

        equity = self._get_current_equity()
        self.prop_firm_guard.update_equity(equity)

        from core.streak_state import streak
        can_trade, reason = streak.can_trade()
        if not can_trade:
            logger.debug(f"[TradingLoop] Gate blocked: {reason}")
            return

        try:
            from infra.node_orchestrator import orchestrator
            if not orchestrator.is_active():
                return
        except Exception:
            pass

        from core.position_tracker import positions
        current_open = positions.count()
        if current_open >= self.MAX_INSTRUMENTS_PER_DAY:
            return

        # ── Gate 5: Portfolio risk engine ─────────────────────────────────
        # Checks total heat, per-market heat, currency exposure.
        # Blocks entry if any limit would be breached.
        # Also triggers emergency SL tightening if heat is elevated.
        try:
            from risk.portfolio_risk_engine import get_portfolio_risk
            pr = get_portfolio_risk()

            # Emergency SL tighten (5-6.5% range)
            pr.tighten_stops_if_overheated()

            # Emergency close (> 6.5%)
            if pr.check_emergency_close():
                logger.warning("[TradingLoop] Emergency close triggered. Skipping new entries.")
                return

        except Exception as e:
            logger.debug(f"[TradingLoop] Portfolio risk check error (non-fatal): {e}")

        self._process_watchlist()

        # ── Gate 4: Filter instruments to enabled markets only ────────────
        # Instruments whose market is disabled in MARKETS config are excluded.
        # This is how you turn US stocks or India on/off without touching
        # individual instrument configs.
        # (Already handled by get_tradeable_symbols() in config.py -
        #  no code change needed here, just documentation.)

        from engines.instrument_scanner import scanner
        tradeable = scanner.get_tradeable_today()
        if not tradeable:
            return

        slots_available = self.MAX_INSTRUMENTS_PER_DAY - current_open
        for inst_data in tradeable[:slots_available * 2]:
            symbol = inst_data["symbol"]
            if positions.has_open_position(symbol):
                continue

            in_kz, kz_name = is_in_kill_zone(symbol)
            if not in_kz:
                # FIX B3: Outside kill zone — SMC still waits for KZ.
                # But Mean Reversion setups are valid any time price is at
                # a BB extreme — no session timing required.
                # Route to MR analysis directly, skip SMC gate.
                try:
                    from ml.regime_classifier import regime_model
                    df_check = _get_ohlcv_with_ws_fallback(symbol, "1h", 50)
                    regime   = regime_model.predict(symbol, df_check) if df_check is not None else "UNKNOWN"
                except Exception:
                    regime = "UNKNOWN"

                if regime == "RANGING":
                    result = self._analyse_mean_reversion(symbol, "", regime)
                    if result and result.get("executed"):
                        slots_available -= 1
                        if slots_available <= 0:
                            break
                else:
                    self._add_to_watchlist(symbol, inst_data)
                continue

            # FIX 6: Regime check is now a first-class method call,
            # not a monkey-patch in run_bot_final.py.
            result = self._regime_checked_analyse(symbol, kz_name)
            if result and result.get("executed"):
                slots_available -= 1
                if slots_available <= 0:
                    break

        self._run_options_cycle()

    def _run_options_cycle(self):
        """
        Options signal check. Runs after regular SMC signal check.
        Only active when MARKETS["options"]["enabled"] = True.
        """
        from config.config import is_market_enabled
        if not is_market_enabled("options"):
            return

        from config.config import get_symbols_for_market
        option_symbols = get_symbols_for_market("options")
        if not option_symbols:
            return

        try:
            from options.options_engine import get_options_engine

            # Use NIFTY and BANKNIFTY as primary options candidates
            for symbol in ["NIFTY_FUT", "BANKNIFTY_FUT"]:
                from core.data_agent import get_data_agent
                df = get_data_agent().get_ohlcv(symbol, exchange="zerodha", timeframe="1h", limit=100)
                if df is None or len(df) < 20:
                    continue

                # Get SMC direction if any signal exists
                smc_direction = None
                try:
                    from engines.daily_bias_engine import bias_engine
                    bias = bias_engine.get_bias(symbol)
                    if bias and bias.get("bias") != "NO_TRADE":
                        smc_direction = ("buy"
                                         if bias.get("bias") == "BUY"
                                         else "sell")
                except Exception:
                    pass

                strategy = get_options_engine().select_strategy(
                    symbol, df, smc_direction
                )
                if strategy:
                    logger.info(
                        f"[TradingLoop] Options signal: "
                        f"{strategy['name']} on {strategy['symbol']}"
                    )
                    # Send to Telegram for manual review (options require
                    # human confirmation until auto-execution is validated)
                    self._notify_options_signal(strategy)

        except Exception as e:
            logger.error(f"[TradingLoop] Options cycle error: {e}")

    def _notify_options_signal(self, strategy: dict):
        """Send options signal to Telegram for review."""
        try:
            from interfaces.telegram_interface import tg
            strikes = strategy.get("strikes", {})
            strike_str = " | ".join(f"{k}={v}" for k, v in strikes.items()
                                     if isinstance(v, (int, float)))
            tg.send(
                f"⚙️ <b>OPTIONS SIGNAL</b>\n"
                f"Strategy : {strategy['name'].upper()}\n"
                f"Symbol   : {strategy['symbol']}\n"
                f"Spot     : {strategy.get('spot', '?')}\n"
                f"Strikes  : {strike_str}\n"
                f"DTE      : {strategy.get('dte', '?')}\n"
                f"IV Rank  : {strategy.get('iv_rank', '?')}\n"
                f"Regime   : {strategy.get('regime', '?')}\n"
                f"Reason   : {strategy.get('reason', '?')}\n\n"
                f"⚠️ Manual confirmation required.\n"
                f"Reply /options_confirm to execute."
            )
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 6 - Regime check integrated directly
    # ─────────────────────────────────────────────────────────────────────────

    def _regime_checked_analyse(self, symbol: str,
                                  kz_name: str = "") -> Optional[dict]:
        """
        New Gate Stack (v10.5):
        1. Calendar Gate (News blackout)
        2. Prop Firm Guard
        3. Regime Gate & Routing
        """
        # ── 1. Calendar Gate ──────────────────────────────────────────────
        blackout_status = self.calendar.is_blackout_now(symbol)
        if blackout_status.get("blackout"):
            logger.info(f"[TradingLoop] {symbol}: Blocked by Economic Calendar - {blackout_status.get('reason')}")
            return None

        # ── 2. Prop Firm Guard ────────────────────────────────────────────
        # Run with dummy 1.0% risk to get the scaling ratio
        guard_result = self.prop_firm_guard.check_trade(risk_pct=1.0, symbol=symbol)
        if not guard_result["allowed"]:
            logger.info(f"[TradingLoop] {symbol}: Prop Firm Guard blocked: {guard_result['reason']}")
            return None
            
        # If guard scaled it down (e.g., 0.5%), the multiplier is 0.5
        prop_risk_multiplier = guard_result["scaled_risk_pct"]

        # ── 3. Regime Gate ────────────────────────────────────────────────
        regime = "UNKNOWN"
        try:
            from ml.regime_classifier import regime_model
            df_1h = _get_ohlcv_with_ws_fallback(symbol, "1h", 100)
            if df_1h is not None:
                regime = regime_model.predict(symbol, df_1h)
                self._volatile_override = regime_model.should_reduce_size(regime)
            else:
                self._volatile_override = False
        except Exception as e:
            logger.debug(f"[TradingLoop] Regime check error (non-fatal): {e}")
            self._volatile_override = False

        if self._volatile_override:
            prop_risk_multiplier *= 0.5

        # ── 4. Routing ────────────────────────────────────────────────────
        from config.config import get_asset_params
        is_gold = get_asset_params(symbol).get("label") == "Gold"

        # Gold has its own trend/ranging logic in gold_strategy.py
        if is_gold or regime_model.is_favourable(regime):
            return self._analyse_and_execute(symbol, kz_name, prop_risk_multiplier)
        elif regime == "RANGING":
            return self._analyse_mean_reversion(symbol, kz_name, regime, prop_risk_multiplier)
        else:
            logger.info(f"[TradingLoop] {symbol}: Regime={regime} - unfavourable. Skipping.")
            return None

    def _analyse_mean_reversion(self, symbol: str, kz_name: str, regime: str, prop_risk_multiplier: float = 1.0) -> Optional[dict]:
        session_tag = f" [{kz_name}]" if kz_name else ""
        logger.info(f"[TradingLoop] {symbol}: Regime=RANGING. Routing to Mean Reversion.{session_tag}")
        df_15m = _get_ohlcv_with_ws_fallback(symbol, "15m", 250)
        mr_result = get_mr_engine().analyze(symbol, df_15m, regime)
        
        if not mr_result.get("signal"):
            reason = mr_result.get("reason", "unknown")
            logger.info(f"[TradingLoop] {symbol}: MR scan — no setup. Reason: {reason}")
            return None
        
        if mr_result.get("signal"):
            conf_gate = CONFIDENCE_GATES.get(symbol, CONFIDENCE_GATES.get("default", 0.50))
            if (mr_result.get("confidence", 0) / 100.0) < conf_gate:
                logger.info(f"[TradingLoop] {symbol}: MR confidence {mr_result['confidence']}% below gate {conf_gate*100}%")
                return None
                
            final_risk = mr_result["risk_pct"] * 100.0 * prop_risk_multiplier
            validated = {
                "action": mr_result["signal"],
                "price": mr_result["entry"],
                "symbol": symbol,
                "is_swing_trade": False,
                "approved": True,
                "reason": mr_result["reason"],
                "grade": "B",
                "risk_pct": final_risk,
                "is_paper": self._is_paper,
                "exchange": self._get_exchange_for(symbol),
                "node": self._get_node_name(),
                "order_type": "market",
                "strict_post_only": False,
                "signal_id": str(uuid.uuid4())[:8].upper()
            }
            
            logger.info(
                f"[TradingLoop] MR SIGNAL: {symbol} {mr_result['signal'].upper()} "
                f"conf={mr_result['confidence']}% risk={final_risk:.2f}%"
            )
            
            result = self._execute(validated, mr_result["entry"])
            if result and result.get("status") in ("filled", "paper_filled"):
                self._trades_today += 1
                return {"executed": True, "trade_id": result.get("trade_id")}
        return None



    # ─────────────────────────────────────────────────────────────────────────
    # SIGNAL ANALYSIS + EXECUTION
    # ─────────────────────────────────────────────────────────────────────────

    def _analyse_and_execute(self, symbol: str,
                              kz_name: str = "",
                              prop_risk_multiplier: float = 1.0) -> Optional[dict]:
        logger.info(f"[TradingLoop] Analysing {symbol} ({kz_name})...")

        # FIX: Fetch 500 candles so SMA_200 is fully warmed (matches backtest)
        df_15m = _get_ohlcv_with_ws_fallback(symbol, "15m", 500)
        df_1h = _get_ohlcv_with_ws_fallback(symbol, "1h", 250)
        df_4h = _get_ohlcv_with_ws_fallback(symbol, "4h", 60)

        if df_15m is None or len(df_15m) < 60 or df_1h is None or len(df_1h) < 60:
            logger.warning(f"[TradingLoop] Insufficient data for {symbol}")
            return None

        # ── Route gold to dedicated pullback strategy ─────────────────────
        from config.config import get_asset_params as _gap
        _is_gold = _gap(symbol).get("label") == "Gold"

        if _is_gold:
            from engines.gold_strategy import (
                attach_gold_indicators, analyze_gold_context
            )
            df_15m = attach_gold_indicators(df_15m)
            context = analyze_gold_context(symbol, df_15m, len(df_15m) - 1)
            logger.info(f"[TradingLoop] {symbol}: Gold pullback strategy used")
        else:
            # ── Pre-attach indicators (matching backtest exactly) ─────────
            import numpy as np
            df_15m = df_15m.copy()
            df_15m['EMA_21']  = df_15m['close'].ewm(span=21, adjust=False).mean()
            df_15m['SMA_50']  = df_15m['close'].rolling(50).mean()
            df_15m['SMA_200'] = df_15m['close'].rolling(200).mean()
            delta = df_15m['close'].diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            df_15m['rsi_14'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

            from engines.hybrid_strategy import HybridStrategyAgent
            strategy = HybridStrategyAgent()
            
            # ── Pre-compute SMC catalogs (matching backtest architecture) ─
            fvg_catalog = strategy._build_fvg_catalog(df_15m)
            ob_catalog  = strategy._build_ob_catalog(df_15m)
            structure   = strategy._build_structure(df_15m)
            
            context = strategy.analyze_market_context(
                symbol, df_15m,
                i=len(df_15m) - 1,
                fvg_catalog=fvg_catalog,
                ob_catalog=ob_catalog,
                structure=structure
            )

        if "error" in context:
            return None

        confidence   = context.get("Confidence_Pct", 0)
        grade_str    = context.get("Structure_Score", "C")
        macro_trend  = context.get("Macro_Trend", "Unknown")
        current_price = context.get("Current_Price", df_15m['close'].iloc[-1])

        # ── Resolve direction from macro_trend + bias engine ──────────────
        # The SMA-cross macro_trend returns "Unknown" in choppy markets
        # (SMA50 > SMA200 but price < EMA21, or vice versa).
        # Instead of blocking ALL trades, fall back to the daily bias engine.
        from engines.daily_bias_engine import bias_engine
        bias_data = bias_engine.get_bias(symbol)
        bias_dir  = bias_data.get("bias", "NO_TRADE") if bias_data else "NO_TRADE"

        if macro_trend in ("Bullish", "Bearish"):
            # Strategy agent has a clear trend — use it
            direction = "buy" if macro_trend == "Bullish" else "sell"
        elif bias_dir in ("BUY", "SELL"):
            # Macro trend unclear but bias engine has a view — use bias
            direction = "buy" if bias_dir == "BUY" else "sell"
            logger.info(
                f"[TradingLoop] {symbol}: macro_trend=Unknown, using bias={bias_dir} as direction"
            )
        else:
            # Both Unknown AND no bias — genuinely no trade
            self._log_signal(symbol, "skip", "No trend: macro=Unknown, bias=NO_TRADE",
                             confidence, grade_str, context, df_1h=df_15m)
            return None

        # Validate direction agrees with bias (if bias has a view)
        if bias_dir in ("BUY", "SELL"):
            expected_dir = "buy" if bias_dir == "BUY" else "sell"
            if direction != expected_dir:
                self._log_signal(symbol, "skip",
                                 f"Bias conflict: bias={bias_dir} signal={direction}",
                                 confidence, grade_str, context, df_1h=df_15m)
                return None
        elif bias_dir == "NO_TRADE":
            # Bias explicitly says don't trade this instrument today
            if not bias_engine.is_tradeable_today(symbol, direction):
                self._log_signal(symbol, "skip",
                                 f"Bias=NO_TRADE for {symbol} today",
                                 confidence, grade_str, context, df_1h=df_15m)
                return None

        grade = None
        for g in ("A+", "A", "B+", "B"):
            if g in grade_str:
                grade = g
                break
        if grade is None:
            return None

        # ── ASSET_PARAMS grade gate (matching backtest exactly) ───────────
        from config.config import get_asset_params
        asset_p = get_asset_params(symbol)
        
        asset_min_grade = asset_p.get("min_grade", "B+")
        allowed_grades = {"A+": ["A+"], "A": ["A+", "A"], "B+": ["A+", "A", "B+"]}
        if grade not in allowed_grades.get(asset_min_grade, ["A+", "A", "B+"]):
            logger.info(f"[TradingLoop] {symbol}: Grade {grade} rejected (min required: {asset_min_grade})")
            return None

        # ── ASSET_PARAMS confidence gate (matching backtest exactly) ──────
        asset_min_conf = asset_p.get("min_conf", 50)
        inst_conf_gate = CONFIDENCE_GATES.get(symbol, CONFIDENCE_GATES.get("default", 0.50)) * 100
        min_conf = max(asset_min_conf, inst_conf_gate)
        
        if confidence < min_conf:
            logger.info(f"[TradingLoop] {symbol}: SMC confidence {confidence}% below gate {min_conf}%")
            return None

        mtf_ok, mtf_modifier, mtf_reason = check_mtf_confluence(symbol, direction, df_1h, df_4h)
        if not mtf_ok:
            self._log_signal(symbol, "skip", f"MTF conflict: {mtf_reason}",
                             confidence, grade_str, context, df_1h=df_15m)
            return None
            
        if mtf_modifier and grade in ("A+", "A", "B+"):
            logger.info(f"[TradingLoop] {symbol}: Downgrading grade from {grade} to {mtf_modifier} due to MTF: {mtf_reason}")
            grade = mtf_modifier
            grade_str = f"Grade {grade}"



        corr_ok, corr_reason = self._correlation_check(symbol, direction)
        if not corr_ok:
            return None

        # ── Zone 1: Order Flow Delta Confirmation ─────────────────────────
        # If price is at an OB, check delta confirms the direction.
        # Dead OBs have negative delta at the level — skip them.
        try:
            from intelligence.order_flow import (
                check_delta_confirmation
            )
            obs = context.get("Order_Blocks", [])
            if obs:
                ob_zone = obs[0].get("zone", [0, 0])
                if ob_zone[0] > 0:
                    df_5m = _get_ohlcv_with_ws_fallback(symbol, "5m", 50)
                    if df_5m is not None and len(df_5m) >= 10:
                        delta_check = check_delta_confirmation(
                            df_5m, ob_zone, direction
                        )
                        if delta_check.get("signal") == "SKIP":
                            logger.info(
                                f"[TradingLoop] {symbol}: "
                                f"DEAD OB — {delta_check['reason']}"
                            )
                            self._log_signal(
                                symbol, "skip",
                                f"Dead OB: {delta_check['reason']}",
                                confidence, grade_str, context, df_1h=df_15m
                            )
                            return None
                        if delta_check.get("signal") == "WAIT":
                            logger.debug(
                                f"[TradingLoop] {symbol}: "
                                f"Delta WAIT — {delta_check['reason']}"
                            )
                            self._add_to_watchlist(symbol, {"symbol": symbol, "score": 7})
                            return None
        except Exception as e:
            logger.debug(f"[TradingLoop] Delta check error (non-fatal): {e}")

        # ── Zone 2: Jarvis Sentiment Override ────────────────────────────
        # Check macro narrative before executing signal.
        try:
            from intelligence.jarvis_llm import get_jarvis
            jarvis = get_jarvis()
            if jarvis.is_ready():
                override = jarvis.get_sentiment_override(symbol, direction)
                action   = override.get("action", "PROCEED")
                if action == "NO_TRADE":
                    logger.info(
                        f"[TradingLoop] {symbol}: Jarvis veto — "
                        f"{override['reason']}"
                    )
                    self._log_signal(
                        symbol, "skip",
                        f"Jarvis: {override['reason']}",
                        confidence, grade_str, context, df_1h=df_15m
                    )
                    return None
                if action == "HALF_SIZE":
                    self._jarvis_half_size = True
                    logger.info(
                        f"[TradingLoop] {symbol}: Jarvis half-size — "
                        f"{override['reason']}"
                    )
                else:
                    self._jarvis_half_size = False
        except Exception as e:
            logger.debug(f"[TradingLoop] Jarvis check error (non-fatal): {e}")
            self._jarvis_half_size = False

        # ── Pre-calculate Risk Pct BEFORE the portfolio gate ──────────────
        from core.risk_agent import RiskAgent
        from core.streak_state import streak

        risk_pct = streak.get_current_risk_pct(grade)
        
        # If volatile regime → halve size
        if getattr(self, "_volatile_override", False):
            risk_pct = round(risk_pct * 0.5, 4)
            logger.info(
                f"[TradingLoop] {symbol}: Volatile regime - "
                f"size halved to {risk_pct:.2f}%"
            )
        
        # If Jarvis flagged half-size (sentiment conflict)
        if getattr(self, "_jarvis_half_size", False):
            risk_pct = round(risk_pct * 0.5, 4)
            logger.info(
                f"[TradingLoop] {symbol}: Jarvis override - "
                f"size halved to {risk_pct:.2f}%"
            )

        # Apply Prop Firm scaling
        if prop_risk_multiplier != 1.0:
            risk_pct = round(risk_pct * prop_risk_multiplier, 4)
            logger.info(f"[TradingLoop] {symbol}: Prop Firm guard scaled risk by {prop_risk_multiplier}x to {risk_pct}%")

        # ── Portfolio-level risk gate ─────────────────────────────────────
        try:
            from risk.portfolio_risk_engine import get_portfolio_risk
            pr_ok, pr_reason = get_portfolio_risk().can_add_position(
                symbol, risk_pct, direction
            )
            if not pr_ok:
                logger.info(f"[TradingLoop] {symbol}: Portfolio gate - {pr_reason}")
                self._log_signal(symbol, "skip", f"Portfolio: {pr_reason}",
                                 confidence, grade_str, context, df_1h=df_15m)
                return None
        except Exception as e:
            logger.debug(f"[TradingLoop] Portfolio gate error (non-fatal): {e}")

        risk_agent = RiskAgent()
        risk_agent.max_risk_per_trade = risk_pct / 100

        equity = self._get_current_equity()

        signal_dict = {
            "action":          direction,
            "price":           current_price,
            "symbol":          symbol,
            "is_swing_trade":  context.get("Is_Swing_Trade", False),
        }

        validated = risk_agent.validate_trade_signal(
            current_equity = equity,
            signal         = signal_dict,
            df             = df_15m,
            confidence_pct = confidence,
        )

        if not validated.get("approved"):
            return None

        validated["grade"]     = grade
        validated["risk_pct"]  = risk_pct
        validated["is_paper"]  = self._is_paper
        validated["exchange"]  = self._get_exchange_for(symbol)
        validated["node"]      = self._get_node_name()

        # ── Hybrid Execution: OB Limit Order Logic ────────────────────────
        # Detect the nearest Order Block boundary for precise limit entry.
        # Node-aware: AWS uses strict postOnly, local uses standard limits.
        obs = context.get("Order_Blocks", [])
        ob_limit_price = None
        if obs:
            nearest_ob = obs[0]
            ob_zone = nearest_ob.get("zone", [0, 0])
            if ob_zone[0] > 0:
                # BUY: enter at top of OB (demand), SELL: enter at bottom (supply)
                if direction in ("buy", "long"):
                    ob_limit_price = float(ob_zone[1])  # top of demand OB
                else:
                    ob_limit_price = float(ob_zone[0])  # bottom of supply OB

        node_name = self._get_node_name()
        is_cloud = node_name in ("aws", "gcp", "oracle")

        if ob_limit_price and ob_limit_price > 0:
            validated["order_type"]       = "limit"
            validated["limit_price"]      = round(ob_limit_price, 5)
            validated["strict_post_only"] = is_cloud  # True on AWS, False on laptop/phone
            logger.info(
                f"[TradingLoop] {symbol}: Limit @ {ob_limit_price:.5f} "
                f"({'postOnly' if is_cloud else 'standard'})"
            )
        else:
            validated["order_type"]       = "market"
            validated["strict_post_only"] = False

        logger.info(
            f"[TradingLoop] SIGNAL: {symbol} {direction.upper()} "
            f"grade={grade} conf={confidence}% risk={risk_pct:.2f}% "
            f"{'[PAPER]' if self._is_paper else '[LIVE]'}"
        )

        # Generate signal_id BEFORE execution so it can be stored in position
        signal_id = str(uuid.uuid4())[:8].upper()
        validated["signal_id"] = signal_id

        result = self._execute(validated, current_price)

        if result and result.get("status") in ("filled", "paper_filled"):
            self._trades_today += 1
            self._log_signal(symbol, "traded", "Executed",
                             confidence, grade_str, context,
                             validated=validated, df_1h=df_1h,
                             signal_id=signal_id)
            return {"executed": True, "trade_id": result.get("trade_id")}

        self._log_signal(symbol, "failed",
                         result.get("reason", "execution failed") if result else "no result",
                         confidence, grade_str, context, df_1h=df_1h)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # EXECUTION
    # ─────────────────────────────────────────────────────────────────────────

    def _execute(self, validated: dict, current_price: float) -> dict:
        """
        Route to broker_router which handles:
          - Paper mode simulation (all instruments)
          - Exchange routing (Binance / Alpaca / Zerodha)
          - Market-specific pre-checks (earnings, circuit breakers, PDT)
          - Share vs unit sizing (stocks need shares, not lot_size)
        """
        try:
            from brokers.broker_router import get_router
            return get_router().execute(validated, current_price,
                                        is_paper=self._is_paper)
        except Exception as e:
            logger.error(f"[TradingLoop] Router error: {e}")
            # Fallback to original paths
            if self._is_paper:
                return self._paper_execute(validated, current_price)
            return self._live_execute(validated, current_price)

    def _paper_execute(self, validated: dict, current_price: float) -> dict:
        """
        FIX M4: Slippage now owned exclusively by paper_engine.simulate_fill().
        Old code duplicated slippage with a flat pip_size*2 formula for all
        assets. paper_engine uses asset-class-aware slippage (crypto != forex).
        """
        symbol    = validated["symbol"]
        direction = validated["direction"]
        trade_id  = validated.get("trade_id") or str(uuid.uuid4())[:8].upper()
        signal_id = validated.get("signal_id")

        # FIX M4: delegate all slippage logic to paper_engine
        try:
            from core.paper_trading import get_paper_engine
            fill_data  = get_paper_engine().simulate_fill(validated, current_price)
            fill_price = fill_data["fill_price"]
        except Exception:
            fill_price = current_price   # fallback

        from core.position_tracker import positions
        positions.open({
            **validated,
            "trade_id":    trade_id,
            "entry":       fill_price,
            "entry_price": fill_price,
            "is_paper":    True,
            "exchange":    "paper",
            "signal_id":   signal_id,   # FIX: enables ML backfill
        })

        try:
            from interfaces.telegram_interface import tg
            tg.send_trade_open({
                **validated,
                "trade_id":    trade_id,
                "entry_price": fill_price,
                "current_sl":  validated["stop_loss"],
                "tp1_price":   validated.get("take_profit_1"),
                "current_tp":  validated.get("take_profit_2"),
                "is_paper":    True,
            })
        except Exception:
            pass

        logger.info(
            f"[TradingLoop] 📄 PAPER FILLED: {symbol} {direction.upper()} "
            f"@ {fill_price} | ID={trade_id}"
        )
        return {"status": "paper_filled", "trade_id": trade_id,
                "fill_price": fill_price}

    def _live_execute(self, validated: dict, current_price: float) -> dict:
        try:
            from core.execution_agent import ExecutionAgent
            from core.data_agent import get_data_agent
            exchange = validated.get("exchange", "alpaca")
            ea = ExecutionAgent(data_agent=get_data_agent())
            return ea.execute_trade(validated, current_price, exchange=exchange)
        except Exception as e:
            logger.error(f"[TradingLoop] Live execution error: {e}")
            return {"status": "failed", "reason": str(e)}

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 4 - Paper equity used for sizing
    # ─────────────────────────────────────────────────────────────────────────

    def _get_current_equity(self) -> float:
        """
        FIX 4: Now reads compounded paper equity, not fixed starting equity.
        Without this, position sizes were identical on trade #1 and trade #100
        even if paper equity had grown 20% - no compounding benefit.
        """
        if self._is_paper:
            try:
                from core.paper_trading import get_paper_engine
                return get_paper_engine().get_equity()
            except Exception:
                from config.config import PAPER_TRADING
                return float(PAPER_TRADING.get("starting_equity", 10000))

        # Live mode: read from broker
        try:
            from core.data_agent import get_data_agent
            da      = get_data_agent()
            account = da.alpaca.get_account() if da.alpaca else None
            if account:
                return float(account.equity)
        except Exception:
            pass

        return 10000.0

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 3 - ML features complete (26 fields)
    # ─────────────────────────────────────────────────────────────────────────

    def _log_signal(self, symbol: str, status: str, reason: str,
                     confidence: int, grade: str, context: dict,
                     validated: dict = None,
                     df_1h=None,
                     signal_id: str = None):
        """
        FIX 3: Now calls feature_engineering.extract_features() for full
        26-field feature set. Old inline dict only had 10 fields.

        Missing fields were: atr_pct, atr_expansion, above_ema21/50/200,
        ema21_above_ema50, dist_to_swing_high/low_atr, pd_position,
        rsi_divergence_type, volume_signal, volume_expanding,
        structure_event, asset_class, funding_rate, day_of_week.

        All 16 missing fields are now captured automatically.
        """
        try:
            from core.database_manager import db

            # Generate signal_id if not provided
            sid = signal_id or str(uuid.uuid4())[:8].upper()

            # FIX M2: Correct session detection
            h            = datetime.now(timezone.utc).hour
            session_name = _get_session_name(h)

            db.save_signal({
                "signal_id":       sid,
                "symbol":          symbol,
                "direction":       "buy" if context.get("Macro_Trend") == "Bullish" else "sell",
                "confidence":      confidence,
                "grade":           grade,
                "was_traded":      status == "traded",
                "skip_reason":     reason if status != "traded" else None,
                "entry_price":     context.get("Current_Price"),
                "atr":             validated.get("atr") if validated else None,
                "session":         session_name,
                "macro_trend":     context.get("Macro_Trend"),
                "fvg_hit":         any(
                    f.get("price_inside")
                    for f in context.get("Recent_FVGs", [])
                ),
                "ob_hit":          any(
                    ob.get("zone", [0,0])[0] <=
                    context.get("Current_Price", 0) <=
                    ob.get("zone", [0,1])[1]
                    for ob in context.get("Order_Blocks", [])
                ),
                "sweep_detected":  context.get(
                    "Liquidity_Sweep", {}
                ).get("sweep_detected", False),
                "rsi_divergence":  context.get(
                    "RSI_Divergence", {}
                ).get("divergence", "none"),
                "volume_signal":   context.get("Volume_Delta", {}).get("signal", ""),
                "structure_event": context.get(
                    "Market_Structure", {}
                ).get("event", ""),
                "signal_time":     datetime.now(timezone.utc).isoformat(),
            })

            # FIX 3: Use feature_engineering for complete ML feature set
            if df_1h is not None:
                try:
                    from ml.feature_engineering import extract_features
                    features = extract_features(symbol, df_1h, context, session_name)
                except Exception as fe:
                    logger.debug(f"[TradingLoop] Feature extraction failed: {fe}")
                    # Fallback to basic features if extraction fails
                    features = {
                        "confidence":  confidence,
                        "grade":       grade,
                        "macro_trend": context.get("Macro_Trend"),
                        "utc_hour":    h,
                    }
            else:
                features = {"confidence": confidence, "utc_hour": h}

            db.save_ml_features(
                signal_id   = sid,
                symbol      = symbol,
                signal_time = datetime.now(timezone.utc).isoformat(),
                features    = features,
            )

        except Exception as e:
            logger.debug(f"[TradingLoop] Signal log error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # WATCHLIST
    # ─────────────────────────────────────────────────────────────────────────

    def _add_to_watchlist(self, symbol: str, inst_data: dict):
        if any(w["symbol"] == symbol for w in self._watchlist):
            return
        self._watchlist.append({
            "symbol":   symbol,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "score":    inst_data.get("score", 0),
        })
        logger.debug(f"[TradingLoop] Added {symbol} to watchlist (outside KZ)")

    def _process_watchlist(self):
        if not self._watchlist:
            return

        still_pending = []
        for item in self._watchlist:
            symbol = item["symbol"]
            in_kz, kz_name = is_in_kill_zone(symbol)

            if in_kz:
                from core.position_tracker import positions
                if not positions.has_open_position(symbol):
                    self._regime_checked_analyse(symbol, f"{kz_name} (watchlist)")
            else:
                added = datetime.fromisoformat(item["added_at"])
                age_h = (datetime.now(timezone.utc) - added).total_seconds() / 3600
                if age_h < 4:
                    still_pending.append(item)

        self._watchlist = still_pending

    # ─────────────────────────────────────────────────────────────────────────
    # CORRELATION GUARD - unchanged
    # ─────────────────────────────────────────────────────────────────────────

    def _correlation_check(self, symbol: str, direction: str) -> tuple:
        try:
            from core.position_tracker import positions
            from config.config import RISK

            max_corr    = RISK.get("max_correlated_exposure_pct", 2.0)
            usd_long    = {"EURUSD=X", "GBPUSD=X", "AUDUSD=X", "XAUUSD=X", "BTCUSDT", "ETHUSDT"}
            usd_short   = {"USDJPY=X", "USDCAD=X", "USDCHF=X"}
            total_risk  = 0.0

            for pos in positions.get_all():
                same_dir = (
                    pos["direction"] in ("buy","long") and direction in ("buy","long")
                ) or (
                    pos["direction"] in ("sell","short") and direction in ("sell","short")
                )
                if same_dir and pos["symbol"] in (usd_long | usd_short):
                    total_risk += pos.get("risk_pct", 1.0)

            if total_risk >= max_corr:
                return False, f"Correlated {total_risk:.1f}% >= limit {max_corr}%"
            return True, "OK"
        except Exception:
            return True, "OK"

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _get_exchange_for(self, symbol: str) -> str:
        from config.config import get_instrument
        return get_instrument(symbol).get("exchange", "paper")

    def _get_node_name(self) -> str:
        import socket
        hostname = socket.gethostname().upper()
        from config.config import NODES
        for name, cfg in NODES.items():
            if any(p.upper() in hostname for p in cfg.get("hostname_patterns", [])):
                return name
        return "unknown"


# ── Singleton ─────────────────────────────────────────────────────────────────
trading_loop = TradingLoop()

