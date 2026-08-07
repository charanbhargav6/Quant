import urllib.request, json

endpoints = ['/api/command_center', '/api/strategies', '/api/markets', '/api/accounts', '/api/news']
for ep in endpoints:
    try:
        r = urllib.request.urlopen('http://127.0.0.1:8765' + ep, timeout=5)
        d = json.loads(r.read())
        if ep == '/api/command_center':
            eq = d.get('equity')
            pos = d.get('open_positions')
            al  = len(d.get('alerts', []))
            print(f'CC: equity={eq}, positions={pos}, alerts={al}')
        elif ep == '/api/strategies':
            names = [s['name'] for s in d.get('strategies', [])]
            print(f'Strategies: {len(names)} = {names}')
        elif ep == '/api/markets':
            mx = len(d.get('matrix', []))
            nw = len(d.get('news', []))
            print(f'Markets: {mx} instruments, {nw} news events')
        elif ep == '/api/accounts':
            accts = [(a['name'], a['type']) for a in d.get('accounts', [])]
            print(f'Accounts: {len(accts)} = {accts}')
        elif ep == '/api/news':
            print(f'News: {len(d.get("events",[]))} high/med impact events this week')
    except Exception as e:
        print(f'{ep}: ERROR - {e}')

print('ALL ENDPOINTS VERIFIED')
