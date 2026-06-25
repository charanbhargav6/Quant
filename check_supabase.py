import os
import requests
from dotenv import load_dotenv

load_dotenv()
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

headers = {
    "apikey": supabase_key,
    "Authorization": f"Bearer {supabase_key}"
}

print("--- SYSTEM STATUS ---")
resp = requests.get(f"{supabase_url}/rest/v1/crave_system_status?select=*", headers=headers).json()
for r in resp:
    print(r)

print("\n--- RECENT TRADES (Last 5) ---")
resp2 = requests.get(f"{supabase_url}/rest/v1/crave_trades?select=*&order=close_time.desc&limit=5", headers=headers).json()
for r in resp2:
    print(r['trade_id'], r['symbol'], r['direction'], r['r_multiple'], r.get('node', 'unknown'))

print("\n--- OPEN POSITIONS ---")
resp3 = requests.get(f"{supabase_url}/rest/v1/crave_open_positions?select=*", headers=headers).json()
for r in resp3:
    print(r['trade_id'], r['symbol'], r['direction'], r['unrealised_r'], r.get('node', 'unknown'))
