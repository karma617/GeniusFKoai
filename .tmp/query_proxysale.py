# -*- coding: utf-8 -*-
import sqlite3, json

DB = r'D:\work\ai\GeniusFKoai\account_manager.db'
OUT = r'D:\work\ai\GeniusFKoai\.tmp\proxysale_results.txt'

lines = []
conn = sqlite3.connect(DB)
conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
cur = conn.cursor()

# search task_events for proxysale / gate-rotate / the exact session id
for kw in ['proxysale', 'gate-rotate', 'sp-g2fb5btkhmbo', 'QPyROIexEg', 'area-JP', 'rotat']:
    cur.execute("SELECT COUNT(*) FROM task_events WHERE message LIKE ? OR detail_json LIKE ?", (f'%{kw}%', f'%{kw}%'))
    n = cur.fetchone()[0]
    lines.append(f"count(message/detail LIKE %{kw}%) = {n}")
    if n > 0 and n <= 30:
        cur.execute("SELECT id, task_id, created_at, message FROM task_events WHERE message LIKE ? OR detail_json LIKE ? ORDER BY id LIMIT 30", (f'%{kw}%', f'%{kw}%'))
        for r in cur.fetchall():
            m = r[3] if len(r[3]) <= 400 else r[3][:400] + '...'
            lines.append(f"   [{r[0]}] {r[2]} | {r[1]} | {m}")

# proxies table
cur.execute("SELECT * FROM proxies")
lines.append("\nPROXIES table:")
for r in cur.fetchall():
    lines.append(json.dumps(r, ensure_ascii=False, default=str))

# configs table (may hold proxy config)
cur.execute("SELECT * FROM configs")
lines.append("\nCONFIGS:")
for r in cur.fetchall():
    v = r[1]
    if len(str(v)) > 500: v = str(v)[:500] + '...'
    lines.append(json.dumps([r[0], v], ensure_ascii=False))

conn.close()
with open(OUT, 'w', encoding='utf-8') as f:
    f.write("\n".join(lines))
print("DONE")
