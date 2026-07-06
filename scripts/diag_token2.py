#!/usr/bin/env python3
"""诊断脚本2：测试不同日期HTML文件中的凭证。"""
import re
import sys
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
print(f"Total HTML files: {len(html_files)}")

# 找6月的文件
june_files = [f for f in html_files if f.name.startswith("202606")]
print(f"June HTML files: {len(june_files)}")
for f in june_files:
    print(f"  {f.name}")

if not june_files:
    print("No June files found, testing last 3 files")
    june_files = html_files[-3:]

# 测试每个June文件的第一条凭证
for html_file in june_files:
    html = html_file.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator="\n", strip=True)

    cred_lines = []
    for line in text.split("\n"):
        line = line.strip()
        if CRED_LINE_RE.match(line):
            cred_lines.append(line)

    if not cred_lines:
        print(f"\n{html_file.name}: No cred lines found")
        continue

    parts = cred_lines[0].split("----", 3)
    email, password, client_id, refresh_token = parts

    url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "https://graph.microsoft.com/.default",
    }

    try:
        resp = requests.post(url, data=data, timeout=15)
        status = "✓ VALID" if resp.status_code == 200 else f"✗ {resp.status_code}"
        print(f"\n{html_file.name}")
        print(f"  Email: {email}")
        print(f"  Status: {status}")
        if resp.status_code != 200:
            try:
                err = resp.json()
                print(f"  Error: {err.get('error_description', '')[:120]}")
            except Exception:
                print(f"  Response: {resp.text[:200]}")
    except Exception as e:
        print(f"\n{html_file.name}: Error - {e}")
