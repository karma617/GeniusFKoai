# -*- coding: utf-8 -*-
import sqlite3, json

DB = r'D:\work\ai\GeniusFKoai\account_manager.db'
OUT = r'D:\work\ai\GeniusFKoai\.tmp\geo_results.txt'

lines = []
conn = sqlite3.connect(DB)
conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
cur = conn.cursor()

# 1. Any events mentioning the IPv6 egress or city-level keywords
for kw in ['240f:e1:38e8', '东京', '大阪', 'city', '城市', 'geo', 'region']:
    cur.execute("SELECT COUNT(*) FROM task_events WHERE message LIKE ? OR detail_json LIKE ?", (f'%{kw}%', f'%{kw}%'))
    n = cur.fetchone()[0]
    lines.append(f"count(message/detail LIKE %{kw}%) = {n}")
    if n > 0 and n <= 20:
        cur.execute("SELECT id, task_id, created_at, message FROM task_events WHERE message LIKE ? OR detail_json LIKE ? ORDER BY id LIMIT 20", (f'%{kw}%', f'%{kw}%'))
        for r in cur.fetchall():
            m = r[3] if len(r[3]) <= 300 else r[3][:300] + '...'
            lines.append(f"   [{r[0]}] {r[2]} | {r[1]} | {m}")

# 2. Registration task payload (proxy region preference)
cur.execute("SELECT payload_json FROM tasks WHERE id='task_1785605620735_07453e'")
p = cur.fetchone()[0]
lines.append("\nREG TASK payload_json:")
lines.append(p)

# 3. accounts row for 3228 full
cur.execute("SELECT * FROM accounts WHERE id=3228")
lines.append("\nACCOUNT 3228 row:")
lines.append(json.dumps(cur.fetchone(), ensure_ascii=False, default=str))

# 4. provider_accounts / account_credentials metadata for 3228 (may hold proxy/region info)
cur.execute("SELECT id, provider_type, login_identifier, metadata_json FROM provider_accounts WHERE account_id=3228")
lines.append("\nPROVIDER_ACCOUNTS 3228:")
for r in cur.fetchall():
    lines.append(json.dumps(r, ensure_ascii=False, default=str))

cur.execute("SELECT id, scope, credential_type, key, source, metadata_json, created_at FROM account_credentials WHERE account_id=3228")
lines.append("\nACCOUNT_CREDENTIALS 3228:")
for r in cur.fetchall():
    lines.append(json.dumps(r, ensure_ascii=False, default=str))

conn.close()
with open(OUT, 'w', encoding='utf-8') as f:
    f.write("\n".join(lines))
print("DONE")
