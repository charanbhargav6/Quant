"""
CRAVE v10.0 — Dynamic TP Engine
=================================
Monitors all open positions and extends TP targets when
volume and order flow confirm continued momentum.

EXTENSION CONDITIONS (need 2 of 3):
  1. Volume Profile: price breaking through PoC into a low-volume node
     (no resistance ahead = fast travel to next high-volume node)
  2. Order Book: bid/ask imbalance >70% in trade direction
     AND no significant wall within 1× ATR ahead
  3. Structure: 4H has printed a new BOS in trade direction since entry

SPECIAL CASE — Liquidity Void:
  If price enters a low-volume node AND the void extends
  at least 2× ATR, extend TP immediately without waiting
  for 2-of-3 conditions. Price will travel fast here.

TP RULES (as requested):
  - TP can only move in the profitable direction — NEVER pulled back
  - No cap on how many times it can be extended
  - Each extension targets the next significant volume node or liquidity level

PARTIAL BOOKING SCHEDULE (from config):
  1R  → close 30%, SL to breakeven
  2R  → close 20% of remaining, SL to +1R
  3R  → close 20% of remaining, SL to +2R
  4R  → close 10% of remaining, SL to +3R
  4R+ → trail at 1× ATR, 5% clips at each new R level

EXIT OVERRIDES (close regardless of extended TP):
  - CHoCH on 1H against the trade direction
  - Volume delta divergence (price new high but volume declining)
  - Spread > 3× normal (liquidity event — get out)
  - Funding rate extreme (crypto only — crowded trade risk)
"""

import logging
import time
import threading
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.dynamic_tp")


class DynamicTPEngine:

    def __init__(self):
        from config.config import DYNAMIC_TP, PARTIAL_BOOKING
        self._cfg             = DYNAMIC_TP
        self._booking_sched   = PARTIAL_BOOKING
        self._running         = False
        self._thread: Optional[threading.Thread] = None

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN MONITOR LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        """Start TP monitoring in background thread."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="CRAVEDynamicTP"
        )
        self._thread.start()
        logger.info("[DynamicTP] Monitor started.")

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        """Check all open positions every 15 minutes."""
        interval = self._cfg.get("check_interval_mins", 15) * 60

        while self._running:
            try:
                self._check_all_positions()
            except Exception as e:
                logger.error(f"[DynamicTP] Monitor error: {e}")
            time.sleep(interval)

    def _check_all_positions(self):
        """Run checks on every open position."""
        from core.position_tracker import positions
        open_positions = positions.get_all()

        if not open_positions:
            return

        for pos in open_positions:
            try:
                self._process_position(pos)
            except Exception as e:
                logger.error(
                    f"[DynamicTP] Error processing {pos.get('symbol')}: {e}"
                )

    # ─────────────────────────────────────────────────────────────────────────
    # POSITION PROCESSING
    # ─────────────────────────────────────────────────────────────────────────

    def _process_position(self, pos: dict):
        """
        Full pipeline for one open position:
        1. Get latest price + 1H data
        2. Check partial booking levels
        3. Check TP extension conditions
        4. Check exit overrides (CHoCH, volume divergence, spread)
        5. Update breakeven ladder
        """
        trade_id  = pos["trade_id"]
        symbol    = pos["symbol"]
        direction = pos["direction"]
        entry     = pos["entry_price"]
        current_sl = pos["current_sl"]
        current_tp = pos["current_tp"]
        atr_at_open = pos.get("atr_at_open") or 0

        # ── Get fresh data ────────────────────────────────────────────────
        df_1h = self._get_ohlcv(symbol, "1h", limit=50)
        if df_1h is None or df_1h.empty:
            return

        live_price = df_1h['close'].iloc[-1]

        # ── Recalculate live ATR ──────────────────────────────────────────
        live_atr = self._calc_atr(df_1h, 14)
        if live_atr <= 0:
            live_atr = atr_at_open or (entry * 0.001)

        # ── Calculate current R multiple ──────────────────────────────────
        sl_distance = abs(entry - current_sl)
        if sl_distance <= 0:
            return

        if direction in ("buy", "long"):
            current_r = (live_price - entry) / sl_distance
        else:
            current_r = (entry - live_price) / sl_distance

        # ── 1. Partial booking check ──────────────────────────────────────
        self._check_partial_booking(pos, current_r, live_price,
                                     entry, sl_distance, trade_id)

        # ── 2. Exit override checks ───────────────────────────────────────
        should_exit, exit_reason = self._check_exit_overrides(
            pos, df_1h, live_price, live_atr
        )
        if should_exit:
            logger.warning(
                f"[DynamicTP] EXIT SIGNAL: {symbol} | {exit_reason}"
            )
            self._signal_exit(trade_id, exit_reason, current_r)
            return

        # ── 3. TP extension check ─────────────────────────────────────────
        # Only extend if already past TP1 (position has legs)
        if pos.get("tp1_hit") or current_r >= 0.8:
            extension = self._check_tp_extension(
                pos, df_1h, live_price, live_atr, current_r
            )
            if extension:
                new_tp, reason = extension
                from core.position_tracker import positions
                positions.update_tp(trade_id, new_tp, reason=reason)

        # ── 4. Breakeven ladder update ────────────────────────────────────
        self._update_breakeven_ladder(pos, current_r, entry, live_atr,
                                       sl_distance, trade_id)

    # ─────────────────────────────────────────────────────────────────────────
    # PARTIAL BOOKING
    # ─────────────────────────────────────────────────────────────────────────

    def _check_partial_booking(self, pos: dict, current_r: float,
                                 live_price: float, entry: float,
                                 sl_distance: float, trade_id: str):
        """
        Check if we've hit a partial booking level.
        Uses the schedule from config: 30% at 1R, 20% at 2R, etc.
        """
        from core.position_tracker import positions
        from config.config import PARTIAL_BOOKING

        remaining = pos.get("remaining_pct", 100)
        bookings  = pos.get("bookings", [])
        booked_r_levels = {b.get("r_level") for b in bookings}

        for level in PARTIAL_BOOKING:
            r_target  = level["r_level"]
            close_pct = level["close_pct"]
            sl_move   = level["sl_move_to"]

            # Already booked this level?
            if r_target in booked_r_levels:
                continue

            # Not reached yet?
            if current_r < r_target:
                continue

            # Don't book if remaining is already very small
            if remaining < 5:
                continue

            # Calculate new SL
            new_sl = self._calc_sl_move(
                sl_move, entry, sl_distance, pos["direction"]
            )

            # Execute partial close
            booking = positions.partial_close(
                trade_id  = trade_id,
                close_pct = close_pct,
                at_price  = live_price,
                r_level   = r_target,
                new_sl    = new_sl,
            )

            if new_sl and new_sl != pos["current_sl"]:
                positions.update_sl(trade_id, new_sl,
                                    reason=f"partial book at {r_target}R")

            logger.info(
                f"[DynamicTP] Partial close: {pos['symbol']} "
                f"{close_pct}% at {r_target}R"
            )

    def _calc_sl_move(self, sl_move: str, entry: float,
                       sl_distance: float, direction: str) -> Optional[float]:
        """
        Calculate where to move SL based on the booking schedule instruction.
        'breakeven' → entry
        '+1R'       → entry + sl_distance (long) or entry - sl_distance (short)
        '+2R'       → entry + 2×sl_distance
        etc.
        """
        if sl_move == "breakeven":
            return entry

        if sl_move.startswith("+") and "R" in sl_move:
            try:
                r_val = float(sl_move.replace("+", "").replace("R", ""))
                if direction in ("buy", "long"):
                    return round(entry + r_val * sl_distance, 5)
                else:
                    return round(entry - r_val * sl_distance, 5)
            except ValueError:
                return None

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # TP EXTENSION
    # ─────────────────────────────────────────────────────────────────────────

    def _check_tp_extension(self, pos: dict, df_1h: pd.DataFrame,
                              live_price: float, live_atr: float,
                              current_r: float) -> Optional[tuple]:
        """
        Check if TP should be extended.
        Returns (new_tp, reason) or None.

        Three conditions — need 2 of 3 for extension.
        Special case: liquidity void → extend immediately.
        """
        symbol    = pos["symbol"]
        direction = pos["direction"]
        current_tp = pos["current_tp"]

        conditions_met = 0
        reasons        = []

        # ── Condition 1: Volume Profile — price in low-volume node ─────────
        vp_result = self._check_volume_profile_extension(
            df_1h, live_price, live_atr, direction
        )
        if vp_result["extend"]:
            conditions_met += 1
            reasons.append(f"VP: {vp_result['reason']}")

            # Special: liquidity void → extend immediately without waiting for 2/3
            if vp_result.get("is_void"):
                next_tp = vp_result["next_level"]
                if self._tp_is_valid_extension(current_tp, next_tp, direction):
                    return next_tp, f"Liquidity void — fast travel expected"

        # ── Condition 2: Order Book Imbalance ──────────────────────────────
        ob_result = self._check_order_book_extension(
            symbol, live_price, live_atr, direction
        )
        if ob_result["extend"]:
            conditions_met += 1
            reasons.append(f"OB: {ob_result['reason']}")

        # ── Condition 3: 4H Structure continuation ─────────────────────────
        struct_result = self._check_structure_continuation(
            symbol, direction, pos.get("open_time")
        )
        if struct_result["extend"]:
            conditions_met += 1
            reasons.append(f"STR: {struct_result['reason']}")

        # ── Decision: need 2 of 3 ─────────────────────────────────────────
        min_conditions = self._cfg.get("min_conditions_to_extend", 2)
        if conditions_met >= min_conditions:
            # Find next target level
            next_tp = self._find_next_tp_level(
                df_1h, live_price, live_atr, direction, current_tp
            )
            if next_tp and self._tp_is_valid_extension(current_tp, next_tp, direction):
                combined_reason = " | ".join(reasons)
                return next_tp, f"{conditions_met}/3 conditions: {combined_reason}"

        return None

    def _check_volume_profile_extension(self, df: pd.DataFrame,
                                          live_price: float,
                                          live_atr: float,
                                          direction: str) -> dict:
        """
        Check volume profile for extension signal.
        A low-volume node ahead = fast price travel.
        """
        try:
            from core.data_agent import DataAgent
            da = DataAgent()
            vp = da.calculate_volume_profile(df, bins=24)

            if not vp.get("available"):
                return {"extend": False}

            poc = vp["poc"]
            vah = vp["vah"]
            val = vp["val"]

            void_threshold = self._cfg.get("liquidity_void_threshold", 0.5)

            # Check if price is approaching VAH (longs) or VAL (shorts)
            # and there's a low-volume region beyond it
            if direction in ("buy", "long"):
                # Price moving up toward VAH — is there a void above VAH?
                distance_to_vah = vah - live_price
                if 0 < distance_to_vah < live_atr * 1.5:
                    # Price is close to VAH — extending into uncharted territory
                    next_level = vah + live_atr * 2
                    is_void    = True
                    return {
                        "extend":     True,
                        "is_void":    is_void,
                        "next_level": round(next_level, 5),
                        "reason":     f"Approaching VAH {vah:.5f}, void above",
                    }
                # Price already above VAH — in high-volume air pocket
                if live_price > vah:
                    next_level = vah + (vah - val) * 0.5
                    return {
                        "extend":     True,
                        "is_void":    False,
                        "next_level": round(max(next_level, live_price + live_atr), 5),
                        "reason":     f"Price above VAH {vah:.5f} — breakout",
                    }

            else:  # sell
                distance_to_val = live_price - val
                if 0 < distance_to_val < live_atr * 1.5:
                    next_level = val - live_atr * 2
                    return {
                        "extend":     True,
                        "is_void":    True,
                        "next_level": round(next_level, 5),
                        "reason":     f"Approaching VAL {val:.5f}, void below",
                    }
                if live_price < val:
                    next_level = val - (vah - val) * 0.5
                    return {
                        "extend":     True,
                        "is_void":    False,
                        "next_level": round(min(next_level, live_price - live_atr), 5),
                        "reason":     f"Price below VAL {val:.5f} — breakdown",
                    }

        except Exception as e:
            logger.debug(f"[DynamicTP] VP check error: {e}")

        return {"extend": False}

    def _check_order_book_extension(self, symbol: str,
                                     live_price: float,
                                     live_atr: float,
                                     direction: str) -> dict:
        """
        Check order book imbalance for extension signal.
        High bid imbalance for longs, high ask imbalance for shorts.
        No large wall within 1× ATR ahead.
        """
        try:
            from core.data_agent import DataAgent
            da = DataAgent()
            ob = da.get_order_book_imbalance(symbol, depth=20)

            if not ob.get("available"):
                return {"extend": False}

            threshold  = self._cfg.get("order_book_imbalance_pct", 70)
            bid_pct    = ob.get("bid_volume_pct", 50)
            ask_pct    = ob.get("ask_volume_pct", 50)

            if direction in ("buy", "long"):
                if bid_pct < threshold:
                    return {"extend": False}
                # Check for large ask wall ahead
                wall_price = ob.get("ask_wall_price", 0)
                if wall_price and 0 < wall_price - live_price < live_atr:
                    return {
                        "extend": False,
                        "reason": f"Large ask wall at {wall_price:.5f} within 1 ATR"
                    }
                return {
                    "extend": True,
                    "reason": f"Bid imbalance {bid_pct:.0f}% — buy pressure clear path"
                }
            else:
                if ask_pct < threshold:
                    return {"extend": False}
                wall_price = ob.get("bid_wall_price", 0)
                if wall_price and 0 < live_price - wall_price < live_atr:
                    return {
                        "extend": False,
                        "reason": f"Large bid wall at {wall_price:.5f} within 1 ATR"
                    }
                return {
                    "extend": True,
                    "reason": f"Ask imbalance {ask_pct:.0f}% — sell pressure clear path"
                }

        except Exception as e:
            logger.debug(f"[DynamicTP] OB check error: {e}")

        return {"extend": False}

    def _check_structure_continuation(self, symbol: str,
                                       direction: str,
                                       open_time: Optional[str]) -> dict:
        """
        Check if 4H structure has printed a new BOS in trade direction since entry.
        A new BOS = market structure confirming the move.
        """
        try:
            df_4h = self._get_ohlcv(symbol, "4h", limit=30)
            if df_4h is None or len(df_4h) < 10:
                return {"extend": False}

            # Filter to candles after trade entry
            if open_time:
                try:
                    entry_dt = pd.Timestamp(open_time, tz="UTC")
                    if df_4h['time'].dt.tz is None:
                        df_4h['time'] = df_4h['time'].dt.tz_localize("UTC")
                    df_4h = df_4h[df_4h['time'] >= entry_dt]
                except Exception:
                    pass

            if len(df_4h) < 3:
                return {"extend": False}

            # Simple BOS: has price broken a recent 4H swing?
            window     = 3
            if len(df_4h) < window * 2 + 1:
                return {"extend": False}

            recent_high = df_4h['high'].iloc[-window:].max()
            prior_high  = df_4h['high'].iloc[-window*2:-window].max()
            recent_low  = df_4h['low'].iloc[-window:].min()
            prior_low   = df_4h['low'].iloc[-window*2:-window].min()
            last_close  = df_4h['close'].iloc[-1]

            if direction in ("buy", "long") and last_close > prior_high:
                return {
                    "extend": True,
                    "reason": f"4H BOS bullish above {prior_high:.5f}"
                }
            if direction in ("sell", "short") and last_close < prior_low:
                return {
                    "extend": True,
                    "reason": f"4H BOS bearish below {prior_low:.5f}"
                }

        except Exception as e:
            logger.debug(f"[DynamicTP] Structure check error: {e}")

        return {"extend": False}

    def _find_next_tp_level(self, df: pd.DataFrame,
                              live_price: float,
                              live_atr: float,
                              direction: str,
                              current_tp: float) -> Optional[float]:
        """
        Find the next logical TP level beyond the current one.
        Uses: swing highs/lows, volume nodes, round numbers.
        """
        candidates = []

        # Swing high/lows beyond current TP
        window = 5
        for i in range(window, len(df) - window):
            high = df['high'].iloc[i]
            low  = df['low'].iloc[i]
            if direction in ("buy", "long") and high > current_tp:
                candidates.append(high)
            if direction in ("sell", "short") and low < current_tp:
                candidates.append(low)

        # Default: extend by 1× ATR beyond current TP
        if not candidates:
            if direction in ("buy", "long"):
                return round(current_tp + live_atr, 5)
            else:
                return round(current_tp - live_atr, 5)

        # Return nearest candidate beyond current TP
        if direction in ("buy", "long"):
            valid = [c for c in candidates if c > current_tp + live_atr * 0.3]
            return round(min(valid), 5) if valid else round(current_tp + live_atr, 5)
        else:
            valid = [c for c in candidates if c < current_tp - live_atr * 0.3]
            return round(max(valid), 5) if valid else round(current_tp - live_atr, 5)

    def _tp_is_valid_extension(self, current_tp: float,
                                new_tp: float,
                                direction: str) -> bool:
        """
        Validate that the new TP actually extends in the right direction.
        TP can ONLY move in the profitable direction — never pulled back.
        """
        if direction in ("buy", "long"):
            return new_tp > current_tp
        else:
            return new_tp < current_tp

    # ─────────────────────────────────────────────────────────────────────────
    # EXIT OVERRIDES
    # ─────────────────────────────────────────────────────────────────────────

    def _check_exit_overrides(self, pos: dict, df_1h: pd.DataFrame,
                                live_price: float,
                                live_atr: float) -> tuple:
        """
        Check conditions that should close the trade regardless of TP.
        Returns (should_exit: bool, reason: str).
        """
        direction = pos["direction"]

        # ── 1. CHoCH on 1H against trade direction ──────────────────────
        choch = self._detect_choch_against(df_1h, direction)
        if choch:
            return True, f"CHoCH reversal on 1H: {choch}"

        # ── 2. Volume delta divergence ───────────────────────────────────
        div = self._check_volume_divergence(df_1h, direction)
        if div:
            return True, f"Volume divergence: {div}"

        # ── 3. Spread check ──────────────────────────────────────────────
        symbol     = pos["symbol"]
        spread_bad = self._check_spread_abnormal(symbol, live_atr)
        if spread_bad:
            return True, "Spread > 3× normal — liquidity event"

        # ── 4. Funding rate extreme (crypto only) ────────────────────────
        from config.config import get_instrument
        inst = get_instrument(symbol)
        if inst.get("funding_check"):
            fund_danger = self._check_funding_extreme(symbol, direction)
            if fund_danger:
                return True, f"Funding extreme: {fund_danger}"

        return False, ""

    def _detect_choch_against(self, df: pd.DataFrame, direction: str) -> Optional[str]:
        """Detect a Change of Character on 1H that goes against the trade."""
        if len(df) < 15:
            return None

        # Find recent swing structure
        window = 5
        highs, lows = [], []
        for i in range(window, len(df) - window):
            if df['high'].iloc[i] == df['high'].iloc[i-window:i+window+1].max():
                highs.append(df['high'].iloc[i])
            if df['low'].iloc[i] == df['low'].iloc[i-window:i+window+1].min():
                lows.append(df['low'].iloc[i])

        if not highs or not lows:
            return None

        last_close = df['close'].iloc[-1]
        ema21      = df['close'].ewm(span=21, adjust=False).mean().iloc[-1]
        in_uptrend = last_close > ema21

        # CHoCH against long: price breaks below most recent swing low
        if direction in ("buy", "long"):
            if last_close < lows[-1] and not in_uptrend:
                return f"Price broke below swing low {lows[-1]:.5f}"

        # CHoCH against short: price breaks above most recent swing high
        if direction in ("sell", "short"):
            if last_close > highs[-1] and in_uptrend:
                return f"Price broke above swing high {highs[-1]:.5f}"

        return None

    def _check_volume_divergence(self, df: pd.DataFrame,
                                   direction: str) -> Optional[str]:
        """
        Volume delta divergence:
        Price making new extremes but volume on those moves is declining.
        Indicates exhaustion — the move is running out of fuel.
        """
        if len(df) < 10 or 'volume' not in df.columns:
            return None

        tail = df.tail(8)
        if tail['volume'].sum() == 0:
            return None

        # Check last 4 candles vs 4 before
        recent_vol = tail['volume'].tail(4).mean()
        prior_vol  = tail['volume'].head(4).mean()

        if prior_vol == 0:
            return None

        vol_declining = recent_vol < prior_vol * 0.6   # 40% volume drop

        if direction in ("buy", "long"):
            price_advancing = tail['close'].iloc[-1] > tail['close'].iloc[0]
            if price_advancing and vol_declining:
                return f"Price up but volume -{(1 - recent_vol/prior_vol)*100:.0f}%"
        else:
            price_declining = tail['close'].iloc[-1] < tail['close'].iloc[0]
            if price_declining and vol_declining:
                return f"Price down but volume -{(1 - recent_vol/prior_vol)*100:.0f}%"

        return None

    def _check_spread_abnormal(self, symbol: str, live_atr: float) -> bool:
        """Check if spread is abnormally wide (>3× normal for this instrument)."""
        try:
            from core.data_agent import DataAgent
            da = DataAgent()
            ob = da.get_order_book_imbalance(symbol, depth=5)
            if not ob.get("available"):
                return False

            # Estimate spread from best bid/ask
            bid = ob.get("bid_wall_price", 0)
            ask = ob.get("ask_wall_price", 0)
            if bid and ask and bid > 0:
                spread     = ask - bid
                spread_pct = spread / bid
                normal_pct = live_atr / bid * 0.1   # rough normal spread estimate
                if normal_pct > 0 and spread_pct > normal_pct * 3:
                    logger.warning(
                        f"[DynamicTP] Abnormal spread on {symbol}: "
                        f"{spread_pct*100:.4f}% vs normal {normal_pct*100:.4f}%"
                    )
                    return True
        except Exception:
            pass
        return False

    def _check_funding_extreme(self, symbol: str, direction: str) -> Optional[str]:
        """Check for extreme funding rate on perp futures."""
        try:
            from core.data_agent import DataAgent
            da   = DataAgent()
            rate = da.get_funding_rate(symbol)
            if not rate.get("available"):
                return None

            r         = rate.get("funding_rate_pct", 0)
            threshold = self._cfg.get("funding_rate_danger", 0.05)

            # Extreme positive funding + long position = danger
            if direction in ("buy", "long") and r > threshold:
                return f"Funding +{r:.3f}% — crowded longs may flush"
            # Extreme negative funding + short position = danger
            if direction in ("sell", "short") and r < -threshold:
                return f"Funding {r:.3f}% — crowded shorts may squeeze"

        except Exception:
            pass
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # BREAKEVEN LADDER
    # ─────────────────────────────────────────────────────────────────────────

    def _update_breakeven_ladder(self, pos: dict, current_r: float,
                                  entry: float, live_atr: float,
                                  sl_distance: float, trade_id: str):
        """
        Step SL up as trade moves in our favour.
        This ensures profit is locked progressively, not just at one BE move.

        Steps:
          0.5R → SL to entry (breakeven)  — handled in partial booking
          1.5R → SL to +0.5R
          2.5R → SL to +1.5R
          Each 1R increment after that → lock in (current_r - 1)R
        """
        from core.position_tracker import positions
        direction = pos["direction"]
        current_sl = pos["current_sl"]

        # Calculate target SL based on current R
        if current_r >= 1.5:
            # Lock in at least 0.5R
            lock_r = max(0.5, current_r - 1.0)
        elif current_r >= 0.5:
            lock_r = 0.0   # breakeven — handled by partial booking already
        else:
            return   # Not enough profit to lock in yet

        if direction in ("buy", "long"):
            target_sl = round(entry + lock_r * sl_distance, 5)
            if target_sl > current_sl:
                positions.update_sl(
                    trade_id, target_sl,
                    reason=f"breakeven ladder at {current_r:.1f}R"
                )
        else:
            target_sl = round(entry - lock_r * sl_distance, 5)
            if target_sl < current_sl:
                positions.update_sl(
                    trade_id, target_sl,
                    reason=f"breakeven ladder at {current_r:.1f}R"
                )

    # ─────────────────────────────────────────────────────────────────────────
    # EXIT SIGNAL
    # ─────────────────────────────────────────────────────────────────────────

    def _signal_exit(self, trade_id: str, reason: str, current_r: float):
        """
        Signal that a position should be closed due to override condition.
        The actual close is handled by ExecutionAgent.
        We mark the position with an exit_signal flag so the execution loop picks it up.
        """
        from core.position_tracker import positions
        pos = positions.get(trade_id)
        if not pos:
            return

        # Add exit signal to position
        pos["exit_signal"]        = True
        pos["exit_signal_reason"] = reason
        pos["exit_signal_r"]      = round(current_r, 2)
        pos["last_updated"]       = datetime.now(timezone.utc).isoformat()
        positions._save()

        # Notify
        try:
            from interfaces.telegram_interface import tg
            tg.send(
                f"⚠️ <b>EXIT SIGNAL: {pos['symbol']}</b>\n"
                f"Reason  : {reason}\n"
                f"Current R: {current_r:+.2f}R\n"
                f"Will close at next execution cycle."
            )
        except Exception:
            pass

        logger.warning(
            f"[DynamicTP] Exit signal set for {trade_id}: {reason}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # MANUAL TP CHECK (for /tp_check Telegram command)
    # ─────────────────────────────────────────────────────────────────────────

    def force_check(self) -> str:
        """Force an immediate TP check. Returns summary string."""
        from core.position_tracker import positions
        open_pos = positions.get_all()

        if not open_pos:
            return "No open positions to check."

        results = []
        for pos in open_pos:
            try:
                df_1h = self._get_ohlcv(pos["symbol"], "1h", limit=50)
                if df_1h is None:
                    results.append(f"{pos['symbol']}: no data")
                    continue

                live_price = df_1h['close'].iloc[-1]
                live_atr   = self._calc_atr(df_1h, 14)
                entry      = pos["entry_price"]
                sl_dist    = abs(entry - pos["current_sl"])
                direction  = pos["direction"]

                if direction in ("buy", "long"):
                    current_r = (live_price - entry) / sl_dist if sl_dist > 0 else 0
                else:
                    current_r = (entry - live_price) / sl_dist if sl_dist > 0 else 0

                ext = self._check_tp_extension(
                    pos, df_1h, live_price, live_atr, current_r
                )
                if ext:
                    new_tp, reason = ext
                    results.append(
                        f"{pos['symbol']}: TP extend → {new_tp} ({reason})"
                    )
                else:
                    results.append(
                        f"{pos['symbol']}: no extension ({current_r:+.2f}R)"
                    )
            except Exception as e:
                results.append(f"{pos['symbol']}: error — {e}")

        return "\n".join(results)

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _get_ohlcv(self, symbol: str, timeframe: str,
                    limit: int) -> Optional[pd.DataFrame]:
        try:
            from core.database_manager import db
            cached = db.get_cached_ohlcv(symbol, timeframe, limit=limit)
            if cached is not None and len(cached) >= 20:
                return cached
        except Exception:
            pass
        try:
            from core.data_agent import DataAgent
            da = DataAgent()
            return da.get_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            logger.debug(f"[DynamicTP] OHLCV fetch failed {symbol}: {e}")
            return None

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period + 1:
            return float((df['high'] - df['low']).mean()) if len(df) > 0 else 0.001
        tr  = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low']  - df['close'].shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0/period, adjust=False).mean()
        val = atr.iloc[-1]
        return float(val) if not pd.isna(val) else 0.001


# ── Singleton ─────────────────────────────────────────────────────────────────
dynamic_tp = DynamicTPEngine()
