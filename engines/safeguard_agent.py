import logging

logger = logging.getLogger(__name__)

class SafeguardAgent:
    """
    Deterministic safeguard for AI Trade Managers (like Jarvis).
    Validates any SL/TP modification commands against strict mathematical boundaries.
    """
    def __init__(self):
        pass
        
    def validate_sl_trail(self, entry: float, current_sl: float, new_sl: float, direction: str, live_spread: float = 0.0) -> bool:
        """
        Ensures the new SL is strictly in profit compared to the entry,
        does not widen the stop, and obeys a minimum distance (2 * live_spread) from entry
        to prevent immediate stop-outs.
        """
        try:
            min_distance = live_spread * 2
            
            if direction in ('buy', 'long'):
                if new_sl <= entry:
                    logger.warning(f"[Safeguard] Rejected: New SL {new_sl} is not in profit (entry {entry}).")
                    return False
                if current_sl != 0 and new_sl < current_sl:
                    logger.warning(f"[Safeguard] Rejected: New SL {new_sl} widens the stop (current {current_sl}).")
                    return False
                if (new_sl - entry) < min_distance:
                    logger.warning(f"[Safeguard] Rejected: New SL {new_sl} is too close to entry (spread buffer).")
                    return False
            else:
                if new_sl >= entry:
                    logger.warning(f"[Safeguard] Rejected: New SL {new_sl} is not in profit (entry {entry}).")
                    return False
                if current_sl != 0 and new_sl > current_sl:
                    logger.warning(f"[Safeguard] Rejected: New SL {new_sl} widens the stop (current {current_sl}).")
                    return False
                if (entry - new_sl) < min_distance:
                    logger.warning(f"[Safeguard] Rejected: New SL {new_sl} is too close to entry (spread buffer).")
                    return False
            return True
        except Exception as e:
            logger.error(f"[Safeguard] Validation error: {e}")
            return False

safeguard = SafeguardAgent()
