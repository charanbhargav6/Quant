import sys
import logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
sys.path.insert(0, "/home/ubuntu/engine")

# Force it to think it's a real run
from engines.instrument_scanner import scanner
print("Testing AXISBANK scanner...")
res = scanner._score_instrument("AXISBANK")
print(res)
