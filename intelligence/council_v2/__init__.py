# intelligence/council_v2/__init__.py
"""
CRAVE Council v2 — Adversarial AI Intelligence Council

Public API:
    from intelligence.council_v2 import get_debate_engine
    engine = get_debate_engine()
    result = engine.run(context)         # full 3-round debate
    result = engine.quick_market_assessment("EURUSD", context)  # lightweight
"""

from .debate_engine import get_debate_engine, DebateEngine
from .provider_registry import get_registry, ProviderRegistry

__all__ = ["get_debate_engine", "DebateEngine", "get_registry", "ProviderRegistry"]

