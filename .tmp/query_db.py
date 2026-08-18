# -*- coding: utf-8 -*-
import sqlite3, json, sys, io

DB = r'D:\work\ai\GeniusFKoai\account_manager.db'
OUT = r'D:\work\ai\GeniusFKoai\.tmp\db_schema.txt'

lines = []
conn = sqlite3.connect(DB)
conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
lines.append("TABLES: " + json.dumps(tables, ensure_ascii=False))

for t in tables:
    try:
        cur.execute(f'PRAGMA table_info("{t}")')
        cols = [(r[1], r[2]) for r in cur.fetchall()]
        lines.append(f"\n== TABLE {t} ==")
        for c in cols:
            lines.append("   " + repr(c))
        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
        lines.append("    rowcount: " + str(cur.fetchone()[0]))
    except Exception as e:
        lines.append(f"  error on {t}: {e}")
conn.close()

with open(OUT, 'w', encoding='utf-8') as f:
    f.write("\n".join(lines))
print("DONE")
