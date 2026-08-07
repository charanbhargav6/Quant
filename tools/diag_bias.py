import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv('.env')

from engines.daily_bias_engine import bias_engine
from engines.instrument_scanner import scanner

print('=== RUNNING BIAS ANALYSIS ===')
try:
    results = bias_engine.run_daily_analysis()
    if results:
        for sym, data in results.items():
            b = data.get("bias", "?")
            s = data.get("strength", 0)
            r = data.get("reason", "")
            print(f'  {sym:15s} bias={b:10s} strength={s} reason={r}')
    else:
        print('NO RESULTS from bias engine!')
except Exception as e:
    print(f'Bias engine error: {e}')

print('')
print('=== TRADEABLE TODAY ===')
try:
    tradeable = scanner.get_tradeable_today()
    if tradeable:
        for t in tradeable:
            print(f'  {t.get("symbol","?")}')
    else:
        print('NO tradeable instruments!')
except Exception as e:
    print(f'Scanner error: {e}')
