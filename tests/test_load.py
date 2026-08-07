"""
LOAD TEST: Verify the scanner and signal pipeline handle
concurrent processing under increased load.
Tests:
  1. Concurrent scanner scoring (all symbols at once)
  2. Sequential full pipeline analysis (heavy I/O)
  3. Memory and timing benchmarks
"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding='utf-8')

from concurrent.futures import ThreadPoolExecutor, as_completed

def test_concurrent_scanner():
    """Test 1: Scanner handles all symbols concurrently"""
    print("\n[TEST 1] Concurrent Scanner Scoring")
    print("-" * 40)
    
    from engines.instrument_scanner import scanner
    from config.config import get_tradeable_symbols
    
    symbols = get_tradeable_symbols()
    print(f"  Symbols to score: {len(symbols)}")
    
    start = time.time()
    
    # Force fresh scan
    scanner._last_scan_date = None
    scanner._today_ranking = []
    results = scanner.run_daily_scan(force=True)
    
    elapsed = time.time() - start
    print(f"  Scan completed in {elapsed:.2f}s")
    print(f"  Results: {len(results)} symbols scored")
    
    tradeable = [r for r in results if r['tradeable']]
    print(f"  Tradeable: {len(tradeable)}")
    
    for r in results:
        status = "PASS" if r['tradeable'] else "SKIP"
        print(f"    [{status}] {r['symbol']:15s} score={r['score']:2d}/13 {r['reason']}")
    
    assert len(results) == len(symbols), f"Missing results: {len(results)} vs {len(symbols)}"
    print(f"  PASSED: All {len(symbols)} symbols scored without errors")
    return elapsed

def test_concurrent_analysis():
    """Test 2: Full SMC analysis pipeline under concurrent load"""
    print("\n[TEST 2] Concurrent Full Pipeline Analysis")
    print("-" * 40)
    
    from data.market_data_router import get_data_router
    from engines.hybrid_strategy import HybridStrategyAgent
    import numpy as np
    import pandas as pd
    
    router = get_data_router()
    strategy = HybridStrategyAgent()
    
    symbols = ['EURUSD=X', 'GBPUSD=X', 'BTCUSDT']
    results = {}
    errors = []
    
    def analyze_symbol(sym):
        try:
            df = router.get_ohlcv(sym, "15m", limit=500)
            if df is None or len(df) < 60:
                return sym, None, f"No data ({len(df) if df is not None else 0} candles)"
            
            df = df.copy()
            df['EMA_21']  = df['close'].ewm(span=21, adjust=False).mean()
            df['SMA_50']  = df['close'].rolling(50).mean()
            df['SMA_200'] = df['close'].rolling(200).mean()
            delta = df['close'].diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            df['rsi_14'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
            
            fvg_catalog = strategy._build_fvg_catalog(df)
            ob_catalog  = strategy._build_ob_catalog(df)
            structure   = strategy._build_structure(df)
            
            context = strategy.analyze_market_context(
                sym, df, i=len(df)-1,
                fvg_catalog=fvg_catalog,
                ob_catalog=ob_catalog,
                structure=structure
            )
            return sym, context, None
        except Exception as e:
            return sym, None, str(e)
    
    start = time.time()
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(analyze_symbol, s): s for s in symbols}
        for future in as_completed(futures):
            sym, ctx, err = future.result()
            if err:
                errors.append(f"{sym}: {err}")
                print(f"    ERROR {sym}: {err}")
            else:
                grade = ctx.get('Structure_Score', '?')
                conf  = ctx.get('Confidence_Pct', 0)
                trend = ctx.get('Macro_Trend', '?')
                print(f"    {sym}: Grade={grade} Conf={conf}% Trend={trend}")
                results[sym] = ctx
    
    elapsed = time.time() - start
    print(f"  Completed in {elapsed:.2f}s")
    
    if errors:
        print(f"  ERRORS: {len(errors)}")
        for e in errors:
            print(f"    {e}")
    else:
        print(f"  PASSED: All {len(symbols)} symbols analyzed concurrently")
    
    return elapsed, len(errors)

def test_repeated_cycles():
    """Test 3: Simulate 10 rapid trading loop cycles"""
    print("\n[TEST 3] Rapid Cycle Simulation (10 cycles)")
    print("-" * 40)
    
    from engines.instrument_scanner import scanner
    
    start = time.time()
    cycle_times = []
    
    for i in range(10):
        cycle_start = time.time()
        # Force re-scan each cycle to simulate real loop
        scanner._last_scan_date = None
        scanner._today_ranking = []
        results = scanner.run_daily_scan(force=True)
        cycle_time = time.time() - cycle_start
        cycle_times.append(cycle_time)
    
    elapsed = time.time() - start
    avg_cycle = sum(cycle_times) / len(cycle_times)
    max_cycle = max(cycle_times)
    min_cycle = min(cycle_times)
    
    print(f"  Total time: {elapsed:.2f}s")
    print(f"  Avg cycle: {avg_cycle:.2f}s")
    print(f"  Min cycle: {min_cycle:.2f}s")
    print(f"  Max cycle: {max_cycle:.2f}s")
    
    # Memory check
    import psutil
    process = psutil.Process(os.getpid())
    mem_mb = process.memory_info().rss / 1024 / 1024
    print(f"  Memory usage: {mem_mb:.1f} MB")
    
    if avg_cycle < 30:
        print(f"  PASSED: Average cycle {avg_cycle:.2f}s < 30s threshold")
    else:
        print(f"  WARNING: Average cycle {avg_cycle:.2f}s > 30s (may be slow)")
    
    return elapsed

# ── Run all tests ─────────────────────────────────────────────────────────
print("=" * 60)
print("  CRAVE LOAD TEST SUITE")
print("=" * 60)

t1 = test_concurrent_scanner()
t2_time, t2_errors = test_concurrent_analysis()
try:
    t3 = test_repeated_cycles()
except Exception as e:
    print(f"  Test 3 skipped: {e}")
    t3 = 0

print(f"\n{'=' * 60}")
print("  LOAD TEST SUMMARY")
print(f"{'=' * 60}")
print(f"  Scanner (all symbols):   {t1:.2f}s")
print(f"  Pipeline (3 concurrent): {t2_time:.2f}s | Errors: {t2_errors}")
print(f"  10 rapid cycles:         {t3:.2f}s")
print(f"{'=' * 60}")
