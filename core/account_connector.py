"""
CRAVE — Account Connector (fixes the "Connect & Verify" gap)
================================================================
PROBLEM THIS FIXES:
  quant_server.py's old /api/accounts/add handler saved credentials to the
  DB and wrote a .env.<profile> file, but NEVER called any broker's
  connect() method. It returned {"status":"success","balance": <the number
  the user typed into the form>} unconditionally. The UI then displayed
  "Account verified and added!" even for a wrong password or a dead server.

FIX:
  One function, verify_and_connect(), that:
    1. Dispatches to the correct broker adapter based on account_type/broker.
    2. Actually calls that broker's real connect()/auth method.
    3. Pulls REAL balance/equity from the broker — never from user input.
    4. Returns a structured result the API layer can trust completely:
       {"ok": True/False, "balance": <real>, "equity": <real>,
        "currency": ..., "server": ..., "reason": <if failed>}
    5. Never returns ok=True unless the broker actually confirmed identity.

FIELD SHAPE DIFFERS PER BROKER — do not force one generic form:
  MT5      : login (int), password, server            → direct login
  Binance  : api_key, api_secret                       → no "server", no OAuth
  Zerodha  : api_key, api_secret, then OAuth redirect   → NOT a password login.
             verify_and_connect() for zerodha returns a login_url instead of
             an immediate ok/fail — the caller must complete OAuth separately
             via ZerodhaAgent.complete_login(request_token).
"""

import logging
from typing import Optional

logger = logging.getLogger("crave.account_connector")


class AccountVerificationError(Exception):
    pass


def verify_and_connect(account_type: str, broker: str, credentials: dict) -> dict:
    """
    account_type: "prop_firm" | "real" | "demo" | "binance"
    broker:       "mt5" | "zerodha" | "binance"
    credentials:  broker-specific dict, see per-broker section below.

    Returns a dict that is ALWAYS safe to show the user directly:
      {"ok": bool, "balance": float|None, "equity": float|None,
       "currency": str|None, "server": str|None, "reason": str|None,
       "requires_oauth": bool, "login_url": str|None}
    """
    broker = (broker or "").lower()

    if broker == "mt5":
        return _verify_mt5(credentials)
    elif broker == "binance":
        return _verify_binance(credentials)
    elif broker == "zerodha":
        return _verify_zerodha(credentials)
    else:
        return {
            "ok": False, "balance": None, "equity": None, "currency": None,
            "server": None, "reason": f"Unknown broker '{broker}'",
            "requires_oauth": False, "login_url": None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# MT5 — direct login, synchronous verify
# ─────────────────────────────────────────────────────────────────────────────

def _verify_mt5(credentials: dict) -> dict:
    """credentials: {login, password, server}"""
    login    = credentials.get("login")
    password = credentials.get("password")
    server   = credentials.get("server")

    if not (login and password and server):
        return _fail("Login, password, and server are all required for MT5.")

    try:
        # A fresh agent instance, NOT the shared singleton — verifying a new
        # account must never disturb whatever account is currently live-trading.
        from brokers.mt5_agent import MT5Agent
        agent = MT5Agent()
        connected = agent.connect(login=login, password=password, server=server)

        if not connected:
            return _fail(
                "MT5 rejected this login/password/server combination. "
                "Double-check the server name exactly matches what your "
                "broker shows in the MT5 terminal (case-sensitive)."
            )

        info = agent.get_account_info()
        agent.disconnect()

        if not info:
            return _fail("Connected but could not read account info from MT5.")

        # Identity verification: a terminal connection alone is not enough;
        # confirm that the broker reports the exact requested account and
        # server. This prevents saving a different already-logged-in account.
        try:
            requested_login = int(login)
            reported_login = int(info.get("login"))
        except (TypeError, ValueError):
            return _fail("MT5 returned an invalid account login during verification.")
        if reported_login != requested_login:
            return _fail(
                f"MT5 connected, but returned account {reported_login} instead of "
                f"requested account {requested_login}."
            )
        requested_server = str(server).strip().lower()
        reported_server = str(info.get("server") or "").strip().lower()
        if requested_server and reported_server != requested_server:
            return _fail(
                f"MT5 connected to server '{info.get('server')}', not requested "
                f"server '{server}'."
            )

        return {
            "ok": True,
            "balance": info["balance"],
            "equity": info["equity"],
            "currency": info["currency"],
            "server": info["server"],
            "reason": None,
            "requires_oauth": False,
            "login_url": None,
        }

    except ImportError:
        return _fail(
            "MetaTrader5 package not installed on this machine (Windows-only). "
            "This account can be saved but cannot be verified from this server."
        )
    except Exception as e:
        logger.exception("[AccountConnector] MT5 verify error")
        return _fail(f"Unexpected MT5 error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# BINANCE — API key/secret, synchronous verify. NOT a username/password/server
# triple — the UI must show "API Key" + "API Secret" fields, no server field.
# ─────────────────────────────────────────────────────────────────────────────

def _verify_binance(credentials: dict) -> dict:
    """credentials: {api_key, api_secret}"""
    api_key    = credentials.get("api_key")
    api_secret = credentials.get("api_secret")

    if not (api_key and api_secret):
        return _fail("API Key and API Secret are both required for Binance.")

    try:
        from brokers.binance_agent import BinanceAgent
        agent = BinanceAgent()
        # BinanceAgent.connect() historically reused (login, password) as
        # (api_key, api_secret) — kept for compatibility, but named honestly here.
        connected = agent.connect(login=api_key, password=api_secret)

        if not connected:
            return _fail(
                "Binance rejected this API key/secret. Check that the key "
                "has Futures trading permission enabled and isn't IP-restricted "
                "to a different address than this server."
            )

        info = agent.get_account_info()
        if not info:
            return _fail("Connected but could not read balance from Binance.")

        return {
            "ok": True,
            "balance": info["balance"],
            "equity": info["equity"],
            "currency": "USDT",
            "server": info.get("server", "Binance-Futures"),
            "reason": None,
            "requires_oauth": False,
            "login_url": None,
        }

    except ImportError:
        return _fail("ccxt package not installed on this server.")
    except Exception as e:
        logger.exception("[AccountConnector] Binance verify error")
        return _fail(f"Unexpected Binance error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# ZERODHA — OAuth only. There is no password to check server-side; Kite
# Connect requires the user to log in via a browser redirect and hand back a
# request_token. verify_and_connect() therefore does NOT return ok=True/False
# immediately — it returns a login_url and requires_oauth=True. The frontend
# must open that URL, capture request_token from the redirect, then call
# complete_zerodha_login() below to finish verification.
# ─────────────────────────────────────────────────────────────────────────────

def _verify_zerodha(credentials: dict) -> dict:
    """credentials: {api_key, api_secret}"""
    api_key    = credentials.get("api_key")
    api_secret = credentials.get("api_secret")

    if not (api_key and api_secret):
        return _fail("API Key and API Secret are both required for Zerodha.")

    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        login_url = kite.login_url()
        return {
            "ok": False,               # not verified yet — OAuth still pending
            "balance": None, "equity": None, "currency": None, "server": None,
            "reason": "Zerodha requires browser login to verify. Open login_url, "
                      "approve, then submit the request_token to complete setup.",
            "requires_oauth": True,
            "login_url": login_url,
        }
    except ImportError:
        return _fail("kiteconnect package not installed on this server.")
    except Exception as e:
        logger.exception("[AccountConnector] Zerodha login_url error")
        return _fail(f"Unexpected Zerodha error: {e}")


def complete_zerodha_login(api_key: str, api_secret: str, request_token: str) -> dict:
    """
    Call this once the user has completed the Zerodha OAuth redirect and you
    have their request_token. THIS is the actual verification step for
    Zerodha — it exchanges the token for a session and confirms via margins().
    """
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data["access_token"]
        kite.set_access_token(access_token)

        margins = kite.margins()
        equity_segment = margins.get("equity", {})
        balance = equity_segment.get("net", 0.0)

        return {
            "ok": True,
            "balance": balance,
            "equity": balance,
            "currency": "INR",
            "server": "Zerodha/Kite",
            "reason": None,
            "requires_oauth": False,
            "login_url": None,
            "access_token": access_token,  # caller must store this, not the password
        }
    except Exception as e:
        logger.exception("[AccountConnector] Zerodha complete_login error")
        return _fail(f"Zerodha OAuth completion failed: {e}")


def _fail(reason: str) -> dict:
    return {
        "ok": False, "balance": None, "equity": None, "currency": None,
        "server": None, "reason": reason, "requires_oauth": False,
        "login_url": None,
    }
