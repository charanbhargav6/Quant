import sys, os
sys.path.insert(0, 'D:/Desktop/engine')
os.chdir('D:/Desktop/engine')
from dotenv import load_dotenv
load_dotenv('.env')
from config.config import INSTRUMENTS, RISK, PROP_FIRM

print('=== INSTRUMENTS (all) ===')
for k, v in INSTRUMENTS.items():
    ac = v.get('asset_class', '?')
    ex = v.get('exchange', '?')
    label = v.get('label', k)
    print(f'  {k}: {ac} | {ex} | {label}')
print(f'Total: {len(INSTRUMENTS)}')
print()
print('=== RISK CONFIG ===')
for k, v in RISK.items():
    print(f'  {k}: {v}')
print()
print('PROP_FIRM:', PROP_FIRM)

# Check DB stats
from core.database_manager import get_db
db = get_db()
rows = db.query("SELECT COUNT(*) as n FROM trades", ())
print('Total trades in DB:', rows[0]['n'] if rows else 0)
rows2 = db.query("SELECT is_paper, COUNT(*) as n FROM trades GROUP BY is_paper", ())
for r in rows2:
    print(f"  is_paper={r['is_paper']}: {r['n']} trades")

rows3 = db.query("SELECT COUNT(*) as n FROM signals", ())
print('Total signals in DB:', rows3[0]['n'] if rows3 else 0)
