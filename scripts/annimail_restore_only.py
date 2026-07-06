#!/usr/bin/env python3
"""从 _html 目录恢复全部凭证到 txt 文件，不做任何验证或删除。

仅提取 + 写入，恢复 99 个原始凭证文件。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

CRED_LINE_RE = re.compile(
    r"[\w.-]+@(?:outlook|hotmail)\.com"
    r"----[\w-]+"
    r"----[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    r"----.+",
)

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    html_dir = Path("scripts/annimail_orders/_html")
    out_dir = Path("scripts/annimail_orders")
    out_dir.mkdir(parents=True, exist_ok=True)

    html_files = sorted(html_dir.glob("*.html"))
    print(f"HTML files: {len(html_files)}")

    files_written = 0
    total_creds = 0

    for hf in html_files:
        html = hf.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(separator="\n", strip=True)

        seen: set[str] = set()
        cred_lines: list[str] = []
        for line in text.split("\n"):
            line = line.strip()
            if CRED_LINE_RE.match(line) and line not in seen:
                seen.add(line)
                cred_lines.append(line)

        if not cred_lines:
            continue

        # Determine domain
        first_email = cred_lines[0].split("----")[0].lower()
        domain = "hotmail" if "@hotmail.com" in first_email else "outlook"

        # Build filename from HTML stem
        stem = hf.stem
        if len(stem) >= 14:
            dt_str = f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}_{stem[8:10]}-{stem[10:12]}-{stem[12:14]}"
        else:
            dt_str = stem

        out_name = f"{dt_str}-{domain}.txt"
        out_path = out_dir / out_name
        out_path.write_text("\n".join(cred_lines) + "\n", encoding="utf-8")
        files_written += 1
        total_creds += len(cred_lines)
        print(f"  ✓ {out_name} ({len(cred_lines)} creds)")

    print(f"\n恢复完成: {files_written} 个文件, {total_creds} 条凭证")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
