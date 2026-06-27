"""
CRAVE v10.2 — Broker Router
==============================
Routes all trade execution to the correct broker based on the instrument's
exchange field in config.py.

ROUTING TABLE:
  binance  → data_agent.py Binance (existing, crypto futures)
  alpaca   → data_agent.py Alpaca (existing, forex/gold)
  alpaca   + asset_class in (stocks, indices) → alpaca_stocks_agent.py
  zerodha  → zerodha_agent.py (Indian markets)
  paper    → paper_trading.py simulate_fill() (no broker needed)
  yfinance → backtest data only, not routed to any live broker

The router also handles:
  - Pre-execution market checks (is market open?)
  - Earnings blackout enforcement for stocks
  - Circuit breaker checks for Indian stocks
  - PDT rule enforcement for US stocks
  - Share vs unit position sizing

USAGE:
  from brokers.broker_router import router

  result = router.execute(validated_signal, current_price, is_paper=True)
  # Returns: {"status": "paper_filled"/"filled"/"blocked", "trade_id": ...}
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.broker_router")


class BrokerRouter:

    def __init__(self):
        # Brokers loaded lazily — no crash on import
        self._alpaca_stocks = None
        self._zerodha       = None
        self._mt5           = None

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN EXECUTE
    # ─────────────────────────────────────────────────────────────────────────

    def execute(self, validated: dict, current_price: float,
                 is_paper: bool = True) -> dict:
        """
        Route a validated signal to the correct execution path.

        For paper mode: all instruments use paper_engine.simulate_fill().
        For live mode:  routed to exchange-specific agent.

        Pre-execution checks:
          1. Market open check (stocks only)
          2. Earnings blackout check (stocks with earnings_blackout=True)
          3. Circuit breaker check (Indian stocks)
          4. PDT rule check (US stocks, accounts < $25k)
          5. Share size calculation (stocks replace lot_size with shares)
        """
        symbol   = validated.get("symbol", "UNKNOWN")
        from config.config import get_instrument, get_asset_class
        inst     = get_instrument(symbol)
        exchange = inst.get("exchange", "paper")
        asset    = get_asset_class(symbol)

        # ── Paper mode: simulate fill, no broker needed ───────────────────
        if is_paper or exchange in ("paper", "yfinance"):
            return self._paper_fill(validated, current_price)

        # ── Pre-execution checks ──────────────────────────────────────────
        blocked, reason = self._pre_execution_checks(symbol, inst, asset, validated)
        if blocked:
            logger.warning(f"[Router] {symbol} blocked pre-execution: {reason}")
            return {"status": "blocked", "reason": reason}

        # ── Route to correct broker ───────────────────────────────────────
        if exchange == "mt5":
            return self._execute_mt5(validated, current_price)

        elif exchange == "binance":
            return self._execute_binance(validated, current_price)

        elif exchange == "alpaca":
            if asset in ("stocks", "indices", "etf"):
                return self._execute_alpaca_stocks(validated, current_price, inst)
            else:
                return self._execute_alpaca_existing(validated, current_price)

        elif exchange == "zerodha":
            return self._execute_zerodha(validated, current_price, inst)

        else:
            logger.error(f"[Router] Unknown exchange: {exchange} for {symbol}")
            return {"status": "failed", "reason": f"Unknown exchange: {exchange}"}

    # ─────────────────────────────────────────────────────────────────────────
    # PRE-EXECUTION CHECKS
    # ─────────────────────────────────────────────────────────────────────────

    def _pre_execution_checks(self, symbol: str, inst: dict,
                               asset: str, validated: dict) -> tuple:
        """
        Run all pre-execution checks. Returns (blocked: bool, reason: str).
        """
        # ── Market hours check ─────────────────────────────────────────────
        if asset in ("stocks", "indices") and inst.get("market") == "us_stocks":
            agent = self._get_alpaca_stocks()
            if not agent.is_market_open():
                return True, "US market is closed"

        if asset in ("stocks_india", "index_futures") and inst.get("market") == "india":
            now_h = datetime.now(timezone.utc).hour
            now_m = datetime.now(timezone.utc).minute
            now_t = now_h * 60 + now_m
            open_t  = 4 * 60        # 04:00 UTC = 09:30 IST
            close_t = 10 * 60       # 10:00 UTC = 15:30 IST
            if not (open_t <= now_t < close_t):
                return True, "NSE market is closed"

        # ── Earnings blackout (US stocks) ──────────────────────────────────
        if inst.get("earnings_blackout") and asset == "stocks":
            agent   = self._get_alpaca_stocks()
            blocked, reason = agent.is_earnings_blackout(symbol)
            if blocked:
                return True, reason

        # ── Circuit breaker (Indian stocks) ───────────────────────────────
        if asset in ("stocks_india", "index_futures"):
            agent = self._get_zerodha()
            ts    = inst.get("tradingsymbol", symbol)
            kx    = inst.get("kite_exchange", "NSE")
            if agent.is_authenticated() and agent.is_circuit_breaker_active(ts, kx):
                return True, f"{symbol} at circuit limit — cannot trade"

        # ── PDT rule (US stocks) ───────────────────────────────────────────
        if asset == "stocks":
            agent       = self._get_alpaca_stocks()
            equity      = agent.get_account_equity()
            pdt_ok, msg = agent.can_day_trade(equity)
            if not pdt_ok:
                return True, msg

        return False, "OK"

    def close_position(self, symbol: str, is_paper: bool, exchange: str) -> bool:
        """Route close request to the correct live broker if not paper."""
        if is_paper or exchange == "paper":
            return True

        logger.info(f"[Router] Sending LIVE close command for {symbol} on {exchange}")
        try:
            if exchange == "mt5":
                agent = self._get_mt5()
                return agent.close_position(crave_symbol=symbol)
            elif exchange == "alpaca":
                agent = self._get_alpaca_stocks()
                return agent.close_position(symbol)
            elif exchange == "zerodha":
                agent = self._get_zerodha()
                return agent.close_position(symbol)
            elif exchange == "binance":
                logger.warning(f"[Router] Binance live close not implemented yet for {symbol}")
                return False
            else:
                logger.warning(f"[Router] Unknown exchange {exchange} for close_position")
                return False
        except Exception as e:
            logger.error(f"[Router] Live close failed for {symbol}: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # EXECUTION PATHS
    # ─────────────────────────────────────────────────────────────────────────

    def _paper_fill(self, validated: dict, current_price: float) -> dict:
        """Route to paper engine simulation."""
        import uuid
        try:
            from core.paper_trading import get_paper_engine
            fill = get_paper_engine().simulate_fill(validated, current_price)
            if fill.get("status") == "pending":
                logger.info(f"[Router] Order for {validated['symbol']} pending: {fill.get('reason')}")
                return {"status": "pending", "reason": fill.get("reason")}

            trade_id = str(uuid.uuid4())[:8].upper()

            from core.position_tracker import positions
            positions.open({
                **validated,
                "trade_id":    trade_id,
                "entry":       fill["fill_price"],
                "entry_price": fill["fill_price"],
                "is_paper":    True,
                "exchange":    "paper",
                "signal_id":   validated.get("signal_id"),
            })

            return {
                "status":      "paper_filled",
                "trade_id":    trade_id,
                "fill_price":  fill["fill_price"],
                "slippage":    fill["slippage"],
            }
        except Exception as e:
            logger.error(f"[Router] Paper fill failed: {e}")
            return {"status": "failed", "reason": str(e)}

    def _execute_binance(self, validated: dict, current_price: float) -> dict:
        """Route to existing ExecutionAgent Binance path."""
        try:
            from core.execution_agent import ExecutionAgent
            from core.data_agent import DataAgent
            ea = ExecutionAgent(data_agent=DataAgent())
            return ea.execute_trade(validated, current_price, exchange="binance")
        except Exception as e:
            logger.error(f"[Router] Binance execution failed: {e}")
            return {"status": "failed", "reason": str(e)}

    def _execute_alpaca_existing(self, validated: dict,
                                   current_price: float) -> dict:
        """Route forex/gold to existing ExecutionAgent Alpaca path."""
        try:
            from core.execution_agent import ExecutionAgent
            from core.data_agent import DataAgent
            ea = ExecutionAgent(data_agent=DataAgent())
            return ea.execute_trade(validated, current_price, exchange="alpaca")
        except Exception as e:
            logger.error(f"[Router] Alpaca execution failed: {e}")
            return {"status": "failed", "reason": str(e)}

    def _execute_alpaca_stocks(self, validated: dict, current_price: float,
                                 inst: dict) -> dict:
        """
        Route US stocks to AlpacaStocksAgent with share-based sizing.
        Replaces lot_size (units) with shares (integer) in the order.
        """
        try:
            agent  = self._get_alpaca_stocks()
            symbol = validated["symbol"]

            # Recalculate position size in shares
            equity     = agent.get_account_equity()
            risk_pct   = validated.get("risk_pct", 1.0)
            entry      = validated.get("entry", current_price)
            stop_loss  = validated.get("stop_loss")
            take_profit = validated.get("take_profit_2") or validated.get("take_profit")

            shares = agent.calculate_share_size(
                equity, risk_pct, entry, stop_loss
            )
            if shares <= 0:
                return {"status": "failed", "reason": "Share size calculation returned 0"}

            result = agent.place_order(
                symbol     = symbol,
                direction  = validated["direction"],
                shares     = shares,
                stop_loss  = stop_loss,
                take_profit = take_profit,
            )

            if result:
                # Register in position tracker
                import uuid
                trade_id = str(uuid.uuid4())[:8].upper()
                from core.position_tracker import positions
                positions.open({
                    **validated,
                    "trade_id":    trade_id,
                    "lot_size":    shares,
                    "entry_price": current_price,
                    "is_paper":    False,
                    "exchange":    "alpaca",
                    "signal_id":   validated.get("signal_id"),
                })
                return {"status": "filled", "trade_id": trade_id, "shares": shares}

            return {"status": "failed", "reason": "Order submission failed"}

        except Exception as e:
            logger.error(f"[Router] Alpaca stocks execution failed: {e}")
            return {"status": "failed", "reason": str(e)}

    def _execute_zerodha(self, validated: dict, current_price: float,
                          inst: dict) -> dict:
        """Route Indian stocks to ZerodhaAgent."""
        try:
            from config.config import get_lot_size
            agent         = self._get_zerodha()
            symbol        = validated["symbol"]
            tradingsymbol = inst.get("tradingsymbol", symbol)
            kite_exchange = inst.get("kite_exchange", "NSE")
            lot_size      = get_lot_size(tradingsymbol)

            # For F&O, quantity = lots × lot_size
            lots     = max(1, int(validated.get("lot_size", 1)))
            quantity = lots * lot_size if lot_size > 1 else lots

            # Use bracket order for intraday
            result = agent.place_bracket_order(
                tradingsymbol = tradingsymbol,
                direction     = validated["direction"],
                quantity      = quantity,
                entry_price   = validated.get("entry", current_price),
                stop_loss     = validated["stop_loss"],
                target        = validated.get("take_profit_2") or validated.get("take_profit"),
                kite_exchange = kite_exchange,
            )

            if result:
                import uuid
                trade_id = str(uuid.uuid4())[:8].upper()
                from core.position_tracker import positions
                positions.open({
                    **validated,
                    "trade_id":    trade_id,
                    "lot_size":    quantity,
                    "entry_price": current_price,
                    "is_paper":    False,
                    "exchange":    "zerodha",
                    "signal_id":   validated.get("signal_id"),
                })
                return {"status": "filled", "trade_id": trade_id, "quantity": quantity}

            return {"status": "failed", "reason": "Zerodha order failed"}

        except Exception as e:
            logger.error(f"[Router] Zerodha execution failed: {e}")
            return {"status": "failed", "reason": str(e)}

    # ─────────────────────────────────────────────────────────────────────────
    # BROKER ACCESSORS (lazy)
    # ─────────────────────────────────────────────────────────────────────────

    def _get_alpaca_stocks(self):
        if self._alpaca_stocks is None:
            from brokers.alpaca_stocks_agent import get_alpaca_stocks
            self._alpaca_stocks = get_alpaca_stocks()
        return self._alpaca_stocks

    def _get_zerodha(self):
        if self._zerodha is None:
            from brokers.zerodha_agent import get_zerodha
            self._zerodha = get_zerodha()
        return self._zerodha

    def _get_mt5(self):
        if self._mt5 is None:
            from brokers.mt5_agent import get_mt5
            self._mt5 = get_mt5()
        return self._mt5

    def _execute_mt5(self, validated: dict, current_price: float) -> dict:
        """
        Route forex/gold/crypto to MetaTrader 5.
        Falls back to paper mode if MT5 is not available (e.g. on AWS/Linux).
        """
        try:
            agent = self._get_mt5()
            if not agent.ensure_connected():
                logger.warning("[Router] MT5 not available — falling back to paper mode")
                return self._paper_fill(validated, current_price)

            symbol    = validated["symbol"]
            direction = validated["direction"]
            entry     = validated.get("entry", current_price)
            sl        = validated["stop_loss"]
            tp        = validated.get("take_profit_2") or validated.get("take_profit")

            # Calculate lot size using MT5's tick value for accurate sizing
            equity   = agent.get_account_info()
            eq_value = equity["equity"] if equity else 10000
            risk_pct = validated.get("risk_pct", 1.0)

            lot_size = agent.calculate_lot_size(
                crave_symbol=symbol,
                equity=eq_value,
                risk_pct=risk_pct,
                entry=entry,
                sl=sl,
            )

            result = agent.place_order(
                crave_symbol=symbol,
                direction=direction,
                lot_size=lot_size,
                sl=sl,
                tp=tp,
                comment=f"CRAVE {validated.get('grade', '?')}",
            )

            if result.get("status") == "filled":
                import uuid
                trade_id = str(uuid.uuid4())[:8].upper()

                from core.position_tracker import positions
                positions.open({
                    **validated,
                    "trade_id":     trade_id,
                    "entry":        result["fill_price"],
                    "entry_price":  result["fill_price"],
                    "lot_size":     result["volume"],
                    "is_paper":     False,
                    "exchange":     "mt5",
                    "mt5_ticket":   result["ticket"],
                    "signal_id":    validated.get("signal_id"),
                })

                logger.info(
                    f"[Router] MT5 FILLED ✅ | {symbol} {direction.upper()} "
                    f"| Lot: {result['volume']} | Price: {result['fill_price']} "
                    f"| Ticket: {result['ticket']}"
                )
                return {"status": "filled", "trade_id": trade_id,
                        "fill_price": result["fill_price"],
                        "mt5_ticket": result["ticket"]}

            logger.warning(f"[Router] MT5 order failed: {result.get('reason')}")
            # Fall back to paper if MT5 execution fails
            logger.info("[Router] Falling back to paper fill")
            return self._paper_fill(validated, current_price)

        except Exception as e:
            logger.error(f"[Router] MT5 execution error: {e}")
            return self._paper_fill(validated, current_price)

    # ─────────────────────────────────────────────────────────────────────────
    # STATUS
    # ─────────────────────────────────────────────────────────────────────────

    def get_status_message(self) -> str:
        lines = ["📡 <b>BROKER STATUS</b>", "━━━━━━━━━━━━━━━"]

        # Existing brokers
        try:
            from core.data_agent import DataAgent
            da = DataAgent()
            lines.append(f"{'✅' if da.binance else '❌'} Binance: "
                         f"{'connected' if da.binance else 'not configured'}")
            lines.append(f"{'✅' if da.alpaca else '❌'} Alpaca: "
                         f"{'connected' if da.alpaca else 'not configured'}")
        except Exception:
            pass

        # US Stocks
        try:
            ag = self._get_alpaca_stocks()
            lines.append(f"{'✅' if ag.is_authenticated() else '❌'} Alpaca Stocks: "
                         f"{'connected' if ag.is_authenticated() else 'not configured'}")
        except Exception:
            lines.append("❌ Alpaca Stocks: error")

        # India
        try:
            zr = self._get_zerodha()
            lines.append(f"{'✅' if zr.is_authenticated() else '❌'} Zerodha: "
                         f"{'authenticated' if zr.is_authenticated() else 'needs daily login'}")
        except Exception:
            lines.append("❌ Zerodha: error")

        # MT5
        try:
            mt5 = self._get_mt5()
            lines.append(f"{'✅' if mt5.is_connected() else '❌'} MT5: "
                         f"{'connected' if mt5.is_connected() else 'not connected'}")
        except Exception:
            lines.append("❌ MT5: not available")

        return "\n".join(lines)


# ── Singleton ─────────────────────────────────────────────────────────────────
_router_instance: Optional[BrokerRouter] = None

def get_router() -> BrokerRouter:
    global _router_instance
    if _router_instance is None:
        _router_instance = BrokerRouter()
    return _router_instance

router = get_router
