# -*- coding: utf-8 -*-
import sqlite3, json

DB = r'D:\work\ai\GeniusFKoai\account_manager.db'
OUT = r'D:\work\ai\GeniusFKoai\.tmp\task4_events.txt'
TASK_ID = 'task_1785605620735_07453e'

lines = []
conn = sqlite3.connect(DB)
conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
cur = conn.cursor()

# All events for subtask task_4 (账号 #4) in reg window
cur.execute("""SELECT id, task_id, type, level, message, detail_json, created_at FROM task_events
               WHERE task_id=? AND created_at BETWEEN '2026-08-01 17:39:00' AND '2026-08-01 17:44:00'
               ORDER BY id""", (TASK_ID,))
evs = cur.fetchall()
lines.append(f"EVENTS for {TASK_ID} 17:39-17:44 ({len(evs)}):")
for e in evs:
    msg = e[4]
    if len(msg) > 500:
        msg = msg[:500] + '...[truncated]'
    d = e[5]
    if len(d) > 300:
        d = d[:300] + '...[truncated]'
    lines.append(f"[{e[0]}] {e[6]} | {e[2]}/{e[3]} | {msg} | {d}")

# Any events anywhere mentioning session-Ft3ZaR (the proxy session id)
cur.execute("""SELECT id, task_id, type, level, message, detail_json, created_at FROM task_events
               WHERE message LIKE '%Ft3ZaR%' OR detail_json LIKE '%Ft3ZaR%' ORDER BY id LIMIT 50""")
evs2 = cur.fetchall()
lines.append(f"\nEVENTS mentioning proxy session Ft3ZaR ({len(evs2)}):")
for e in evs2:
    lines.append(f"[{e[0]}] {e[6]} | {e[1]} | {e[4]}")

conn.close()
with open(OUT, 'w', encoding='utf-8') as f:
    f.write("\n".join(lines))
print("DONE", len(evs), "events,", len(evs2), "Ft3ZaR events")
