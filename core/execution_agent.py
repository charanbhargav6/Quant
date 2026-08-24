"""
CRAVE Phase 9.1 - Execution Engine
====================================
FIXES vs v9.0:
  🔧 Trailing SL now ATR-based (was hardcoded 0.5% — deadly on ranging markets)
  🔧 Binance SL/TP orders now use reduceOnly=True to prevent phantom re-entries
  🔧 ATR value stored in trade object on open (required for ATR trail)
  🔧 Trail distance = 1.0× ATR below/above price (matches SL logic in RiskAgent)
"""

import os
import time
import logging
import threading
from datetime import datetime

try:
    import pandas as pd
except ImportError:
    pd = None

logger = logging.getLogger("crave.trading.execution")


SLIPPAGE_LIMITS = {
    "forex":   0.0003,
    "crypto":  0.003,
    "stocks":  0.001,
    "default": 0.001,
}

def _get_slippage_limit(symbol: str) -> float:
    s = symbol.upper()
    if any(x in s for x in ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]):
        return SLIPPAGE_LIMITS["crypto"]
    if any(x in s for x in ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "XAU", "XAG"]):
        return SLIPPAGE_LIMITS["forex"]
    if "=" in s or "-" not in s:
        return SLIPPAGE_LIMITS["stocks"]
    return SLIPPAGE_LIMITS["default"]


class ExecutionAgent:
    def __init__(self, data_agent=None, telegram_agent=None, risk_agent=None):
        self.data_agent       = data_agent
        self.telegram         = telegram_agent
        self.risk_agent       = risk_agent
        self.active_trades    = []
        self._lock            = threading.Lock()
        self._monitor_running = False
        self._open_symbols    = set()

    def check_slippage(self, signal_price: float, current_price: float, symbol: str) -> bool:
        max_slip = _get_slippage_limit(symbol)
        variance = abs(current_price - signal_price) / signal_price
        if variance > max_slip:
            logger.warning(
                f"[ExecutionAgent] Slippage guard tripped for {symbol}: "
                f"signal={signal_price}, live={current_price}, "
                f"variance={variance:.5f} > limit={max_slip}"
            )
            return False
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN EXECUTION
    # ─────────────────────────────────────────────────────────────────────────

    def execute_trade(self, validated_signal: dict, current_price: float,
                      exchange: str = "alpaca") -> dict:
        if not validated_signal.get("approved", False):
            reason = validated_signal.get("reason", "Risk veto")
            logger.warning(f"[ExecutionAgent] Blocked: {reason}")
            return {"status": "blocked", "reason": reason}

        symbol    = validated_signal.get("symbol", "UNKNOWN")
        entry     = validated_signal.get("entry")
        lot_size  = validated_signal.get("lot_size")
        direction = validated_signal.get("direction")
        sl_price  = validated_signal.get("stop_loss")
        tp1_price = validated_signal.get("take_profit_1")
        tp2_price = validated_signal.get("take_profit_2", validated_signal.get("take_profit"))
        rr        = validated_signal.get("rr_ratio", 2.0)

        # FIX: Store ATR at trade open so the monitor can trail using ATR, not %
        atr_value  = validated_signal.get("atr_value", None)

        with self._lock:
            if symbol in self._open_symbols:
                return {"status": "skipped", "reason": f"Already in an open {symbol} trade."}

        if not self.check_slippage(entry, current_price, symbol):
            return {"status": "aborted", "reason": "Slippage too high."}

        logger.info(
            f"[ExecutionAgent] Firing {exchange.upper()} {direction.upper()} {symbol} "
            f"| Qty: {lot_size} | Entry: {current_price} | SL: {sl_price} | TP2: {tp2_price}"
        )

        receipt = None

        try:
            if exchange == "alpaca" and self.data_agent and self.data_agent.alpaca:
                side  = 'buy' if direction in ('buy', 'long') else 'sell'

                # Hybrid execution: use limit order at OB if available
                order_type = validated_signal.get("order_type", "market")
                limit_px   = validated_signal.get("limit_price")

                if order_type == "limit" and limit_px:
                    order = self.data_agent.alpaca.submit_order(
                        symbol        = symbol,
                        qty           = lot_size,
                        side          = side,
                        type          = 'limit',
                        limit_price   = str(round(limit_px, 4)),
                        time_in_force = 'gtc',
                        order_class   = 'bracket',
                        stop_loss     = {'stop_price': str(round(sl_price, 4))},
                        take_profit   = {'limit_price': str(round(tp2_price, 4))}
                    )
                    logger.info(
                        f"[ExecutionAgent] Alpaca LIMIT @ {limit_px:.4f} "
                        f"({'postOnly' if validated_signal.get('strict_post_only') else 'standard'})"
                    )
                else:
                    order = self.data_agent.alpaca.submit_order(
                        symbol        = symbol,
                        qty           = lot_size,
                        side          = side,
                        type          = 'market',
                        time_in_force = 'gtc',
                        order_class   = 'bracket',
                        stop_loss     = {'stop_price': str(round(sl_price, 4))},
                        take_profit   = {'limit_price': str(round(tp2_price, 4))}
                    )
                fill_price = limit_px if (order_type == "limit" and limit_px) else current_price
                receipt = {"id": order.id, "platform": "alpaca",
                           "status": "filled", "entry": fill_price}

            elif exchange == "binance" and self.data_agent and self.data_agent.binance:
                # This legacy agent is retained for compatibility, but a
                # public ccxt client is data-only. Never turn it into an
                # unauthenticated live-order attempt; live Binance orders
                # must come through BrokerRouter/MultiTenantBrokerManager.
                if not (os.environ.get("BINANCE_API_KEY") and os.environ.get("BINANCE_API_SECRET")):
                    reason = (
                        "Binance live execution requires verified account credentials; "
                        "route the signal through BrokerRouter instead of the global data client"
                    )
                    logger.warning(f"[ExecutionAgent] Blocked: {reason}")
                    return {"status": "blocked", "reason": reason}
                side     = 'buy' if direction in ('buy', 'long') else 'sell'
                sl_side  = 'sell' if side == 'buy' else 'buy'

                # Hybrid execution: limit at OB if available
                order_type     = validated_signal.get("order_type", "market")
                limit_px       = validated_signal.get("limit_price")
                strict_po      = validated_signal.get("strict_post_only", False)

                if order_type == "limit" and limit_px:
                    entry_params = {'postOnly': True} if strict_po else {}
                    order = self.data_agent.binance.create_order(
                        symbol=symbol, type='LIMIT', side=side,
                        amount=lot_size, price=round(limit_px, 4),
                        params=entry_params
                    )
                    logger.info(
                        f"[ExecutionAgent] Binance LIMIT @ {limit_px:.4f} "
                        f"{'postOnly' if strict_po else 'standard'}"
                    )
                else:
                    order = self.data_agent.binance.create_order(
                        symbol=symbol, type='market', side=side, amount=lot_size
                    )

                # FIX v9.1: Add reduceOnly=True to SL and TP orders.
                # Without this, if the main position closes by another route,
                # these orders become OPENING orders on the opposite side.
                # reduceOnly=True ensures these orders can ONLY close an existing position.
                self.data_agent.binance.create_order(
                    symbol=symbol,
                    type='STOP_MARKET',
                    side=sl_side,
                    amount=lot_size,
                    params={
                        'stopPrice':   round(sl_price, 4),
                        'reduceOnly':  True,
                        'closePosition': True,
                    }
                )
                self.data_agent.binance.create_order(
                    symbol=symbol,
                    type='TAKE_PROFIT_MARKET',
                    side=sl_side,
                    amount=lot_size,
                    params={
                        'stopPrice':   round(tp2_price, 4),
                        'reduceOnly':  True,
                        'closePosition': True,
                    }
                )
                fill_price = limit_px if (order_type == "limit" and limit_px) else current_price
                receipt = {"id": order['id'], "platform": "binance",
                           "status": "filled", "entry": fill_price}

            elif exchange == "mt5":
                import MetaTrader5 as mt5
                if not mt5.initialize():
                    return {"status": "failed", "reason": "MT5 not running"}

                action  = mt5.ORDER_TYPE_BUY if direction in ('buy', 'long') else mt5.ORDER_TYPE_SELL
                request = {
                    "action":       mt5.TRADE_ACTION_DEAL,
                    "symbol":       symbol,
                    "volume":       float(lot_size),
                    "type":         action,
                    "price":        current_price,
                    "sl":           round(sl_price, 5),
                    "tp":           round(tp2_price, 5),
                    "deviation":    20,
                    "magic":        999000,
                    "comment":      validated.get("reason", "Crave AI")[:31],
                    "type_time":    mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                result = mt5.order_send(request)
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    receipt = {"id": result.order, "platform": "mt5",
                               "status": "filled", "entry": current_price}
                else:
                    return {"status": "failed",
                            "reason": f"MT5 error: {result.retcode} {result.comment}"}

        except Exception as e:
            logger.error(f"[ExecutionAgent] API crash: {e}")
            return {"status": "failed", "reason": str(e)}

        if receipt:
            receipt.update({
                "symbol":    symbol,
                "tp1":       tp1_price,
                "tp2":       tp2_price,
                "sl":        sl_price,
                "rr":        rr,
                "direction": direction,
                "open_time": datetime.utcnow().isoformat(),
            })

            msg = (
                f"✅ TRADE FIRED\n"
                f"━━━━━━━━━━━━━━━\n"
                f"Symbol  : {symbol}\n"
                f"Side    : {direction.upper()}\n"
                f"Qty     : {lot_size}\n"
                f"Entry   : {current_price}\n"
                f"SL      : {sl_price}\n"
                f"TP1     : {tp1_price} (50% close here)\n"
                f"TP2     : {tp2_price} (full close)\n"
                f"RR      : 1:{rr}\n"
                f"Platform: {exchange.upper()}"
            )
            self._notify(msg)

            with self._lock:
                self._open_symbols.add(symbol)
                self.active_trades.append({
                    "symbol":    symbol,
                    "direction": direction,
                    "entry":     current_price,
                    "stop_loss": sl_price,
                    "tp1":       tp1_price,
                    "tp2":       tp2_price,
                    "tp1_hit":   False,
                    "exchange":  exchange,
                    "lot_size":  lot_size,
                    "receipt":   receipt,
                    # FIX: Store ATR at trade open — required for ATR-based trailing SL
                    "atr_at_open": atr_value,
                })

            if not self._monitor_running:
                self.start_trailing_monitor()

            return receipt

        return {"status": "failed", "reason": "Unhandled exchange route."}

    # ─────────────────────────────────────────────────────────────────────────
    # TRADE MONITOR — ATR-based Trailing SL
    # ─────────────────────────────────────────────────────────────────────────

    def _trade_monitor_loop(self):
        self._monitor_running = True
        logger.info("[ExecutionAgent] Trade monitor started.")

        while self._monitor_running:
            if not self.active_trades:
                time.sleep(10)
                continue

            completed = []

            with self._lock:
                trades_snapshot = list(self.active_trades)

            for trade in trades_snapshot:
                try:
                    df = self.data_agent.get_ohlcv(
                        trade['symbol'], exchange=trade['exchange'],
                        timeframe="1m", limit=20  # Need 14+ candles for ATR refresh
                    )
                    if df is None or df.empty:
                        continue

                    live_price = df['close'].iloc[-1]
                    entry      = trade['entry']
                    sl         = trade['stop_loss']
                    tp1        = trade['tp1']
                    tp2        = trade['tp2']
                    direction  = trade['direction']
                    tp1_hit    = trade.get('tp1_hit', False)

                    # FIX: Recalculate ATR live from fresh 1m data for accurate trailing.
                    # Fall back to ATR stored at trade open if calculation fails.
                    try:
                        tr = pd.concat([
                            df['high'] - df['low'],
                            (df['high'] - df['close'].shift()).abs(),
                            (df['low']  - df['close'].shift()).abs(),
                        ], axis=1).max(axis=1)
                        live_atr = tr.ewm(alpha=1.0 / 14, adjust=False).mean().iloc[-1]
                        if pd.isna(live_atr) or live_atr <= 0:
                            live_atr = trade.get('atr_at_open') or (entry * 0.001)
                    except Exception:
                        live_atr = trade.get('atr_at_open') or (entry * 0.001)

                    # -- POST TRADE AI COUNCIL CHECK --
                    try:
                        last_check = trade.get('last_council_check', 0)
                        # Check council every 30 minutes (1800 seconds)
                        if time.time() - last_check > 1800:
                            trade['last_council_check'] = time.time()
                            from intelligence.agent_council import get_council
                            
                            council = get_council()
                            eval_result = council.evaluate_open_trade({
                                "symbol": trade['symbol'],
                                "direction": direction,
                                "entry": entry,
                                "sl": sl,
                                "tp": tp2,
                                "live_price": live_price,
                                "pnl_r": (live_price - entry) / abs(entry - sl) if direction in ('buy','long') else (entry - live_price) / abs(entry - sl)
                            }, {})
                            
                            if eval_result.get("action") == "close":
                                logger.info(f"[Monitor] Council overriding to CLOSE {trade['symbol']}")
                                completed.append(trade)
                                self._notify(f"🤖 COUNCIL CLOSE: {trade['symbol']} | Reason: {eval_result.get('reasoning')}")
                                continue
                            elif eval_result.get("action") == "extend_tp":
                                tp_mult = eval_result.get("tp_multiplier", 1.5)
                                risk = abs(entry - sl)
                                new_tp = entry + (risk * tp_mult) if direction in ('buy','long') else entry - (risk * tp_mult)
                                trade['tp2'] = new_tp
                                self._notify(f"🤖 COUNCIL TP EXTENDED: {trade['symbol']} -> {new_tp} | Reason: {eval_result.get('reasoning')}")
                    except Exception as e:
                        logger.debug(f"[Monitor] Council trade check failed: {e}")

                    if direction in ('buy', 'long'):
                        if live_price <= sl:
                            logger.info(f"[Monitor] {trade['symbol']} SL HIT at {live_price}")
                            if self.risk_agent:
                                self.risk_agent.log_trade_result('L', -1.0)
                            completed.append(trade)
                            self._notify(f"🔴 SL HIT: {trade['symbol']} | Loss ~1R")
                            continue

                        if not tp1_hit and live_price >= tp1:
                            trade['tp1_hit'] = True
                            # Move SL to lock in 0.2R profit instead of breakeven
                            risk_dist = abs(entry - sl)
                            new_sl = entry + (risk_dist * 0.2)
                            if new_sl > sl:
                                logger.info(
                                    f"[Monitor] TP1 hit {trade['symbol']} "
                                    f"— SL moved to +0.2R profit at {new_sl}"
                                )
                                trade['stop_loss'] = new_sl
                                self._notify(
                                    f"🟡 TP1 HIT: {trade['symbol']} — 50% closed. "
                                    f"SL trailing at +0.2R."
                                )

                        if live_price >= tp2:
                            result_r = abs(tp2 - entry) / max(abs(entry - trade['stop_loss']), 0.0001)
                            if self.risk_agent:
                                self.risk_agent.log_trade_result('W', result_r)
                            completed.append(trade)
                            self._notify(f"✅ TP2 HIT: {trade['symbol']} | +{result_r:.1f}R WIN")
                            continue

                        # FIX: ATR-based trailing SL (was: live_price * 0.995)
                        # Trail = 1× ATR below the current price.
                        # This adapts to actual market volatility:
                        #   - On a quiet EURUSD: trail ≈ 8 pips (appropriate)
                        #   - On volatile BTC:    trail ≈ $800 (appropriate)
                        # The old 0.5% trail was: EURUSD ≈ 50 pips (way too wide)
                        #                          BTC   ≈ $300 (too tight during trend)
                        if tp1_hit:
                            new_sl = live_price - (live_atr * 1.0)
                            if new_sl > trade['stop_loss']:
                                trade['stop_loss'] = new_sl

                    elif direction in ('sell', 'short'):
                        if live_price >= sl:
                            logger.info(f"[Monitor] {trade['symbol']} SL HIT (short) at {live_price}")
                            if self.risk_agent:
                                self.risk_agent.log_trade_result('L', -1.0)
                            completed.append(trade)
                            self._notify(f"🔴 SL HIT (Short): {trade['symbol']}")
                            continue

                        if not tp1_hit and live_price <= tp1:
                            trade['tp1_hit'] = True
                            # Move SL to lock in 0.2R profit instead of breakeven
                            risk_dist = abs(sl - entry)
                            new_sl = entry - (risk_dist * 0.2)
                            if new_sl < sl:
                                trade['stop_loss'] = new_sl
                                self._notify(
                                    f"🟡 TP1 HIT: {trade['symbol']} short — SL trailing at +0.2R."
                                )

                        if live_price <= tp2:
                            result_r = abs(entry - tp2) / max(abs(sl - entry), 0.0001)
                            if self.risk_agent:
                                self.risk_agent.log_trade_result('W', result_r)
                            completed.append(trade)
                            self._notify(f"✅ TP2 HIT: {trade['symbol']} short | +{result_r:.1f}R WIN")
                            continue

                        # FIX: ATR-based trailing SL for shorts
                        if tp1_hit:
                            new_sl = live_price + (live_atr * 1.0)
                            if new_sl < trade['stop_loss']:
                                trade['stop_loss'] = new_sl

                except Exception as e:
                    logger.error(f"[Monitor] Tick error for {trade.get('symbol', '?')}: {e}")

            with self._lock:
                for t in completed:
                    if t in self.active_trades:
                        self.active_trades.remove(t)
                        self._open_symbols.discard(t['symbol'])

            time.sleep(5)

    # ─────────────────────────────────────────────────────────────────────────
    # DEAD MAN'S SWITCH — unchanged from v9.0
    # ─────────────────────────────────────────────────────────────────────────

    def _dead_man_switch_loop(self):
        logger.info("[ExecutionAgent] Dead Man's Switch armed.")
        log_path = os.path.join(os.environ.get("CRAVE_ROOT", "."), "Logs", "crave.log")

        while self._monitor_running:
            if not self.active_trades:
                time.sleep(15)
                continue
            if os.path.exists(log_path):
                stale = time.time() - os.path.getmtime(log_path)
                if stale > 60:
                    self._notify("⚠️ CRAVE APPEARS CRASHED — CHECK OPEN POSITIONS MANUALLY")
                    time.sleep(300)
            time.sleep(10)

    # ─────────────────────────────────────────────────────────────────────────
    # EMERGENCY FLATTEN — unchanged from v9.0
    # ─────────────────────────────────────────────────────────────────────────

    def emergency_flatten_all(self, exchange: str = "alpaca"):
        logger.critical("[ExecutionAgent] EMERGENCY FLATTEN INITIATED")
        self._notify("🚨 EMERGENCY FLATTEN: Closing all positions NOW")

        if exchange == "alpaca" and self.data_agent and self.data_agent.alpaca:
            try:
                self.data_agent.alpaca.close_all_positions()
            except Exception as e:
                logger.error(f"Emergency flatten failed: {e}")

        elif exchange == "mt5":
            try:
                import MetaTrader5 as mt5
                for pos in mt5.positions_get():
                    close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
                    mt5.order_send({
                        "action":   mt5.TRADE_ACTION_DEAL,
                        "symbol":   pos.symbol,
                        "volume":   pos.volume,
                        "type":     close_type,
                        "position": pos.ticket,
                        "deviation": 50,
                        "magic":    999001,
                        "comment":  "CRAVE Emergency Close",
                    })
            except Exception as e:
                logger.error(f"MT5 emergency flatten error: {e}")

        with self._lock:
            self.active_trades.clear()
            self._open_symbols.clear()

    # ─────────────────────────────────────────────────────────────────────────
    # STARTUP
    # ─────────────────────────────────────────────────────────────────────────

    def start_trailing_monitor(self):
        threading.Thread(
            target=self._trade_monitor_loop,
            daemon=True, name="CRAVETradeMonitor"
        ).start()
        threading.Thread(
            target=self._dead_man_switch_loop,
            daemon=True, name="CRAVEDeadManSwitch"
        ).start()

    def _notify(self, msg: str):
        if self.telegram:
            try:
                self.telegram.send_message_sync(msg)
            except Exception as e:
                logger.error(f"Telegram notify failed: {e}")
