"""
CRAVE v10.2 — Zerodha Kite Connect Agent
==========================================
Handles all Indian market execution via Zerodha Kite Connect API.

SUPPORTED INSTRUMENTS:
  NSE equities:   RELIANCE, TCS, INFY, HDFCBANK, etc.
  BSE equities:   BSE-listed stocks (set kite_exchange="BSE")
  NSE F&O:        NIFTY, BANKNIFTY futures and options
  MCX:            Commodity futures (Gold, Silver MCX)

CRITICAL RULES:
  1. Access tokens expire at 06:00 IST (00:30 UTC) every day.
     Bot MUST call daily_login() before 04:00 UTC (market open).
     Scheduled in run_bot.py at 03:30 UTC.

  2. NSE circuit breakers: stocks halt at ±5%, ±10%, ±20% from prev close.
     Before entering any NSE position, check circuit breaker status.
     If locked limit-up or limit-down → do NOT enter.

  3. F&O lot sizes are fixed: NIFTY=50, BANKNIFTY=15, stocks vary.
     Never allow fractional lots. Always validate before order.

  4. Indian equities settle T+1.
     Do not hold delivery positions overnight without understanding this.
     For intraday (MIS), positions are auto-squared at 15:15 IST.

SETUP:
  1. Register at kite.zerodha.com/apps → create a new app
  2. Get API key and API secret
  3. Add to .env:
       ZERODHA_API_KEY=your_api_key
       ZERODHA_API_SECRET=your_api_secret
       ZERODHA_REDIRECT_URL=http://127.0.0.1/redirect
  4. Run daily_login() each morning. It will:
     a) Generate login URL
     b) Send URL to Telegram
     c) You click the link, approve, and paste the request_token back
     d) Bot exchanges it for access_token and stores it

USAGE:
  from brokers.zerodha_agent import get_zerodha

  agent = get_zerodha()
  if agent.is_authenticated():
      order_id = agent.place_order("RELIANCE", "buy", 1)
      positions = agent.get_positions()
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crave.zerodha")

# Token stored in State/ directory, refreshed daily
TOKEN_FILE = Path(__file__).parent.parent / "State" / "zerodha_token.json"


class ZerodhaAgent:

    @staticmethod
    def _decrypt_env_value(raw: str) -> str:
        """See MT5Agent._decrypt_env_password — same encrypted-at-rest
        convention, same plaintext fallback for pre-existing .env files."""
        if not raw:
            return ""
        try:
            from core.secrets_vault import decrypt_secret
            return decrypt_secret(raw)
        except Exception:
            logger.warning(
                "[Zerodha] ZERODHA_API_SECRET did not decrypt as a Fernet "
                "token — using as plaintext. Re-add this account via the "
                "UI to migrate it to encrypted storage."
            )
            return raw

    def __init__(self):
        self._api_key    = os.environ.get("ZERODHA_API_KEY", "")
        self._api_secret = self._decrypt_env_value(os.environ.get("ZERODHA_API_SECRET", ""))
        self._redirect   = os.environ.get("ZERODHA_REDIRECT_URL",
                                           "http://127.0.0.1/redirect")
        self._kite       = None
        self._authenticated = False
        self._token_data: dict = {}

        if not self._api_key:
            logger.info(
                "[Zerodha] ZERODHA_API_KEY not set. "
                "Indian market trading disabled."
            )
            return

        self._load_token()

    # ─────────────────────────────────────────────────────────────────────────
    # AUTHENTICATION
    # ─────────────────────────────────────────────────────────────────────────

    def _load_token(self):
        """Load stored access token if it's still valid (same calendar day IST)."""
        if not TOKEN_FILE.exists():
            return

        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)

            # Check if token was created today (IST day)
            from zoneinfo import ZoneInfo
            ist_now  = datetime.now(ZoneInfo("Asia/Kolkata"))
            ist_date = ist_now.strftime("%Y-%m-%d")
            token_date = data.get("date", "")

            if token_date != ist_date:
                logger.info("[Zerodha] Token expired (wrong date). Re-login required.")
                return

            access_token = data.get("access_token")
            if not access_token:
                return

            # Connect with stored token
            from kiteconnect import KiteConnect
            self._kite = KiteConnect(api_key=self._api_key)
            self._kite.set_access_token(access_token)
            self._authenticated = True
            self._token_data    = data
            logger.info("[Zerodha] ✅ Authenticated with stored token.")

        except ImportError:
            logger.warning(
                "[Zerodha] kiteconnect not installed. "
                "Run: pip install kiteconnect"
            )
        except Exception as e:
            logger.warning(f"[Zerodha] Token load failed: {e}")

    def _save_token(self, access_token: str):
        """Persist access token with today's IST date."""
        from zoneinfo import ZoneInfo
        ist_date = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        data     = {
            "access_token": access_token,
            "date":         ist_date,
            "saved_at":     datetime.now(timezone.utc).isoformat(),
        }
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"[Zerodha] Token saved for {ist_date}.")

    def get_login_url(self) -> str:
        """
        Generate the Zerodha login URL.
        Send this to yourself via Telegram each morning.
        After logging in, Zerodha redirects to your redirect URL
        with ?request_token=xxx in the URL.
        """
        try:
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=self._api_key)
            url  = kite.login_url()
            logger.info(f"[Zerodha] Login URL: {url}")
            return url
        except ImportError:
            return "kiteconnect not installed"
        except Exception as e:
            return f"Error: {e}"

    def complete_login(self, request_token: str) -> bool:
        """
        Complete the OAuth flow with the request_token from the redirect URL.
        Call this after user clicks the login URL and provides the token.

        The request_token appears in the URL after login:
          http://127.0.0.1/redirect?request_token=XXXXXX&action=login&status=success
        """
        try:
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=self._api_key)
            data = kite.generate_session(request_token, api_secret=self._api_secret)
            access_token = data["access_token"]
            kite.set_access_token(access_token)
            self._kite          = kite
            self._authenticated = True
            self._save_token(access_token)
            logger.info("[Zerodha] ✅ Login complete. Access token saved.")

            # Notify
            try:
                from interfaces.telegram_interface import tg
                tg.send(
                    "✅ <b>Zerodha Login Complete</b>\n"
                    "Indian market trading is now active.\n"
                    f"Token valid until midnight IST tonight."
                )
            except Exception:
                pass

            return True

        except Exception as e:
            logger.error(f"[Zerodha] Login failed: {e}")
            return False

    def daily_login(self) -> str:
        """
        Trigger the daily login flow.
        Called by scheduler at 03:30 UTC (09:00 IST) every trading day.
        Returns the login URL to send to user via Telegram.

        Full flow:
          1. Bot generates login URL and sends to Telegram
          2. User clicks URL, approves on Zerodha
          3. User copies request_token from redirect URL
          4. User sends: /zerodha_token <request_token> to bot
          5. Bot calls complete_login(request_token)
        """
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Kolkata"))

        # Skip on weekends — NSE closed Saturday + Sunday
        if now.weekday() >= 5:
            logger.info("[Zerodha] Weekend — skipping daily login.")
            return ""

        url = self.get_login_url()

        try:
            from interfaces.telegram_interface import tg
            tg.send(
                f"🔐 <b>Zerodha Daily Login Required</b>\n"
                f"Click to login:\n{url}\n\n"
                f"After login, paste the request_token:\n"
                f"/zerodha_token YOUR_TOKEN_HERE"
            )
        except Exception:
            pass

        logger.info("[Zerodha] Daily login URL sent to Telegram.")
        return url

    def is_authenticated(self) -> bool:
        return self._authenticated and self._kite is not None

    # ─────────────────────────────────────────────────────────────────────────
    # CIRCUIT BREAKER CHECK
    # ─────────────────────────────────────────────────────────────────────────

    def is_circuit_breaker_active(self, tradingsymbol: str,
                                   kite_exchange: str = "NSE") -> bool:
        """
        Check if a stock is currently locked in a circuit breaker.
        NSE circuit breakers: ±5%, ±10%, ±20% from previous close.
        If locked limit-up or limit-down → DO NOT enter.

        Returns True if trading should be blocked.
        """
        if not self.is_authenticated():
            return False

        try:
            quote = self._kite.quote(f"{kite_exchange}:{tradingsymbol}")
            data  = quote.get(f"{kite_exchange}:{tradingsymbol}", {})

            ohlc       = data.get("ohlc", {})
            prev_close = ohlc.get("close", 0)
            last_price = data.get("last_price", 0)
            upper_cb   = data.get("upper_circuit_limit", 0)
            lower_cb   = data.get("lower_circuit_limit", 0)

            if upper_cb and lower_cb and last_price:
                at_upper = last_price >= upper_cb * 0.999
                at_lower = last_price <= lower_cb * 1.001
                if at_upper or at_lower:
                    limit = "upper" if at_upper else "lower"
                    logger.warning(
                        f"[Zerodha] {tradingsymbol} at {limit} circuit limit "
                        f"(price={last_price}, upper={upper_cb}, lower={lower_cb})"
                    )
                    return True

            return False

        except Exception as e:
            logger.debug(f"[Zerodha] Circuit check failed for {tradingsymbol}: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # ORDER EXECUTION
    # ─────────────────────────────────────────────────────────────────────────

    def place_order(self, tradingsymbol: str, direction: str,
                     quantity: int, kite_exchange: str = "NSE",
                     order_type: str = "MARKET",
                     product: str = "MIS") -> Optional[str]:
        """
        Place an order on NSE/BSE/NFO.

        Parameters:
          tradingsymbol: NSE symbol (e.g., "RELIANCE", "NIFTY23OCTFUT")
          direction:     "buy" or "sell"
          quantity:      number of shares or lots
          kite_exchange: "NSE" / "BSE" / "NFO" / "MCX"
          order_type:    "MARKET" / "LIMIT" / "SL" / "SL-M"
          product:       "MIS" (intraday) / "CNC" (delivery) / "NRML" (F&O)

        Returns order_id string or None on failure.

        SAFETY CHECKS:
          - Verifies authentication
          - Checks circuit breaker before entry
          - Validates lot size for F&O instruments
          - Logs every order attempt regardless of outcome
        """
        if not self.is_authenticated():
            logger.error("[Zerodha] Not authenticated. Cannot place order.")
            return None

        # Circuit breaker check
        if self.is_circuit_breaker_active(tradingsymbol, kite_exchange):
            logger.warning(
                f"[Zerodha] Order blocked: {tradingsymbol} at circuit limit."
            )
            return None

        # Validate F&O lot size
        from config.config import get_lot_size
        lot_size = get_lot_size(tradingsymbol)
        if lot_size > 1 and quantity % lot_size != 0:
            logger.error(
                f"[Zerodha] Invalid quantity {quantity} for {tradingsymbol} "
                f"(lot size = {lot_size}). Must be a multiple of {lot_size}."
            )
            return None

        transaction = "BUY" if direction.lower() in ("buy", "long") else "SELL"

        try:
            order_id = self._kite.place_order(
                variety          = self._kite.VARIETY_REGULAR,
                exchange         = kite_exchange,
                tradingsymbol    = tradingsymbol,
                transaction_type = transaction,
                quantity         = quantity,
                product          = product,
                order_type       = order_type,
            )
            logger.info(
                f"[Zerodha] Order placed: {tradingsymbol} {transaction} "
                f"qty={quantity} product={product} → order_id={order_id}"
            )
            return str(order_id)

        except Exception as e:
            logger.error(f"[Zerodha] Order failed {tradingsymbol}: {e}")
            return None

    def place_bracket_order(self, tradingsymbol: str, direction: str,
                             quantity: int, entry_price: float,
                             stop_loss: float, target: float,
                             kite_exchange: str = "NSE") -> Optional[str]:
        """
        Place a bracket order (entry + SL + target in one).
        Only available for MIS (intraday) positions on Kite.
        """
        if not self.is_authenticated():
            return None

        transaction = "BUY" if direction.lower() in ("buy","long") else "SELL"

        try:
            sl_pts     = abs(entry_price - stop_loss)
            target_pts = abs(target - entry_price)

            order_id = self._kite.place_order(
                variety          = self._kite.VARIETY_BO,
                exchange         = kite_exchange,
                tradingsymbol    = tradingsymbol,
                transaction_type = transaction,
                quantity         = quantity,
                product          = self._kite.PRODUCT_MIS,
                order_type       = self._kite.ORDER_TYPE_LIMIT,
                price            = entry_price,
                stoploss         = round(sl_pts, 2),
                squareoff        = round(target_pts, 2),
            )
            logger.info(
                f"[Zerodha] Bracket order: {tradingsymbol} {transaction} "
                f"entry={entry_price} sl={stop_loss} target={target}"
            )
            return str(order_id)

        except Exception as e:
            logger.error(f"[Zerodha] Bracket order failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        if not self.is_authenticated():
            return False
        try:
            self._kite.cancel_order(
                variety  = self._kite.VARIETY_REGULAR,
                order_id = order_id,
            )
            logger.info(f"[Zerodha] Order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"[Zerodha] Cancel failed {order_id}: {e}")
            return False

    def close_position(self, tradingsymbol: str,
                        kite_exchange: str = "NSE") -> bool:
        """Close all open positions for a symbol."""
        if not self.is_authenticated():
            return False

        try:
            positions = self._kite.positions()
            net_pos   = positions.get("net", [])

            for pos in net_pos:
                if (pos["tradingsymbol"] == tradingsymbol and
                        pos["exchange"] == kite_exchange and
                        pos["quantity"] != 0):

                    close_direction = "SELL" if pos["quantity"] > 0 else "BUY"
                    qty             = abs(pos["quantity"])

                    self._kite.place_order(
                        variety          = self._kite.VARIETY_REGULAR,
                        exchange         = kite_exchange,
                        tradingsymbol    = tradingsymbol,
                        transaction_type = close_direction,
                        quantity         = qty,
                        product          = pos["product"],
                        order_type       = self._kite.ORDER_TYPE_MARKET,
                    )
                    logger.info(
                        f"[Zerodha] Closed position: {tradingsymbol} "
                        f"qty={qty} direction={close_direction}"
                    )
            return True

        except Exception as e:
            logger.error(f"[Zerodha] Close position failed {tradingsymbol}: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # DATA
    # ─────────────────────────────────────────────────────────────────────────

    def get_ohlcv(self, tradingsymbol: str, kite_exchange: str = "NSE",
                   interval: str = "15minute",
                   days: int = 10) -> Optional[object]:
        """
        Fetch OHLCV from Kite Connect.
        Returns DataFrame or None.

        intervals: minute, 3minute, 5minute, 10minute, 15minute,
                   30minute, 60minute, day
        """
        if not self.is_authenticated():
            return None

        try:
            import pandas as pd
            instrument_token = self._get_instrument_token(
                tradingsymbol, kite_exchange
            )
            if not instrument_token:
                return None

            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            to_date   = datetime.now().strftime("%Y-%m-%d")

            data   = self._kite.historical_data(
                instrument_token, from_date, to_date, interval
            )
            if not data:
                return None

            df = pd.DataFrame(data)
            df.rename(columns={"date": "time"}, inplace=True)
            df["time"] = pd.to_datetime(df["time"], utc=True)
            return df[["time", "open", "high", "low", "close", "volume"]]

        except Exception as e:
            logger.error(f"[Zerodha] OHLCV fetch failed {tradingsymbol}: {e}")
            return None

    def _get_instrument_token(self, tradingsymbol: str,
                               exchange: str = "NSE") -> Optional[int]:
        """Look up instrument token for a symbol."""
        try:
            instruments = self._kite.instruments(exchange)
            for inst in instruments:
                if inst["tradingsymbol"] == tradingsymbol:
                    return inst["instrument_token"]
        except Exception as e:
            logger.debug(f"[Zerodha] Token lookup failed {tradingsymbol}: {e}")
        return None

    def get_quote(self, tradingsymbol: str,
                   kite_exchange: str = "NSE") -> Optional[dict]:
        """Get live quote for a symbol."""
        if not self.is_authenticated():
            return None
        try:
            quote = self._kite.quote(f"{kite_exchange}:{tradingsymbol}")
            return quote.get(f"{kite_exchange}:{tradingsymbol}")
        except Exception as e:
            logger.debug(f"[Zerodha] Quote failed {tradingsymbol}: {e}")
            return None

    def get_positions(self) -> list:
        """Get all current positions."""
        if not self.is_authenticated():
            return []
        try:
            return self._kite.positions().get("net", [])
        except Exception as e:
            logger.error(f"[Zerodha] Get positions failed: {e}")
            return []

    def get_account_funds(self) -> dict:
        """Get available margin/funds."""
        if not self.is_authenticated():
            return {}
        try:
            return self._kite.margins()
        except Exception as e:
            logger.error(f"[Zerodha] Get funds failed: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────────
    # FII/DII DATA
    # ─────────────────────────────────────────────────────────────────────────

    def get_fii_dii_data(self) -> dict:
        """
        Fetch FII (Foreign Institutional Investor) and DII (Domestic Institutional
        Investor) flow data from NSE website.
        Published daily after market close. Strong directional indicator for Nifty.

        FII net buying → bullish bias for next day
        FII net selling + DII buying → potential support at key levels
        Both selling → bearish bias, reduce size or skip India trades
        """
        try:
            import requests
            url     = "https://www.nseindia.com/api/fiidiiTradeReact"
            headers = {
                "User-Agent":   "Mozilla/5.0",
                "Accept":       "application/json",
                "Referer":      "https://www.nseindia.com",
            }
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    latest = data[0]
                    fii_net = float(latest.get("fiiNetDii", 0))
                    dii_net = float(latest.get("diiNetDii", 0))

                    bias = "BULLISH" if fii_net > 0 else "BEARISH" if fii_net < -500 else "NEUTRAL"
                    return {
                        "date":     latest.get("date", ""),
                        "fii_net":  round(fii_net, 2),
                        "dii_net":  round(dii_net, 2),
                        "bias":     bias,
                        "combined": round(fii_net + dii_net, 2),
                    }
        except Exception as e:
            logger.debug(f"[Zerodha] FII/DII fetch failed: {e}")

        return {"available": False}

    def get_pcr(self, symbol: str = "NIFTY") -> dict:
        """
        Put-Call Ratio from NSE option chain.
        PCR > 1.2 = bearish (more puts than calls = hedging)
        PCR < 0.8 = bullish (more calls = directional bets)
        Updated every 3 minutes during market hours.
        """
        try:
            import requests
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
            headers = {
                "User-Agent":   "Mozilla/5.0",
                "Accept":       "application/json",
                "Referer":      "https://www.nseindia.com",
            }
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data   = resp.json()
                chains = data.get("records", {}).get("data", [])

                total_put_oi  = sum(
                    c.get("PE", {}).get("openInterest", 0) for c in chains
                )
                total_call_oi = sum(
                    c.get("CE", {}).get("openInterest", 0) for c in chains
                )

                if total_call_oi > 0:
                    pcr = round(total_put_oi / total_call_oi, 3)
                    signal = (
                        "BEARISH" if pcr > 1.2 else
                        "BULLISH" if pcr < 0.8 else
                        "NEUTRAL"
                    )
                    return {
                        "pcr":        pcr,
                        "signal":     signal,
                        "put_oi":     total_put_oi,
                        "call_oi":    total_call_oi,
                        "available":  True,
                    }

        except Exception as e:
            logger.debug(f"[Zerodha] PCR fetch failed: {e}")

        return {"available": False}

    # ─────────────────────────────────────────────────────────────────────────
    # STATUS
    # ─────────────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        from zoneinfo import ZoneInfo
        ist_now = datetime.now(ZoneInfo("Asia/Kolkata"))
        return {
            "authenticated":   self._authenticated,
            "api_key_set":     bool(self._api_key),
            "token_date":      self._token_data.get("date", "none"),
            "ist_time":        ist_now.strftime("%H:%M IST"),
            "market_open":     "04:00" <= datetime.now(timezone.utc).strftime("%H:%M") < "10:00",
        }

    def get_status_message(self) -> str:
        s = self.get_status()
        auth   = "✅ Authenticated" if s["authenticated"] else "❌ Not logged in"
        market = "🟢 Open" if s["market_open"] else "⏹️ Closed"
        return (
            f"🇮🇳 <b>ZERODHA STATUS</b>\n"
            f"Auth     : {auth}\n"
            f"Token    : {s['token_date']}\n"
            f"Market   : {market}\n"
            f"IST Time : {s['ist_time']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# LAZY SINGLETON
# ─────────────────────────────────────────────────────────────────────────────

_zerodha_instance: Optional[ZerodhaAgent] = None

def get_zerodha() -> ZerodhaAgent:
    global _zerodha_instance
    if _zerodha_instance is None:
        _zerodha_instance = ZerodhaAgent()
    return _zerodha_instance
