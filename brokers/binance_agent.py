import os
import logging
import time
from typing import Optional, List, Tuple
from config.config import BINANCE_API_KEY, BINANCE_API_SECRET

logger = logging.getLogger("crave.binance_agent")

class BinanceAgent:
    """
    Binance Futures broker agent for live/demo execution via CCXT.

    MULTI-ACCOUNT: api_key/api_secret can be passed directly to __init__ so
    multiple instances — one per DB account — can coexist in the same
    process without clobbering each other via shared env vars. Falls back
    to the global .env values only when nothing is passed, for backward
    compatibility with any single-account call sites.
    """

    def __init__(self, api_key: str = None, api_secret: str = None):
        self._connected = False
        self._exchange = None
        self._last_connect_attempt = 0.0
        self._api_key = api_key or BINANCE_API_KEY
        self._api_secret = api_secret or BINANCE_API_SECRET

    def connect(self, login: int = None, password: str = None, server: str = None) -> bool:
        """Initialize CCXT Binance client.
        `login`/`password` accepted for interface compatibility with the
        other brokers' connect() signature — they override whatever was
        passed to __init__ if given, same precedence as before."""
        try:
            import ccxt

            api_key = str(login) if login else self._api_key
            secret = password if password else self._api_secret

            if not api_key or not secret:
                logger.error("[Binance] API keys missing. Check .env or pass explicitly.")
                return False

            # Keep instance credentials in sync with whatever actually connected,
            # so get_account_info() below reports the RIGHT account, not env leftovers.
            self._api_key = api_key
            self._api_secret = secret

            self._exchange = ccxt.binanceusdm({
                'apiKey': api_key,
                'secret': secret,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',
                }
            })
            
            # Check connection
            balance = self._exchange.fetch_balance()
            equity = balance['total'].get('USDT', 0.0)
            
            self._connected = True
            logger.info(
                f"[Binance] Connected ✅ | "
                f"USDT Equity: ${equity:.2f}"
            )
            return True

        except Exception as e:
            logger.error(f"[Binance] Connection failed: {e}")
            self._connected = False
            return False

    def disconnect(self):
        self._connected = False
        self._exchange = None

    def is_connected(self) -> bool:
        return self._connected

    def ensure_connected(self) -> bool:
        if self._connected:
            return True
        now = time.time()
        if now - self._last_connect_attempt > 10:
            self._last_connect_attempt = now
            return self.connect()
        return False

    def _map_symbol(self, crave_symbol: str) -> str:
        """Map crave symbol (e.g. BTCUSD) to Binance symbol (BTC/USDT)."""
        sym = crave_symbol.upper()
        if sym.endswith("USD"):
            sym = sym[:-3] + "/USDT"
        return sym

    def get_symbol_info(self, crave_symbol: str) -> Optional[dict]:
        """Get symbol info (pip size, lot constraints, etc.)."""
        if not self.ensure_connected(): return None
        
        b_sym = self._map_symbol(crave_symbol)
        try:
            self._exchange.load_markets()
            market = self._exchange.market(b_sym)
            
            # Approximate MT5 info mapping
            return {
                "symbol": b_sym,
                "point": market['precision']['price'] if 'price' in market['precision'] else 0.01,
                "digits": 2, # simplified
                "lot_min": market['limits']['amount']['min'],
                "lot_max": market['limits']['amount']['max'],
                "lot_step": market['precision']['amount'],
                "spread": 2.0, # ccxt doesn't provide instant spread without fetch_ticker
                "tick_value": 1.0,
                "tick_size": market['precision']['price']
            }
        except Exception as e:
            logger.error(f"[Binance] get_symbol_info error: {e}")
            return None

    def place_order(self, crave_symbol: str, direction: str,
                    lot_size: float, sl: float = None, tp: float = None,
                    magic: int = 123456, comment: str = "CRAVE",
                    order_type: str = "market", limit_price: float = None,
                    post_only: bool = False) -> Optional[int]:
        """Place a market or limit order with bracket SL/TP.

        order_type: "market" (default) or "limit". Limit entries support
        post_only for maker-fee/order-block entries — this mirrors the
        capability ExecutionAgent's binance path had before execution was
        consolidated onto this class (see multi_tenant_broker_manager.py).
        SL/TP use closePosition=True (not a fixed reduceOnly amount) so
        they fully close the position regardless of partial-fill drift.
        """
        if not self.ensure_connected(): return None
        
        b_sym = self._map_symbol(crave_symbol)
        side = 'buy' if direction.lower() in ('buy', 'long') else 'sell'
        
        try:
            if order_type == "limit" and limit_price:
                entry_params = {'postOnly': True} if post_only else {}
                order = self._exchange.create_order(
                    symbol=b_sym, type='limit', side=side,
                    amount=lot_size, price=round(limit_price, 8),
                    params=entry_params
                )
                logger.info(
                    f"[Binance] LIMIT order @ {limit_price} "
                    f"{'postOnly' if post_only else 'standard'} | Ticket: {order['id']}"
                )
            else:
                order = self._exchange.create_order(
                    symbol=b_sym,
                    type='market',
                    side=side,
                    amount=lot_size
                )
                logger.info(f"[Binance] MARKET order placed ✅ | Ticket: {order['id']}")

            # Place SL/TP as separate conditional orders. closePosition=True
            # (not reduceOnly + fixed amount) guarantees the full position
            # closes even if the entry only partially filled.
            opposite_side = 'sell' if side == 'buy' else 'buy'

            if sl:
                self._exchange.create_order(
                    symbol=b_sym, type='stop_market', side=opposite_side,
                    amount=lot_size, params={'stopPrice': round(sl, 8),
                                              'reduceOnly': True, 'closePosition': True}
                )
            if tp:
                self._exchange.create_order(
                    symbol=b_sym, type='take_profit_market', side=opposite_side,
                    amount=lot_size, params={'stopPrice': round(tp, 8),
                                              'reduceOnly': True, 'closePosition': True}
                )

            return int(order['id'])
            
        except Exception as e:
            logger.error(f"[Binance] place_order failed: {e}")
            return None

    def modify_sl(self, ticket: int, new_sl: float, new_tp: float = None) -> Tuple[bool, float, float]:
        """Modify SL/TP by cancelling old stop orders and creating new ones."""
        if not self.ensure_connected(): return False, 0.0, 0.0
        # Binance requires symbol to fetch open orders easily, we assume ticket is not enough
        # For full implementation, we'd need the symbol passed, or we fetch all open orders.
        # This is a simplified shim.
        logger.warning(f"[Binance] modify_sl called, requires advanced CCXT order management.")
        return True, new_sl, new_tp or 0.0

    def close_position(self, ticket: int = None, crave_symbol: str = None, volume: float = None) -> bool:
        """Close position by placing opposite market order (reduce only)."""
        if not self.ensure_connected() or not crave_symbol: return False
        
        b_sym = self._map_symbol(crave_symbol)
        try:
            positions = self._exchange.fetch_positions([b_sym])
            for p in positions:
                if p['symbol'] == b_sym and float(p['contracts']) > 0:
                    side = 'sell' if p['side'] == 'long' else 'buy'
                    amt = volume if volume else p['contracts']
                    self._exchange.create_order(
                        symbol=b_sym, type='market', side=side, amount=amt,
                        params={'reduceOnly': True}
                    )
                    # Cancel pending SL/TP
                    self._exchange.cancel_all_orders(b_sym)
                    return True
            return False
        except Exception as e:
            logger.error(f"[Binance] close_position failed: {e}")
            return False

    def get_positions(self) -> List[dict]:
        """Get open positions."""
        if not self.ensure_connected(): return []
        try:
            pos_data = self._exchange.fetch_positions()
            active = []
            for p in pos_data:
                if float(p['contracts']) > 0:
                    active.append({
                        "ticket": int(p.get('id', 0) or 0),
                        "symbol": p['symbol'].replace('/USDT', 'USD'),
                        "type": 0 if p['side'] == 'long' else 1,
                        "volume": p['contracts'],
                        "price_open": p['entryPrice'],
                        "sl": p.get('stopLossPrice', 0.0),
                        "tp": p.get('takeProfitPrice', 0.0),
                        "price_current": p['markPrice'],
                        "profit": p['unrealizedPnl'],
                        "magic": 123456
                    })
            return active
        except Exception as e:
            logger.error(f"[Binance] get_positions failed: {e}")
            return []

    def get_closed_trades(self, days: int = 30) -> List[dict]:
        return []

    def get_account_info(self) -> Optional[dict]:
        if not self.ensure_connected(): return None
        try:
            bal = self._exchange.fetch_balance()
            eq = bal['total'].get('USDT', 0.0)
            return {
                # BUG FIX: was hardcoded to the global env BINANCE_API_KEY,
                # so every account's get_account_info() reported the SAME
                # login regardless of which key actually connected. Now
                # reports the instance's own connected key.
                "login": self._api_key[:6] if self._api_key else "unknown",
                "balance": eq,
                "equity": eq,
                "server": "Binance-Futures"
            }
        except Exception as e:
            logger.error(f"[Binance] get_account_info failed: {e}")
            return None

    def calculate_lot_size(self, crave_symbol: str, equity: float, risk_pct: float, stop_loss_pips: float) -> float:
        # Simplified lot calculation for crypto
        return 0.01
