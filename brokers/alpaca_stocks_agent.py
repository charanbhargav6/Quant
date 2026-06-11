"""
CRAVE v10.2 — Alpaca US Stocks Agent
=======================================
Handles US equity execution via Alpaca Markets API.
Separate from the existing data_agent.py Alpaca connection —
this file owns all stock-specific logic.

KEY DIFFERENCES FROM FOREX/CRYPTO:
  1. Position sizing is in SHARES not units/lots
     risk_amount / risk_per_share = num_shares (integer)
     Max 20% of equity per single stock position (concentration limit)

  2. Earnings blackout periods
     Block new entries 2 days before earnings, 1 day after.
     Apply 50% hedge to open positions before earnings.

  3. Overnight gap risk
     Stocks gap open. Pre-close check at 19:45 UTC:
       - In drawdown? Close before overnight.
       - In profit? Tighten SL to breakeven, hold.

  4. Market hours
     Only trade 13:30-20:00 UTC (09:30-16:00 ET).
     Open drive (13:30-15:30) and Power Hour (19:00-20:00) only.
     Lunch chop (15:30-19:00) = skip entirely.

  5. PDT Rule (US)
     Pattern Day Trader rule: <$25,000 account = max 3 day trades per week.
     Bot tracks day trade count and blocks entries if limit reached.
     Not enforced on paper accounts.

SETUP:
  Alpaca is already wired in data_agent.py. This file adds stock-specific
  logic (share sizing, earnings, gap risk, PDT tracking) on top.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("crave.alpaca_stocks")


class AlpacaStocksAgent:

    PDT_MAX_DAY_TRADES = 3   # Pattern Day Trader limit for <$25k accounts

    def __init__(self):
        self._alpaca       = None
        self._authenticated = False
        self._day_trades    = 0       # day trades used this week
        self._pdt_reset_day = ""      # ISO date of last PDT reset
        self._earnings_cache: dict = {}
        self._connect()

    def _connect(self):
        """Connect to Alpaca using existing credentials."""
        try:
            api_key = os.environ.get("ALPACA_API_KEY", "")
            secret  = os.environ.get("ALPACA_SECRET_KEY", "")
            is_paper = os.environ.get("TRADING_MODE", "paper") == "paper"

            if not api_key or not secret:
                logger.info("[AlpacaStocks] No API keys — US stocks disabled.")
                return

            # Try new SDK first
            try:
                from alpaca.trading.client import TradingClient
                self._alpaca = TradingClient(api_key, secret, paper=is_paper)
            except ImportError:
                import alpaca_trade_api as tradeapi
                url          = os.environ.get("ALPACA_BASE_URL",
                               "https://paper-api.alpaca.markets")
                self._alpaca = tradeapi.REST(api_key, secret, url)

            self._authenticated = True
            logger.info(
                f"[AlpacaStocks] Connected "
                f"({'paper' if is_paper else 'LIVE'})"
            )

        except Exception as e:
            logger.info(f"[AlpacaStocks] Connection failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # POSITION SIZING — shares not units
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_share_size(self, equity: float, risk_pct: float,
                               entry_price: float,
                               stop_price: float) -> int:
        """
        Calculate number of shares to buy.

        Formula:
          risk_amount    = equity × risk_pct / 100
          risk_per_share = abs(entry_price - stop_price)
          shares         = int(risk_amount / risk_per_share)

        Caps:
          max 20% of equity per single position (concentration limit)
          minimum 1 share

        Example:
          equity=$10,000, risk_pct=1%, entry=$150, stop=$145
          risk_amount = $100
          risk_per_share = $5
          shares = 20
          position_value = 20 × $150 = $3,000 (30% of equity)
          → CAPPED at 20%: max_shares = $2,000 / $150 = 13 shares
        """
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0 or entry_price <= 0:
            return 0

        from config.config import US_STOCKS
        max_pos_pct = US_STOCKS.get("max_position_value_pct", 20.0)

        risk_amount    = equity * (risk_pct / 100)
        shares         = int(risk_amount / risk_per_share)
        max_shares     = int((equity * max_pos_pct / 100) / entry_price)
        final_shares   = max(1, min(shares, max_shares))

        logger.debug(
            f"[AlpacaStocks] Size: equity=${equity:,.0f} risk={risk_pct}% "
            f"entry={entry_price} stop={stop_price} → "
            f"{shares} raw → {final_shares} final (max={max_shares})"
        )
        return final_shares

    # ─────────────────────────────────────────────────────────────────────────
    # EARNINGS BLACKOUT
    # ─────────────────────────────────────────────────────────────────────────

    def get_next_earnings(self, symbol: str) -> Optional[datetime]:
        """
        Get next earnings date for a US stock from yfinance.
        Cached per session to avoid repeated API calls.
        """
        if symbol in self._earnings_cache:
            cached = self._earnings_cache[symbol]
            # Cache expires after 4 hours
            if (datetime.now(timezone.utc) - cached["fetched"]).seconds < 14400:
                return cached["date"]

        try:
            import yfinance as yf
            ticker  = yf.Ticker(symbol)
            cal     = ticker.calendar

            if cal is not None and len(cal) > 0:
                # yfinance returns calendar as DataFrame or dict
                if hasattr(cal, "T"):
                    # DataFrame format
                    earnings_row = cal.T.get("Earnings Date")
                    if earnings_row is not None and len(earnings_row) > 0:
                        date = earnings_row.iloc[0]
                        if hasattr(date, "to_pydatetime"):
                            date = date.to_pydatetime()
                        if hasattr(date, "replace"):
                            date = date.replace(tzinfo=timezone.utc)
                        self._earnings_cache[symbol] = {
                            "date":    date,
                            "fetched": datetime.now(timezone.utc),
                        }
                        return date

        except Exception as e:
            logger.debug(f"[AlpacaStocks] Earnings fetch failed {symbol}: {e}")

        self._earnings_cache[symbol] = {
            "date":    None,
            "fetched": datetime.now(timezone.utc),
        }
        return None

    def is_earnings_blackout(self, symbol: str) -> tuple:
        """
        Check if we're in the earnings blackout window for this symbol.
        Returns (blocked: bool, reason: str).

        Blackout: 2 days before to 1 day after earnings.
        """
        from config.config import US_STOCKS
        days_before = US_STOCKS.get("earnings_blackout_days_before", 2)
        days_after  = US_STOCKS.get("earnings_blackout_days_after", 1)

        earnings_date = self.get_next_earnings(symbol)
        if not earnings_date:
            return False, "No earnings date found"

        now   = datetime.now(timezone.utc)
        delta = (earnings_date - now).days

        if -days_after <= delta <= days_before:
            if delta >= 0:
                return True, f"Earnings in {delta} day(s) — blackout active"
            else:
                return True, f"Post-earnings cooldown ({abs(delta)} day(s) ago)"

        return False, f"Earnings in {delta} days — OK"

    # ─────────────────────────────────────────────────────────────────────────
    # OVERNIGHT GAP RISK CHECK
    # ─────────────────────────────────────────────────────────────────────────

    def should_close_before_overnight(self, entry_price: float,
                                       current_price: float,
                                       stop_loss: float,
                                       direction: str) -> tuple:
        """
        Pre-close check at 19:45 UTC (15 min before US market close).
        Called by event_hedge_manager on open stock positions.

        Rules:
          Position in drawdown (unrealised loss) → CLOSE
          Position at breakeven or better → HOLD (tighten SL to breakeven)

        Returns (should_close: bool, reason: str).
        """
        sl_dist = abs(entry_price - stop_loss)
        if sl_dist == 0:
            return True, "No valid SL — close before overnight"

        if direction in ("buy", "long"):
            unrealised_r = (current_price - entry_price) / sl_dist
        else:
            unrealised_r = (entry_price - current_price) / sl_dist

        if unrealised_r < 0:
            return (
                True,
                f"In drawdown ({unrealised_r:.2f}R) — closing before overnight gap"
            )

        # In profit — suggest tightening SL to breakeven and holding
        return (
            False,
            f"In profit ({unrealised_r:.2f}R) — tighten SL to breakeven and hold"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # PDT RULE TRACKING
    # ─────────────────────────────────────────────────────────────────────────

    def _reset_pdt_if_new_week(self):
        """Reset day trade counter at start of each trading week (Monday)."""
        now  = datetime.now(timezone.utc)
        week = now.strftime("%Y-W%W")
        if self._pdt_reset_day != week:
            self._day_trades    = 0
            self._pdt_reset_day = week

    def can_day_trade(self, equity: float = 0) -> tuple:
        """
        Check PDT compliance. Returns (allowed: bool, reason: str).
        PDT only applies to accounts under $25,000.
        Paper accounts are exempt.
        """
        is_paper = os.environ.get("TRADING_MODE", "paper") == "paper"
        if is_paper:
            return True, "Paper account — PDT not applicable"

        if equity >= 25000:
            return True, "Account >= $25,000 — PDT not applicable"

        self._reset_pdt_if_new_week()
        if self._day_trades >= self.PDT_MAX_DAY_TRADES:
            return (
                False,
                f"PDT limit reached ({self._day_trades}/3 day trades this week)"
            )

        remaining = self.PDT_MAX_DAY_TRADES - self._day_trades
        return True, f"{remaining} day trades remaining this week"

    def record_day_trade(self):
        """Call when a day trade (open+close same day) is executed."""
        self._reset_pdt_if_new_week()
        self._day_trades += 1
        logger.info(
            f"[AlpacaStocks] Day trade recorded: "
            f"{self._day_trades}/{self.PDT_MAX_DAY_TRADES} this week"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ORDER EXECUTION
    # ─────────────────────────────────────────────────────────────────────────

    def place_order(self, symbol: str, direction: str,
                     shares: int, stop_loss: float,
                     take_profit: float) -> Optional[dict]:
        """
        Place a bracket order for a US stock.
        Returns order receipt or None.
        """
        if not self._authenticated or not self._alpaca:
            logger.error("[AlpacaStocks] Not authenticated.")
            return None

        if shares <= 0:
            logger.error(f"[AlpacaStocks] Invalid share count: {shares}")
            return None

        side = "buy" if direction.lower() in ("buy", "long") else "sell"

        try:
            order = self._alpaca.submit_order(
                symbol        = symbol,
                qty           = shares,
                side          = side,
                type          = "market",
                time_in_force = "day",
                order_class   = "bracket",
                stop_loss     = {"stop_price": str(round(stop_loss, 2))},
                take_profit   = {"limit_price": str(round(take_profit, 2))},
            )
            logger.info(
                f"[AlpacaStocks] Order placed: {symbol} {side.upper()} "
                f"{shares} shares | SL={stop_loss} TP={take_profit}"
            )
            return {
                "order_id":  order.id,
                "symbol":    symbol,
                "direction": direction,
                "shares":    shares,
                "sl":        stop_loss,
                "tp":        take_profit,
                "status":    "submitted",
            }

        except Exception as e:
            logger.error(f"[AlpacaStocks] Order failed {symbol}: {e}")
            return None

    def close_position(self, symbol: str) -> bool:
        """Close all shares of a position."""
        if not self._authenticated:
            return False
        try:
            self._alpaca.close_position(symbol)
            logger.info(f"[AlpacaStocks] Closed position: {symbol}")
            return True
        except Exception as e:
            logger.error(f"[AlpacaStocks] Close failed {symbol}: {e}")
            return False

    def close_all_positions(self) -> bool:
        """Emergency: close all open positions."""
        if not self._authenticated:
            return False
        try:
            self._alpaca.close_all_positions()
            logger.warning("[AlpacaStocks] All positions closed.")
            return True
        except Exception as e:
            logger.error(f"[AlpacaStocks] Close all failed: {e}")
            return False

    def get_account_equity(self) -> float:
        """Get current account equity."""
        if not self._authenticated:
            return 0.0
        try:
            account = self._alpaca.get_account()
            return float(account.equity)
        except Exception as e:
            logger.debug(f"[AlpacaStocks] Get equity failed: {e}")
            return 0.0

    def is_market_open(self) -> bool:
        """Check if US market is currently open."""
        try:
            clock = self._alpaca.get_clock()
            return clock.is_open
        except Exception:
            # Fallback: check UTC time
            now_h = datetime.now(timezone.utc).hour
            now_m = datetime.now(timezone.utc).minute
            now_total = now_h * 60 + now_m
            return 13 * 60 + 30 <= now_total <= 20 * 60

    def is_authenticated(self) -> bool:
        return self._authenticated


# ─────────────────────────────────────────────────────────────────────────────
# LAZY SINGLETON
# ─────────────────────────────────────────────────────────────────────────────

_alpaca_stocks_instance: Optional[AlpacaStocksAgent] = None

def get_alpaca_stocks() -> AlpacaStocksAgent:
    global _alpaca_stocks_instance
    if _alpaca_stocks_instance is None:
        _alpaca_stocks_instance = AlpacaStocksAgent()
    return _alpaca_stocks_instance
