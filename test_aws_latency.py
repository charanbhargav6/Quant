import time
from datetime import datetime
import logging
logging.basicConfig(level=logging.INFO)

# Simulate what sleep watcher does
from infra.aws_manager import get_aws
aws = get_aws()

t0 = time.time()
print(f"[{datetime.now()}] 1. Getting instance ID (Pre-warm)...")
aws._get_instance_id()
t1 = time.time()
print(f"[{datetime.now()}] Pre-warm took {t1 - t0:.3f}s")

t2 = time.time()
print(f"[{datetime.now()}] 2. Firing start_instance(wait=False) like on lid close...")
aws.start_instance(wait=False)
t3 = time.time()
print(f"[{datetime.now()}] Start command took {t3 - t2:.3f}s")
