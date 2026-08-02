import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect('Database/crave.db')
cur = conn.cursor()

print('=== LAST 15 SIGNALS ===')
cur.execute('SELECT signal_time, symbol, direction, confidence, grade, was_traded, skip_reason FROM signals ORDER BY id DESC LIMIT 15')
for r in cur.fetchall():
    skip = r[6] or "OK"
    print(f'{r[0][:19]} | {r[1]:12s} | {r[2]:4s} | conf={r[3]:3d} | {r[4]:8s} | traded={r[5]} | {skip}')

print('')
print('=== LAST 5 TRADES ===')
cur.execute('SELECT open_time, symbol, direction, entry_price, exit_price, r_multiple, outcome FROM trades ORDER BY id DESC LIMIT 5')
for r in cur.fetchall():
    print(f'{str(r[0])[:19]} | {r[1]:12s} | {r[2]:4s} | entry={r[3]} | exit={r[4]} | R={r[5]} | {r[6]}')

print('')
print('=== SKIP REASON BREAKDOWN ===')
cur.execute('SELECT skip_reason, COUNT(*) as cnt FROM signals WHERE skip_reason IS NOT NULL GROUP BY skip_reason ORDER BY cnt DESC')
for r in cur.fetchall():
    print(f'  {r[1]:4d}x  {r[0]}')

conn.close()
