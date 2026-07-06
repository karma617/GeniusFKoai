#!/usr/bin/env python3
"""诊断脚本：从 _html 提取一条凭证，测试 token 刷新，打印完整响应。"""
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
print(f"HTML files: {len(html_files)}")
if not html_files:
    sys.exit(1)

html = html_files[0].read_text(encoding="utf-8")
soup = BeautifulSoup(html, "lxml")
text = soup.get_text(separator="\n", strip=True)

cred_lines = []
for line in text.split("\n"):
    line = line.strip()
    if CRED_LINE_RE.match(line):
        cred_lines.append(line)

if not cred_lines:
    print("No credential lines found!")
    sys.exit(1)

print(f"File: {html_files[0].name}")
print(f"Cred lines: {len(cred_lines)}")

parts = cred_lines[0].split("----", 3)
email, password, client_id, refresh_token = parts
print(f"Email: {email}")
print(f"ClientID: {client_id}")
print(f"RefreshToken (first 60): {refresh_token[:60]}...")
print(f"RefreshToken length: {len(refresh_token)}")

# Test 1: login.microsoftonline.com with .default scope
print("\n=== Test 1: login.microsoftonline.com + .default scope ===")
url1 = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
data1 = {
    "client_id": client_id,
    "grant_type": "refresh_token",
    "refresh_token": refresh_token,
    "scope": "https://graph.microsoft.com/.default",
}
try:
    resp = requests.post(url1, data=data1, timeout=15)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:500]}")
except Exception as e:
    print(f"Error: {e}")

# Test 2: login.live.com (old endpoint, no scope)
print("\n=== Test 2: login.live.com (old endpoint) ===")
url2 = "https://login.live.com/oauth20_token.srf"
data2 = {
    "client_id": client_id,
    "grant_type": "refresh_token",
    "refresh_token": refresh_token,
}
try:
    resp = requests.post(url2, data=data2, timeout=15)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:500]}")
except Exception as e:
    print(f"Error: {e}")

# Test 3: login.live.com with scope
print("\n=== Test 3: login.live.com + scope ===")
data3 = {
    "client_id": client_id,
    "grant_type": "refresh_token",
    "refresh_token": refresh_token,
    "scope": "https://graph.microsoft.com/.default",
}
try:
    resp = requests.post(url2, data=data3, timeout=15)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:500]}")
except Exception as e:
    print(f"Error: {e}")
