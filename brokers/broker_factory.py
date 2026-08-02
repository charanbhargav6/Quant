import logging
from config.config import BROKER_TYPE

logger = logging.getLogger("crave.broker_factory")

_active_broker = None

def get_broker():
    """
    Returns the singleton broker instance for the current profile.
    Loads MT5 or Binance based on BROKER_TYPE in config.
    """
    global _active_broker
    if _active_broker is not None:
        return _active_broker

    if BROKER_TYPE == "BINANCE":
        logger.info("[BrokerFactory] Initializing Binance Agent")
        from brokers.binance_agent import BinanceAgent
        _active_broker = BinanceAgent()
    else:
        logger.info("[BrokerFactory] Initializing MT5 Agent")
        from brokers.mt5_agent import MT5Agent
        _active_broker = MT5Agent()
        
    return _active_broker
