# -*- coding: utf-8 -*-
import sqlite3, json, sys, io

DB = r'D:\work\ai\GeniusFKoai\account_manager.db'
OUT = r'D:\work\ai\GeniusFKoai\.tmp\email_results.txt'
EMAIL = 'velds.socials_7p@icloud.com'

lines = []
conn = sqlite3.connect(DB)
conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
cur = conn.cursor()

# 1. accounts
cur.execute("SELECT id, platform, email, password, user_id, created_at, updated_at FROM accounts WHERE email=?", (EMAIL,))
for r in cur.fetchall():
    lines.append("ACCOUNT: " + json.dumps(r, ensure_ascii=False, default=str))

# 2. account_overviews
cur.execute("SELECT account_id, lifecycle_status, validity_status, plan_state, plan_name, display_status, remote_email, checked_at, summary_json, created_at, updated_at FROM account_overviews WHERE account_id IN (SELECT id FROM accounts WHERE email=?)", (EMAIL,))
for r in cur.fetchall():
    lines.append("OVERVIEW: " + json.dumps(r, ensure_ascii=False, default=str))

# 3. tasks mentioning email in payload
cur.execute("SELECT id, type, platform, status, payload_json, result_json, error, started_at, finished_at, created_at, updated_at FROM tasks WHERE payload_json LIKE ? OR result_json LIKE ?", (f'%{EMAIL}%', f'%{EMAIL}%'))
tasks = cur.fetchall()
lines.append(f"\nTASKS matching email: {len(tasks)}")
for r in tasks:
    lines.append("TASK: " + json.dumps(r, ensure_ascii=False, default=str))

# 4. task_events mentioning email
cur.execute("SELECT id, task_id, type, level, message, detail_json, created_at FROM task_events WHERE message LIKE ? OR detail_json LIKE ?", (f'%{EMAIL}%', f'%{EMAIL}%'))
evs = cur.fetchall()
lines.append(f"\nTASK_EVENTS matching email: {len(evs)}")
for r in evs[:200]:
    lines.append("EVENT: " + json.dumps(r, ensure_ascii=False, default=str))

# 5. proxies
cur.execute("SELECT id, url, region, success_count, fail_count, is_active, last_checked FROM proxies")
lines.append("\nPROXIES:")
for r in cur.fetchall():
    lines.append("PROXY: " + json.dumps(r, ensure_ascii=False, default=str))

conn.close()

with open(OUT, 'w', encoding='utf-8') as f:
    f.write("\n".join(lines))
print("DONE", len(tasks), "tasks,", len(evs), "events")
