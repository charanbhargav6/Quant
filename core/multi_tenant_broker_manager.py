"""
CRAVE — Multi-Tenant Broker Manager (Phase 2)
=================================================
WHY THIS IS DIFFERENT FROM THE MT5 SUPERVISOR (Phase 1):
  MT5's Python API holds one terminal session per OS process, so MT5
  accounts need one process each (see core/process_supervisor.py).
  Zerodha (kiteconnect) and Binance (ccxt) have no such constraint — each
  is just an authenticated HTTP client. This manager holds N of them,
  keyed by account_id, in ONE process. No subprocess, no port allocation,
  no supervisor loop needed — just a cache with credential loading.

WHAT IT FIXES:
  Before this, both brokers/zerodha_agent.py (via get_zerodha()) and the
  binance execution path (via core/execution_agent.py -> core/data_agent.py)
  were process-wide singletons reading ONE set of credentials from env
  vars. Two accounts of the same broker type could never coexist. This
  manager loads each account's own decrypted credentials from the DB and
  gives every account_id its own agent instance.

USAGE (from broker_router.py):
  from core.multi_tenant_broker_manager import get_multi_tenant_manager
  mgr = get_multi_tenant_manager(db)
  agent = mgr.get_agent(account_id)   # BinanceAgent or ZerodhaAgent, ready to use
  if agent:
      agent.place_order(...)
"""

import logging
import threading
from typing import Optional, Union

from brokers.binance_agent import BinanceAgent
from brokers.zerodha_agent import ZerodhaAgent
from core.secrets_vault import decrypt_secret

logger = logging.getLogger("crave.multi_tenant_broker_manager")

BrokerAgent = Union[BinanceAgent, ZerodhaAgent]


class MultiTenantBrokerManager:

    def __init__(self, db):
        self.db = db
        self._agents: dict[int, BrokerAgent] = {}   # account_id -> agent instance
        self._lock = threading.Lock()

    def get_agent(self, account_id: int) -> Optional[BrokerAgent]:
        """Returns a connected agent for this account, creating and
        connecting it on first use, and reusing it on every call after.
        Returns None if the account doesn't exist, isn't a Zerodha/Binance
        account, or fails to connect."""
        with self._lock:
            cached = self._agents.get(account_id)
        if cached is not None:
            return cached

        acc = self.db.get_account(account_id)
        if not acc:
            logger.error(f"[MultiTenant] Account {account_id} not found")
            return None

        broker = (acc.get("broker") or "").lower()
        if broker not in ("binance", "zerodha"):
            logger.error(
                f"[MultiTenant] Account {account_id} is broker='{broker}' — "
                f"this manager only handles binance/zerodha. MT5 accounts "
                f"run through core/process_supervisor.py instead."
            )
            return None

        agent = self._create_agent(account_id, broker, acc)
        if agent is None:
            return None

        with self._lock:
            self._agents[account_id] = agent
        logger.info(f"[MultiTenant] Account {account_id} ({broker}) connected and cached")
        return agent

    def _create_agent(self, account_id: int, broker: str, acc: dict) -> Optional[BrokerAgent]:
        # acc["login"] / acc["password"] hold, per broker:
        #   binance: api_key / (encrypted) api_secret
        #   zerodha: api_key / (encrypted) access_token   (see account_endpoints_patch.py's
        #            api_zerodha_complete — Zerodha never stores a password, only the
        #            post-OAuth access token, refreshed daily via daily_login())
        api_key = acc.get("login")
        try:
            secret_or_token = decrypt_secret(acc.get("password") or "")
        except Exception as e:
            logger.error(f"[MultiTenant] Account {account_id} credential decrypt failed: {e}")
            return None

        if broker == "binance":
            agent = BinanceAgent(api_key=api_key, api_secret=secret_or_token)
            if not agent.connect():
                logger.error(f"[MultiTenant] Account {account_id} Binance connect failed")
                self.db.execute(
                    "UPDATE accounts SET status='error', last_error=? WHERE id=?",
                    ("Binance reconnect failed", account_id),
                )
                return None
            return agent

        if broker == "zerodha":
            agent = ZerodhaAgent(api_key=api_key, access_token=secret_or_token, account_id=account_id)
            if not agent.is_authenticated():
                # Access token expired (past 06:00 IST) or was never valid.
                # This account needs a fresh daily_login() OAuth round-trip —
                # not something this manager can complete on its own since
                # it requires a browser redirect. Surface it clearly instead
                # of silently returning a dead agent.
                logger.warning(
                    f"[MultiTenant] Account {account_id} Zerodha token expired/invalid — "
                    f"needs daily_login() to re-authenticate."
                )
                self.db.execute(
                    "UPDATE accounts SET status='needs_reauth', last_error=? WHERE id=?",
                    ("Zerodha access token expired — daily re-login required", account_id),
                )
                return None
            return agent

        return None

    def refresh_agent(self, account_id: int):
        """Evict a cached agent so the next get_agent() call rebuilds it
        from fresh DB credentials — call this after a re-auth (e.g. a new
        Zerodha access_token was just saved via daily_login/complete_login)
        or a credential rotation, so the manager doesn't keep using a
        stale/disconnected instance."""
        with self._lock:
            self._agents.pop(account_id, None)
        logger.info(f"[MultiTenant] Account {account_id} agent evicted, will reconnect on next use")

    def remove_agent(self, account_id: int):
        """Disconnect and drop an account entirely — call when an account
        is deleted or manually disconnected."""
        with self._lock:
            agent = self._agents.pop(account_id, None)
        if agent and hasattr(agent, "disconnect"):
            try:
                agent.disconnect()
            except Exception:
                pass
        logger.info(f"[MultiTenant] Account {account_id} agent removed")

    def active_accounts(self) -> list:
        with self._lock:
            return list(self._agents.keys())


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETON ACCESSOR — one manager per process, mirrors get_supervisor()'s pattern
# ─────────────────────────────────────────────────────────────────────────────

_manager = None


def get_multi_tenant_manager(db) -> MultiTenantBrokerManager:
    global _manager
    if _manager is None:
        _manager = MultiTenantBrokerManager(db)
    return _manager
