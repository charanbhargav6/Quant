from collections import Counter, defaultdict
from pathlib import Path
import re

LOG_DIR = Path('/home/ubuntu/upload')
paths = [LOG_DIR / f'engine.log{suffix}' for suffix in ['', '.1', '.2', '.3']]
patterns = {
    'mt5_connected': re.compile(r'MT5\] Connected'),
    'mt5_failed': re.compile(r'MT5\].*(?:failed|Failed|error|Error|disconnected|Disconnected)', re.I),
    'mt5_plaintext_password': re.compile(r'MT5_PASSWORD did not decrypt'),
    'data_fetch': re.compile(r'DataAgent\].*Fetching'),
    'data_error': re.compile(r'DataAgent\].*(?:error|failed|Error|Failed)', re.I),
    'insufficient_data': re.compile(r'Insufficient data'),
    'council_convene': re.compile(r'CONVENING DEBATE'),
    'council_fail': re.compile(r'ALL LLMs failed'),
    'council_reject': re.compile(r'COUNCIL REJECTED'),
    'council_approve': re.compile(r'COUNCIL APPROVED'),
    'signal': re.compile(r'\[TradingLoop\] SIGNAL:'),
    'paper_fill': re.compile(r'paper_filled|Paper.*filled', re.I),
    'live_fill': re.compile(r'filled|order.*executed|EXECUTED', re.I),
    'order_error': re.compile(r'(?:order|execution|broker).*(?:error|failed|rejected|invalid)', re.I),
    'telegram_error': re.compile(r'Telegram.*(?:error|failed|timed out)', re.I),
    'news_refresh': re.compile(r'NewsSentinel\].*Refreshed'),
    'strategy_evolver': re.compile(r'Evolver\]'),
    'jarvis': re.compile(r'Jarvis'),
    'weekend': re.compile(r'Markets closed \(weekend\)'),
}

counts = Counter()
by_file = defaultdict(Counter)
first_last = {}
account_lines = []
critical = []
strategy_lines = Counter()
asset_lines = Counter()

for path in paths:
    if not path.exists():
        continue
    first = last = None
    with path.open('r', encoding='utf-8', errors='replace') as f:
        for line_no, line in enumerate(f, 1):
            ts = line[:23]
            if re.match(r'^20\d\d-\d\d-\d\d', ts):
                first = first or ts
                last = ts
            for name, pattern in patterns.items():
                if pattern.search(line):
                    counts[name] += 1
                    by_file[path.name][name] += 1
            if any(token in line for token in ['Connected ✅', 'Connection failed', 'Account:', 'Balance:', 'Server:', 'did not decrypt', 'MT5 Agent']):
                account_lines.append((path.name, line_no, line.rstrip()))
            if any(token in line for token in ['ERROR', 'CRITICAL', 'failed', 'Failed', 'rejected', 'Insufficient data']):
                critical.append((path.name, line_no, line.rstrip()))
            m = re.search(r'Analysing\s+([^ ]+)', line)
            if m:
                asset_lines[m.group(1)] += 1
            m = re.search(r'(?:SIGNAL:|No SMC setup found|Insufficient data for|Fetching \d+ x \w+ for)\s+([^ ]+)', line)
            if m:
                strategy_lines[m.group(1)] += 1
    first_last[path.name] = (first, last)

print('=== LOG COVERAGE ===')
for name, window in sorted(first_last.items()):
    print(name, window)
print('\n=== COUNTS ===')
for key, value in counts.most_common():
    print(f'{key}: {value}')
print('\n=== BY FILE ===')
for name, counter in sorted(by_file.items()):
    print(name, dict(counter))
print('\n=== ACCOUNT / BROKER LINES (redacted selection) ===')
for item in account_lines[-80:]:
    print(*item, sep=' | ')
print('\n=== ASSET ANALYSIS COUNTS ===')
for key, value in asset_lines.most_common():
    print(key, value)
print('\n=== CRITICAL SAMPLE (last 120) ===')
for item in critical[-120:]:
    print(*item, sep=' | ')
