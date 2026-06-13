import sys
import logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
sys.path.insert(0, "/home/ubuntu/engine")
from engines.instrument_scanner import scanner
print("Running full daily scan...")
res = scanner.run_daily_scan(force=True)
print(f"Results length: {len(res)}")
