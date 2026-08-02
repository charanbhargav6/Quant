"""
Jarvis Manager - Autonomous AI Trade Manager
Runs in the background, periodically reviews open positions, and sends them
to the AI council (Gemini/Ollama) for structural analysis.
Executes HOLD, CLOSE_EARLY, EXTEND_TP, or PARTIAL_CLOSE dynamically.
"""

import json
import logging
import time
import threading
import re
import hashlib
from typing import Optional
from collections import OrderedDict

logger = logging.getLogger("crave.jarvis_manager")

# --- Global LLM Cache & Limiter ---
MAX_LLM_CALLS_PER_MIN = 10
_llm_tokens = MAX_LLM_CALLS_PER_MIN
_last_token_refill = time.time()
_llm_state_cache = OrderedDict()
# ----------------------------------

class JarvisManager:
    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="JarvisManager"
        )
        self._thread.start()
        logger.info("[JarvisManager] Autonomous AI Trade Manager started.")

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        # Default scan interval: 15 minutes
        interval = 15 * 60

        while self._running:
            try:
                self._check_all_positions()
            except Exception as e:
                logger.error(f"[JarvisManager] Monitor error: {e}")
            time.sleep(interval)

    def _check_all_positions(self):
        try:
            from core.position_tracker import positions
            from brokers.broker_factory import get_broker
            broker = get_broker()
        except ImportError:
            return

        if not broker or not broker.is_connected():
            return
            
        # Use position_tracker instead of MT5 directly to avoid race conditions
        open_positions = positions.get_all()
        if not open_positions:
            return

        for pos in open_positions:
            try:
                self._analyze_and_manage(pos, broker)
            except Exception as e:
                logger.error(f"[JarvisManager] Error managing {pos.get('symbol')}: {e}")

    def _get_live_market_data(self, symbol: str) -> str:
        """Fetches latest 4H/1H OHLCV to give Jarvis structural awareness."""
        try:
            from core.data_agent import get_data_agent
            da = get_data_agent()
            df_4h = da.get_ohlcv(symbol, timeframe="4h", limit=5)
            df_1h = da.get_ohlcv(symbol, timeframe="1h", limit=10)
            
            if df_4h is None or df_1h is None:
                return "Market data unavailable."
                
            # Basic structural string for the LLM
            def df_to_string(df):
                return "\n".join([
                    f"Bar {i}: O:{row['open']} H:{row['high']} L:{row['low']} C:{row['close']}"
                    for i, row in df.iterrows()
                ])
                
            return f"Recent 4H PA:\n{df_to_string(df_4h)}\n\nRecent 1H PA:\n{df_to_string(df_1h)}"
        except Exception as e:
            return f"Error fetching PA: {e}"

    def _analyze_and_manage(self, pos: dict, broker):
        symbol = pos.get("symbol")
        ticket = pos.get("ticket")
        if not ticket:
            return
            
        direction = pos.get("direction")
        entry = pos.get("entry_price")
        current_sl = pos.get("current_sl")
        current_tp = pos.get("current_tp")
        profit = pos.get("profit", 0)
        
        market_data = self._get_live_market_data(symbol)
        
        system_prompt = (
            "You are Jarvis, an elite, highly aggressive autonomous AI Trade Manager. "
            "You are monitoring a live open position. Analyze the recent market structure (4H/1H). "
            "Look for liquidity hunts, CHoCH, or BOS. "
            "Decide on ONE of the following actions:\n"
            "- HOLD: Structure remains valid, let the trade breathe.\n"
            "- CLOSE_EARLY: Structure shifted against us (e.g. CHoCH). Close now.\n"
            "- EXTEND_TP: Massive momentum in our favor. Extend TP to capture a runner.\n"
            "- PARTIAL_CLOSE: Good profit, but volatility is high. Lock in some profits.\n\n"
            "RULE FOR EXTEND_TP: You MUST instruct trailing the SL to +0.5R minimum before extending TP.\n\n"
            "OUTPUT FORMAT:\n"
            "You MUST output a JSON block at the very end of your response, wrapped in [COMMAND]...[/COMMAND].\n"
            'Example 1: [COMMAND]{"action": "CLOSE_EARLY", "reason": "1H Bearish Engulfing"}[/COMMAND]\n'
            'Example 2: [COMMAND]{"action": "EXTEND_TP", "trail_sl_price": 1.1050, "new_tp_price": 1.1200, "reason": "BOS formed"}[/COMMAND]\n'
            'Example 3: [COMMAND]{"action": "HOLD", "reason": "Choppy PA, wait for resolution"}[/COMMAND]\n'
            'Example 4: [COMMAND]{"action": "PARTIAL_CLOSE", "percentage": 0.5, "reason": "Taking 50% off table"}[/COMMAND]'
        )
        
        user_prompt = (
            f"LIVE TRADE: {symbol} {direction} @ {entry}\n"
            f"SL: {current_sl} | TP: {current_tp}\n"
            f"Current Profit: ${profit}\n\n"
            f"MARKET DATA:\n{market_data}\n\n"
            "What is your decision?"
        )

        try:
            import os
            from intelligence.jarvis_chat import call_model
            
            # --- PHASE 1: RATE LIMITER & STATE CACHE ---
            global _llm_tokens, _last_token_refill, _llm_state_cache
            
            # Refill bucket
            now = time.time()
            if now - _last_token_refill > 60:
                _llm_tokens = 10  # 10 calls per min
                _last_token_refill = now
                
            # Check Hash
            state_hash = hashlib.sha256(user_prompt.encode()).hexdigest()
            if state_hash in _llm_state_cache:
                reply = _llm_state_cache[state_hash]
                logger.debug(f"[JarvisManager] Cache hit for {symbol}. Bypassing LLM.")
                self._execute_llm_decision(reply, pos, broker)
                return
                
            if _llm_tokens <= 0:
                logger.warning(f"[JarvisManager] LLM rate limit hit! Skipping {symbol} this cycle.")
                return
                
            _llm_tokens -= 1
            # -------------------------------------------
            
            api_key = os.environ.get("GEMINI_API_KEY", "")
            
            # Using standard gemini flash for fast background inference
            reply, status = call_model("gemini-3.5-flash", system_prompt, user_prompt, api_key)
            if status != 200:
                logger.error(f"[JarvisManager] LLM call failed: {reply}")
                return
                
            # Store in cache
            _llm_state_cache[state_hash] = reply
            if len(_llm_state_cache) > 50:
                _llm_state_cache.popitem(last=False)
                
            self._execute_llm_decision(reply, pos, broker)
            
        except Exception as e:
            logger.error(f"[JarvisManager] Error in LLM analysis: {e}")

    def _execute_llm_decision(self, reply: str, pos: dict, broker):
        from quant_server import push_council
        from core.position_tracker import positions
        import traceback
        
        ticket = pos["ticket"]
        symbol = pos["symbol"]
        trade_id = pos.get("trade_id", str(ticket))
        
        # Extract JSON command
        match = re.search(r"\[COMMAND\](.*?)\[/COMMAND\]", reply, re.DOTALL)
        if not match:
            logger.warning(f"[JarvisManager] No command block found for {symbol}.")
            return
            
        try:
            json_str = match.group(1).strip()
            # Strip markdown if hallucinated
            if json_str.startswith("```json"):
                json_str = json_str[7:]
            if json_str.startswith("```"):
                json_str = json_str[3:]
            if json_str.endswith("```"):
                json_str = json_str[:-3]
            json_str = json_str.strip()
            
            cmd = json.loads(json_str)
            action = cmd.get("action", "HOLD")
            reason = cmd.get("reason", "No reason provided by LLM")
            
            if action == "HOLD":
                push_council("Jarvis AI", f"[{symbol}] Holding position. {reason}")
                
            elif action == "CLOSE_EARLY":
                # Close via MT5 and immediately update local state
                result = broker.close_position(ticket=ticket)
                if result:
                    positions.close(trade_id, pos.get("current_price", entry), reason)
                    push_council("Jarvis AI", f"[{symbol}] ⚠️ CLOSED EARLY. {reason}")
                else:
                    logger.error(f"[JarvisManager] Failed to close {symbol}.")
                    
            elif action == "EXTEND_TP":
                try:
                    new_sl = float(cmd.get("trail_sl_price"))
                    new_tp = float(cmd.get("new_tp_price"))
                    if new_sl and new_tp:
                        from engines.safeguard_agent import safeguard
                        live_spread = 0.0
                        sym_info = broker.get_symbol_info(symbol)
                        if sym_info and sym_info.get("spread") and sym_info.get("point"):
                            live_spread = sym_info["spread"] * sym_info["point"]

                        if safeguard.validate_sl_trail(pos.get("entry_price"), pos.get("current_sl"), new_sl, pos.get("direction"), live_spread):
                            success, actual_sl, actual_tp = broker.modify_sl(ticket, new_sl, new_tp)
                            if success:
                                # Update local tracker immediately with verified broker values
                                positions.update_sl(trade_id, actual_sl, "Jarvis Trailing SL")
                                positions.update_tp(trade_id, actual_tp, "Jarvis Extend TP")
                                push_council("Jarvis AI", f"[{symbol}] 🚀 EXTENDED TP ({actual_tp}) & TRAILED SL ({actual_sl}). {reason}")
                            else:
                                logger.error(f"[JarvisManager] Failed to modify {symbol}.")
                        else:
                            logger.error(f"[JarvisManager] Safeguard rejected SL trail for {symbol}.")
                except (ValueError, TypeError) as e:
                    logger.error(f"[JarvisManager] Type parsing failed for TP Extension: {e}")
                        
            elif action == "PARTIAL_CLOSE":
                try:
                    pct_str = str(cmd.get("percentage", 0.5)).replace('%', '')
                    pct = float(pct_str)
                    if pct > 1:
                        pct = pct / 100.0 # handle 50 instead of 0.5
                    
                    close_volume = max(0.01, round(pos.get("volume", 0.01) * pct, 2))
                    result = broker.close_position(ticket=ticket, volume=close_volume)
                    if result:
                        positions.partial_close(trade_id, pct, "Jarvis Partial Booking")
                        push_council("Jarvis AI", f"[{symbol}] 💰 PARTIAL CLOSE ({pct*100}%). {reason}")
                    else:
                        logger.error(f"[JarvisManager] Failed partial close on {symbol}.")
                except (ValueError, TypeError) as e:
                    logger.error(f"[JarvisManager] Type parsing failed for Partial Close: {e}")
                    
        except json.JSONDecodeError:
            logger.error(f"[JarvisManager] Invalid JSON from LLM: {match.group(1)}")
        except Exception as e:
            logger.error(f"[JarvisManager] Unexpected error: {e}\n{traceback.format_exc()}")

jarvis_manager = JarvisManager()
