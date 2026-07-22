import sqlite3, json, base64, time
conn = sqlite3.connect('account_manager.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT account_id, value FROM account_credentials WHERE key='access_token' ORDER BY account_id DESC")
now = time.time()
found = 0
for r in cur.fetchall():
    token = r['value']
    parts = token.split('.')
    if len(parts) < 2: continue
    try:
        p = parts[1] + '=' * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p))
        if payload.get('exp',0) < now: continue
        auth = payload.get('https://api.openai.com/auth', {})
        trial = auth.get('one_click_trial_eligible', 'MISSING')
        plan = auth.get('chatgpt_plan_type', '')
        if trial != 'MISSING':
            print('aid=' + str(r['account_id']) + ' trial=' + str(trial) + ' plan=' + str(plan))
            found += 1
    except: pass
conn.close()
print('found ' + str(found) + ' accounts with trial field')
