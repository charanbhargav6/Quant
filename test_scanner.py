"""
FULL PIPELINE DIAGNOSTIC
========================
Tests every single gate in the signal chain to find which one
is blocking trades. Runs on XAUUSD and EURUSD.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding='utf-8')

from datetime import datetime, timezone

def diagnose_symbol(symbol):
    print(f"\n{'=' * 60}")
    print(f"  DIAGNOSING: {symbol}")
    print(f"{'=' * 60}")
    
    # Gate 1: Scanner tradeable?
    print("\n[GATE 1] Scanner Score:")
    try:
        from engines.instrument_scanner import scanner
        result = scanner._score_instrument(symbol)
        print(f"  Score: {result['score']}/13 | Tradeable: {result['tradeable']}")
        print(f"  Reason: {result['reason']}")
        for k, v in result['breakdown'].items():
            print(f"    {k}: {v}")
        if not result['tradeable']:
            print("  >>> BLOCKED HERE: Scanner says not tradeable")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Gate 2: Bias engine
    print("\n[GATE 2] Daily Bias:")
    try:
        from engines.daily_bias_engine import bias_engine
        bias = bias_engine.get_bias(symbol)
        if bias:
            print(f"  Bias: {bias.get('bias')} | Strength: {bias.get('strength')}")
            if bias.get('bias') == 'NO_TRADE':
                print("  >>> BLOCKED HERE: Bias is NO_TRADE")
        else:
            print("  No bias data (None returned)")
            print("  >>> BLOCKED HERE: No daily bias computed")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Gate 3: Kill Zone
    print("\n[GATE 3] Kill Zone:")
    try:
        from core.trading_loop import is_in_kill_zone, _get_session_name
        in_kz, kz_name = is_in_kill_zone(symbol)
        hour = datetime.now(timezone.utc).hour
        session = _get_session_name(hour)
        print(f"  In Kill Zone: {in_kz} ({kz_name})")
        print(f"  Current UTC hour: {hour} | Session: {session}")
        if not in_kz:
            print("  >>> NOTE: Outside kill zone (reduced confidence, still proceeds)")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Gate 4: Regime
    print("\n[GATE 4] Regime Classifier:")
    try:
        from ml.regime_classifier import regime_model
        from data.market_data_router import get_data_router
        df_1h = get_data_router().get_ohlcv(symbol, "1h", limit=100)
        if df_1h is not None:
            regime = regime_model.predict(symbol, df_1h)
            favourable = regime_model.is_favourable(regime)
            print(f"  Regime: {regime} | Favourable: {favourable}")
            if not favourable and regime != "RANGING":
                print("  >>> BLOCKED HERE: Unfavourable regime and not ranging")
            elif regime == "RANGING":
                print("  >>> Routes to Mean Reversion (not SMC)")
        else:
            print("  No 1H data available")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Gate 5: SMC Analysis (the actual signal generation)
    print("\n[GATE 5] SMC Signal Analysis:")
    try:
        from data.market_data_router import get_data_router
        router = get_data_router()
        df_15m = router.get_ohlcv(symbol, "15m", limit=500)
        df_1h = router.get_ohlcv(symbol, "1h", limit=250)
        df_4h = router.get_ohlcv(symbol, "4h", limit=60)
        
        if df_15m is None or len(df_15m) < 60:
            print(f"  Insufficient 15m data: {len(df_15m) if df_15m is not None else 'None'}")
            print("  >>> BLOCKED HERE: Not enough data")
        else:
            import numpy as np
            from config.config import get_asset_params
            is_gold = get_asset_params(symbol).get("label") == "Gold"
            
            if is_gold:
                from engines.gold_strategy import attach_gold_indicators, analyze_gold_context
                df_15m_ind = attach_gold_indicators(df_15m)
                context = analyze_gold_context(symbol, df_15m_ind, len(df_15m_ind) - 1)
                print(f"  Using: Gold Pullback Strategy")
            else:
                df_15m_copy = df_15m.copy()
                df_15m_copy['EMA_21']  = df_15m_copy['close'].ewm(span=21, adjust=False).mean()
                df_15m_copy['SMA_50']  = df_15m_copy['close'].rolling(50).mean()
                df_15m_copy['SMA_200'] = df_15m_copy['close'].rolling(200).mean()
                delta = df_15m_copy['close'].diff()
                gain  = delta.clip(lower=0).rolling(14).mean()
                loss  = (-delta.clip(upper=0)).rolling(14).mean()
                df_15m_copy['rsi_14'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
                
                from engines.hybrid_strategy import HybridStrategyAgent
                strategy = HybridStrategyAgent()
                fvg_catalog = strategy._build_fvg_catalog(df_15m_copy)
                ob_catalog  = strategy._build_ob_catalog(df_15m_copy)
                structure   = strategy._build_structure(df_15m_copy)
                
                context = strategy.analyze_market_context(
                    symbol, df_15m_copy,
                    i=len(df_15m_copy) - 1,
                    fvg_catalog=fvg_catalog,
                    ob_catalog=ob_catalog,
                    structure=structure
                )
                print(f"  Using: Hybrid SMC Strategy")
            
            if "error" in context:
                print(f"  ERROR: {context['error']}")
                print("  >>> BLOCKED HERE: Strategy returned error")
            else:
                confidence = context.get("Confidence_Pct", 0)
                grade_str  = context.get("Structure_Score", "C")
                macro_trend = context.get("Macro_Trend", "Unknown")
                
                print(f"  Grade: {grade_str}")
                print(f"  Confidence: {confidence}%")
                print(f"  Macro Trend: {macro_trend}")
                
                # Show breakdown
                breakdown = context.get("Confidence_Breakdown", {})
                if breakdown:
                    print(f"  Breakdown:")
                    for k, v in breakdown.items():
                        print(f"    {k}: {v}")
                
                # Check grade gate
                grade = None
                for g in ("A+", "A", "B+", "B"):
                    if g in grade_str:
                        grade = g
                        break
                
                if grade is None:
                    print(f"  >>> BLOCKED HERE: Grade '{grade_str}' is C (below minimum)")
                else:
                    # Check asset-specific grade gate
                    asset_p = get_asset_params(symbol)
                    asset_min_grade = asset_p.get("min_grade", "B+")
                    allowed_grades = {"A+": ["A+"], "A": ["A+", "A"], "B+": ["A+", "A", "B+"]}
                    if grade not in allowed_grades.get(asset_min_grade, ["A+", "A", "B+"]):
                        print(f"  >>> BLOCKED HERE: Grade {grade} below asset minimum {asset_min_grade}")
                    else:
                        print(f"  Grade {grade} passes asset gate (min: {asset_min_grade})")
                    
                    # Check confidence gate
                    from config.config import CONFIDENCE_GATES
                    inst_conf_gate = CONFIDENCE_GATES.get(symbol, CONFIDENCE_GATES.get("default", 0.40)) * 100
                    asset_min_conf = asset_p.get("min_conf", 50)
                    min_conf = max(asset_min_conf, inst_conf_gate)
                    
                    if confidence < min_conf:
                        print(f"  >>> BLOCKED HERE: Confidence {confidence}% < gate {min_conf}%")
                    else:
                        print(f"  Confidence {confidence}% passes gate {min_conf}%")
                    
                    # Check bias alignment
                    from engines.daily_bias_engine import bias_engine
                    bias = bias_engine.get_bias(symbol)
                    bias_dir = bias.get("bias", "NO_TRADE") if bias else "NO_TRADE"
                    
                    if macro_trend in ("Bullish", "Bearish"):
                        direction = "buy" if macro_trend == "Bullish" else "sell"
                    elif bias_dir in ("BUY", "SELL"):
                        direction = "buy" if bias_dir == "BUY" else "sell"
                    else:
                        direction = None
                        print(f"  >>> BLOCKED HERE: No direction (macro={macro_trend}, bias={bias_dir})")
                    
                    if direction and bias_dir in ("BUY", "SELL"):
                        expected = "buy" if bias_dir == "BUY" else "sell"
                        if direction != expected:
                            print(f"  >>> BLOCKED HERE: Direction conflict (signal={direction}, bias={bias_dir})")
                        else:
                            print(f"  Direction {direction} aligns with bias {bias_dir}")
                    
                    # MTF check
                    if direction:
                        from core.trading_loop import check_mtf_confluence
                        mtf_ok, mtf_mod, mtf_reason = check_mtf_confluence(symbol, direction, df_1h, df_4h)
                        print(f"\n[GATE 6] MTF Confluence:")
                        print(f"  Approved: {mtf_ok} | Modifier: {mtf_mod}")
                        print(f"  Reason: {mtf_reason}")
                        if not mtf_ok:
                            print(f"  >>> BLOCKED HERE: MTF conflict")

    except Exception as e:
        import traceback
        print(f"  ERROR: {e}")
        traceback.print_exc()

    # Gate 7: Streak gate
    print(f"\n[GATE 7] Streak Gate:")
    try:
        from core.streak_state import streak
        can, reason = streak.can_trade()
        print(f"  Can trade: {can} | Reason: {reason}")
        if not can:
            print(f"  >>> BLOCKED HERE: Streak gate")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Gate 8: Prop firm guard
    print(f"\n[GATE 8] Prop Firm Guard:")
    try:
        from core.prop_firm_guard import PropFirmGuard
        pfg = PropFirmGuard()
        result = pfg.check_trade(risk_pct=1.0, symbol=symbol)
        print(f"  Allowed: {result['allowed']}")
        if not result['allowed']:
            print(f"  Reason: {result['reason']}")
            print(f"  >>> BLOCKED HERE: Prop firm guard")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Gate 9: Calendar blackout
    print(f"\n[GATE 9] Economic Calendar:")
    try:
        from engines.economic_calendar import EconomicCalendar
        cal = EconomicCalendar()
        status = cal.is_blackout_now(symbol)
        print(f"  Blackout: {status.get('blackout', False)}")
        if status.get('blackout'):
            print(f"  Reason: {status.get('reason')}")
            print(f"  >>> BLOCKED HERE: Economic calendar blackout")
    except Exception as e:
        print(f"  ERROR: {e}")

# Run diagnostics
for sym in ['XAUUSD=X', 'EURUSD=X', 'GBPUSD=X', 'BTCUSDT']:
    diagnose_symbol(sym)

print(f"\n\n{'=' * 60}")
print("DIAGNOSTIC COMPLETE")
print(f"{'=' * 60}")
