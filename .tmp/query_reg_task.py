# -*- coding: utf-8 -*-
import sqlite3, json, sys

DB = r'D:\work\ai\GeniusFKoai\account_manager.db'
OUT = r'D:\work\ai\GeniusFKoai\.tmp\reg_task_results.txt'
TASK_ID = 'task_1785605620735_07453e'

lines = []
conn = sqlite3.connect(DB)
conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
cur = conn.cursor()

# 1. Full task row
cur.execute("SELECT id, type, platform, status, payload_json, result_json, progress_current, progress_total, success_count, error_count, error, started_at, finished_at, created_at, updated_at FROM tasks WHERE id=?", (TASK_ID,))
r = cur.fetchone()
lines.append("TASK ROW:")
lines.append(json.dumps(r, ensure_ascii=False, default=str))
lines.append("")

# 2. All task_events for this task
cur.execute("SELECT id, task_id, type, level, message, detail_json, created_at FROM task_events WHERE task_id=? ORDER BY id", (TASK_ID,))
evs = cur.fetchall()
lines.append(f"TASK EVENTS ({len(evs)}):")
for e in evs:
    lines.append("EVENT: " + json.dumps(e, ensure_ascii=False, default=str))
lines.append("")

# 3. Any task_events in registration time window mentioning proxy keywords
cur.execute("""SELECT id, task_id, type, level, message, detail_json, created_at FROM task_events
               WHERE created_at BETWEEN '2026-08-01 17:30:00' AND '2026-08-01 18:00:00'
                 AND (message LIKE '%proxy%' OR message LIKE '%代理%' OR detail_json LIKE '%proxy%' OR detail_json LIKE '%代理%')
               ORDER BY id LIMIT 200""")
evs2 = cur.fetchall()
lines.append(f"PROXY EVENTS in reg window ({len(evs2)}):")
for e in evs2:
    lines.append("EVENT: " + json.dumps(e, ensure_ascii=False, default=str))
lines.append("")

# 4. task_logs for this email
cur.execute("SELECT id, platform, email, status, error, detail_json, created_at FROM task_logs WHERE email=? ORDER BY id", ('velds.socials_7p@icloud.com',))
logs = cur.fetchall()
lines.append(f"TASK_LOGS for email ({len(logs)}):")
for l in logs:
    lines.append("LOG: " + json.dumps(l, ensure_ascii=False, default=str))

conn.close()
with open(OUT, 'w', encoding='utf-8') as f:
    f.write("\n".join(lines))
print("DONE", len(evs), "events,", len(evs2), "proxy events,", len(logs), "logs")
