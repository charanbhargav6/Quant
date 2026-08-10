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

    def execute(self, validated: dict, current_price: float) -> dict:
        """
        Route a validated signal to the correct execution path.

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
        exchange = inst.get("exchange", "mt5")
        asset    = get_asset_class(symbol)

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

    def close_position(self, symbol: str, exchange: str) -> bool:
        """Route close request to the correct live broker."""

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

    def _active_broker_accounts(self, db, broker: str, strategy_id: str) -> list:
        """
        Shared multi-account filter for Zerodha/Binance, mirroring the
        pattern _execute_mt5 already uses: only 'connected' accounts of
        the right broker, with this strategy actually enabled for them.
        Centralized here so _execute_binance/_execute_zerodha don't each
        reimplement (and potentially drift on) the same filter logic.

        PHASE 5 FIX: previously only checked the account's own
        strategies_enabled toggle — a purely user-controlled preference.
        Nothing checked whether the strategy had actually PASSED
        walk-forward validation. A strategy correctly marked
        live_ready=False in core/strategy_registry.py (e.g. structure_silver,
        which degrades across folds and is flagged as likely overfit) could
        still be toggled on for a real account and would execute anyway.
        This now refuses at the execution gate regardless of the account's
        toggle — the account-level toggle can only narrow what's allowed,
        never expand past what's actually been validated.
        """
        import json
        from core.strategy_registry import is_live_ready

        if not is_live_ready(strategy_id):
            logger.warning(
                f"[Router] Strategy '{strategy_id}' is not live_ready "
                f"(failed or pending walk-forward validation) — refusing "
                f"to execute for ANY {broker} account regardless of "
                f"per-account strategies_enabled settings."
            )
            return []

        accounts = db.get_accounts()
        result = []
        for acc in accounts:
            if acc["status"] != "connected":
                continue
            if (acc.get("broker") or "").lower() != broker:
                continue
            try:
                enabled = json.loads(acc.get("strategies_enabled", '["all"]'))
            except Exception:
                enabled = ["all"]
            if "all" not in enabled and strategy_id not in enabled:
                logger.debug(f"[Router] Strategy {strategy_id} not enabled for account {acc.get('login')}, skipping.")
                continue
            result.append(acc)
        return result

    def _execute_binance(self, validated: dict, current_price: float) -> dict:
        """
        Route to BinanceAgent via MultiTenantBrokerManager, across all
        active Binance accounts. Consolidates what used to be a separate,
        disconnected path through ExecutionAgent/DataAgent's global-env
        Binance client — that client is still used for market data (see
        core/data_agent.py), but live order placement now goes through the
        same BinanceAgent class used to verify accounts at onboarding, so
        "verified" and "trading" are guaranteed to mean the same connection.
        """
        try:
            from core.database_manager import DatabaseManager
            from core.multi_tenant_broker_manager import get_multi_tenant_manager

            db = DatabaseManager()
            mgr = get_multi_tenant_manager(db)
            strategy_id = validated.get("strategy_id", "")
            # BUGFIX: this used to read validated["node"], which is the
            # machine hostname (laptop/phone/aws from config.NODES), not a
            # strategy identifier. That meant is_live_ready() never matched
            # anything and silently blocked 100% of live trades for a week.
            # See core/strategy_registry.py's module docstring (INCIDENT).
            active_accounts = self._active_broker_accounts(db, "binance", strategy_id)

            if not active_accounts:
                return {"status": "failed", "reason": "No connected Binance accounts have this strategy enabled"}

            symbol    = validated["symbol"]
            direction = validated["direction"]
            sl        = validated["stop_loss"]
            tp        = validated.get("take_profit_2") or validated.get("take_profit")
            risk_pct  = validated.get("risk_pct", 1.0)

            any_success = False
            first_trade_id = None
            last_error = "No accounts attempted"

            for acc in active_accounts:
                agent = mgr.get_agent(acc["id"])
                if agent is None:
                    last_error = f"Account {acc.get('login')} unavailable (see logs)"
                    continue

                info = agent.get_account_info()
                eq_value = info["equity"] if info else 1000.0
                # Binance sizing kept simple (fixed 0.01 lot per calculate_lot_size
                # stub in BinanceAgent) — flagged in strategy_architecture_research.md
                # as a known simplification, not something this phase changes.
                lot_size = agent.calculate_lot_size(symbol, eq_value, risk_pct, 0)

                ticket = agent.place_order(
                    crave_symbol=symbol, direction=direction,
                    lot_size=lot_size, sl=sl, tp=tp,
                    comment=f"CRAVE {validated.get('grade', '?')}",
                    order_type=validated.get("order_type", "market"),
                    limit_price=validated.get("limit_price"),
                    post_only=validated.get("post_only", False),
                )

                if ticket:
                    any_success = True
                    import uuid
                    trade_id = str(uuid.uuid4())[:8].upper()
                    if first_trade_id is None:
                        first_trade_id = trade_id
                    from core.position_tracker import positions
                    positions.open({
                        **validated,
                        "trade_id": trade_id, "lot_size": lot_size,
                        "entry_price": current_price, "is_paper": False,
                        "exchange": "binance", "account_id": acc["id"],
                        "binance_order_id": ticket,
                        "signal_id": validated.get("signal_id"),
                    })
                else:
                    last_error = f"Account {acc.get('login')} order placement failed"

            if any_success:
                return {"status": "filled", "trade_id": first_trade_id}
            return {"status": "failed", "reason": last_error}

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
        """
        Route Indian stocks to ZerodhaAgent via MultiTenantBrokerManager,
        across all active Zerodha accounts. Replaces the single-account
        _get_zerodha() singleton — that path is still used by /api/india
        status endpoints (read-only, fine to stay singleton), but live
        order placement now goes per-account.
        """
        try:
            from config.config import get_lot_size
            from core.database_manager import DatabaseManager
            from core.multi_tenant_broker_manager import get_multi_tenant_manager

            db = DatabaseManager()
            mgr = get_multi_tenant_manager(db)
            strategy_id = validated.get("strategy_id", "")
            # BUGFIX: this used to read validated["node"], which is the
            # machine hostname (laptop/phone/aws from config.NODES), not a
            # strategy identifier. That meant is_live_ready() never matched
            # anything and silently blocked 100% of live trades for a week.
            # See core/strategy_registry.py's module docstring (INCIDENT).
            active_accounts = self._active_broker_accounts(db, "zerodha", strategy_id)

            if not active_accounts:
                return {"status": "failed", "reason": "No connected Zerodha accounts have this strategy enabled"}

            symbol        = validated["symbol"]
            tradingsymbol = inst.get("tradingsymbol", symbol)
            kite_exchange = inst.get("kite_exchange", "NSE")
            lot_size      = get_lot_size(tradingsymbol)
            lots          = max(1, int(validated.get("lot_size", 1)))
            quantity      = lots * lot_size if lot_size > 1 else lots

            any_success = False
            first_trade_id = None
            last_error = "No accounts attempted"

            for acc in active_accounts:
                agent = mgr.get_agent(acc["id"])
                if agent is None:
                    # Most common cause: expired daily access token — see
                    # multi_tenant_broker_manager.py's needs_reauth status.
                    last_error = f"Account {acc.get('login')} unavailable (see logs / needs_reauth)"
                    continue

                result = agent.place_bracket_order(
                    tradingsymbol=tradingsymbol, direction=validated["direction"],
                    quantity=quantity, entry_price=validated.get("entry", current_price),
                    stop_loss=validated["stop_loss"],
                    target=validated.get("take_profit_2") or validated.get("take_profit"),
                    kite_exchange=kite_exchange,
                )

                if result:
                    any_success = True
                    import uuid
                    trade_id = str(uuid.uuid4())[:8].upper()
                    if first_trade_id is None:
                        first_trade_id = trade_id
                    from core.position_tracker import positions
                    positions.open({
                        **validated,
                        "trade_id": trade_id, "lot_size": quantity,
                        "entry_price": current_price, "is_paper": False,
                        "exchange": "zerodha", "account_id": acc["id"],
                        "signal_id": validated.get("signal_id"),
                    })
                else:
                    last_error = f"Account {acc.get('login')} order failed"

            if any_success:
                return {"status": "filled", "trade_id": first_trade_id, "quantity": quantity}
            return {"status": "failed", "reason": last_error}

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
        Route forex/gold/crypto to MetaTrader 5 across all active accounts.
        """
        try:
            agent = self._get_mt5()
            
            from core.database_manager import DatabaseManager
            db = DatabaseManager()
            accounts = db.get_accounts()
            # BUG FIX: previously filtered only by status=="connected", which
            # meant a Zerodha/Binance account sharing this table would have
            # its login/password handed straight to MT5's connect() — silent
            # cross-broker credential mismatch. Now that `broker` is a real
            # column (see migrate_accounts_supervisor.py), filter by it too.
            active_accounts = [a for a in accounts
                                if a["status"] == "connected"
                                and (a.get("broker") or "mt5") == "mt5"]
            
            # If no DB accounts, fallback to single .env account
            if not active_accounts:
                active_accounts = [{"login": None, "password": None, "server": None, "strategies_enabled": '["all"]'}]

            strategy_id = validated.get("strategy_id", "")
            # BUGFIX: this used to read validated["node"], which is the
            # machine hostname (laptop/phone/aws from config.NODES), not a
            # strategy identifier. That meant is_live_ready() never matched
            # anything and silently blocked 100% of live trades for a week.
            # See core/strategy_registry.py's module docstring (INCIDENT).

            # PHASE 5 FIX: same gap as _active_broker_accounts() above — this
            # loop only ever checked the account's own strategies_enabled
            # toggle, never whether the strategy had actually passed
            # walk-forward validation. Refuse the whole batch up front if
            # it hasn't, same as the Zerodha/Binance path.
            from core.strategy_registry import is_live_ready
            if not is_live_ready(strategy_id):
                logger.warning(
                    f"[Router] Strategy '{strategy_id}' is not live_ready — "
                    f"refusing MT5 execution for ANY account regardless of "
                    f"per-account strategies_enabled settings."
                )
                return {"status": "failed", "reason": f"Strategy '{strategy_id}' has not passed walk-forward validation"}

            first_success_trade_id = None
            any_success = False
            last_error = "MT5 not connected"

            for acc in active_accounts:
                # Check if this strategy is enabled for this account
                import json
                try:
                    enabled = json.loads(acc.get("strategies_enabled", '["all"]'))
                except:
                    enabled = ["all"]
                
                # If not "all" and not explicitly enabled, skip this account
                if "all" not in enabled and strategy_id not in enabled:
                    logger.debug(f"[Router] Strategy {strategy_id} not enabled for account {acc.get('login')}, skipping.")
                    continue
                
                # Connect to this specific account
                # NOTE: acc["password"] is Fernet-encrypted at rest (core/secrets_vault.py)
                # since account_endpoints_patch.py started writing accounts via
                # verify_and_connect(). Must decrypt before handing to MT5.
                from core.secrets_vault import decrypt_secret
                real_password = decrypt_secret(acc.get("password") or "")
                if not agent.connect(login=acc.get("login"), password=real_password, server=acc.get("server")):
                    logger.warning(f"[Router] MT5 login failed for account {acc.get('login')}")
                    continue

                symbol    = validated["symbol"]
                direction = validated["direction"]
                entry     = validated.get("entry", current_price)
                sl        = validated["stop_loss"]
                tp        = validated.get("take_profit_2") or validated.get("take_profit")

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
                        "account_login": acc.get("login")
                    })

                    logger.info(
                        f"[Router] MT5 FILLED ✅ | {symbol} {direction.upper()} "
                        f"| Acc: {acc.get('login')} | Lot: {result['volume']} "
                        f"| Price: {result['fill_price']} | Ticket: {result['ticket']}"
                    )
                    any_success = True
                    if not first_success_trade_id:
                        first_success_trade_id = trade_id
                        first_fill_price = result["fill_price"]
                        first_ticket = result["ticket"]
                else:
                    last_error = result.get("reason", "unknown error")
                    logger.warning(f"[Router] MT5 order failed on acc {acc.get('login')}: {last_error}")

            if any_success:
                return {"status": "filled", "trade_id": first_success_trade_id,
                        "fill_price": first_fill_price,
                        "mt5_ticket": first_ticket}
            else:
                return {"status": "failed", "reason": last_error}

        except Exception as e:
            logger.error(f"[Router] MT5 execution error: {e}")
            return {"status": "failed", "reason": str(e)}

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
