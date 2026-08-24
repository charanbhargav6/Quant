from collections import Counter
from pathlib import Path
import re

paths = sorted(Path('/home/ubuntu/upload').glob('engine.log*'))
counts = Counter()
examples = {}
for path in paths:
    with path.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if not re.search(r'\[(ERROR|CRITICAL)\]', line):
                continue
            msg = re.sub(r'^.*?\] (?:crave\.[^:]+: )?', '', line.strip())
            msg = re.sub(r'\d+', '<N>', msg)
            msg = re.sub(r'0x[0-9a-fA-F]+', '<HEX>', msg)
            msg = re.sub(r'\b[A-Fa-f0-9]{32,}\b', '<TOKEN>', msg)
            msg = re.sub(r'\s+', ' ', msg)
            counts[msg] += 1
            examples.setdefault(msg, line.strip())
print('rank,count,normalized_message,example')
for rank, (msg, count) in enumerate(counts.most_common(100), 1):
    print(f'{rank},{count},{msg},{examples[msg]}')
