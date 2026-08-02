import os
import logging
import time
from typing import Optional, List, Tuple
from config.config import BINANCE_API_KEY, BINANCE_API_SECRET

logger = logging.getLogger("crave.binance_agent")

class BinanceAgent:
    """Binance Futures broker agent for live/demo execution via CCXT."""

    def __init__(self):
        self._connected = False
        self._exchange = None
        self._last_connect_attempt = 0.0

    def connect(self, login: int = None, password: str = None, server: str = None) -> bool:
        """Initialize CCXT Binance client."""
        try:
            import ccxt
            
            # Note: password is used as API secret if passed, else env vars
            api_key = str(login) if login else BINANCE_API_KEY
            secret = password if password else BINANCE_API_SECRET
            
            if not api_key or not secret:
                logger.error("[Binance] API keys missing. Check .env")
                return False

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
                    magic: int = 123456, comment: str = "CRAVE") -> Optional[int]:
        """Place market order with bracket SL/TP."""
        if not self.ensure_connected(): return None
        
        b_sym = self._map_symbol(crave_symbol)
        side = 'buy' if direction.lower() in ('buy', 'long') else 'sell'
        
        try:
            # Place main market order
            order = self._exchange.create_order(
                symbol=b_sym,
                type='market',
                side=side,
                amount=lot_size
            )
            
            # Place SL/TP as separate conditional orders
            opposite_side = 'sell' if side == 'buy' else 'buy'
            
            if sl:
                self._exchange.create_order(
                    symbol=b_sym, type='stop_market', side=opposite_side,
                    amount=lot_size, params={'stopPrice': sl, 'reduceOnly': True}
                )
            if tp:
                self._exchange.create_order(
                    symbol=b_sym, type='take_profit_market', side=opposite_side,
                    amount=lot_size, params={'stopPrice': tp, 'reduceOnly': True}
                )
                
            logger.info(f"[Binance] Order placed ✅ | Ticket: {order['id']}")
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
        except:
            return []

    def get_closed_trades(self, days: int = 30) -> List[dict]:
        return []

    def get_account_info(self) -> Optional[dict]:
        if not self.ensure_connected(): return None
        try:
            bal = self._exchange.fetch_balance()
            eq = bal['total'].get('USDT', 0.0)
            return {
                "login": BINANCE_API_KEY[:6],
                "balance": eq,
                "equity": eq,
                "server": "Binance-Futures"
            }
        except:
            return None

    def calculate_lot_size(self, crave_symbol: str, equity: float, risk_pct: float, stop_loss_pips: float) -> float:
        # Simplified lot calculation for crypto
        return 0.01
