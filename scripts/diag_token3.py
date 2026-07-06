#!/usr/bin/env python3
"""低并发测试：只测试 June HTML 文件，验证并发是否是问题根因。"""
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CRED_LINE_RE = re.compile(
    r"[\w.-]+@(?:outlook|hotmail)\.com"
    r"----[\w-]+"
    r"----[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    r"----.+",
)

html_files = sorted(Path("scripts/annimail_orders/_html").glob("*.html"))
june_files = [f for f in html_files if f.name.startswith("202606")]

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
})

valid = 0
invalid = 0
total = 0

for hf in june_files:
    html = hf.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator="\n", strip=True)

    cred_lines = []
    seen = set()
    for line in text.split("\n"):
        line = line.strip()
        if CRED_LINE_RE.match(line) and line not in seen:
            seen.add(line)
            cred_lines.append(line)

    if not cred_lines:
        continue

    # 只测前3条（避免触发反滥用）
    for cred in cred_lines[:3]:
        parts = cred.split("----", 3)
        email, password, client_id, refresh_token = parts

        for scope in ["https://graph.microsoft.com/.default", "https://graph.microsoft.com/Mail.Read"]:
            data = {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": scope,
            }
            resp = session.post(
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                data=data, timeout=20,
            )

            total += 1
            if resp.status_code == 200:
                valid += 1
                print(f"  ✓ {email} (scope={scope})")
                break
            else:
                try:
                    err = resp.json()
                    err_desc = str(err.get("error_description", ""))[:100]
                except Exception:
                    err_desc = resp.text[:100]

                if "AADSTS90023" in err_desc:
                    print(f"  → {email}: AADSTS90023, trying next scope...")
                    continue
                else:
                    invalid += 1
                    print(f"  ✗ {email}: {err_desc}")
                    break

        time.sleep(1)  # 1秒间隔，避免反滥用

print(f"\n总计: {total} | 有效: {valid} | 无效: {invalid}")
