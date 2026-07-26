#!/usr/bin/env python3
from pathlib import Path
src = Path("platforms/chatgpt/browser_register.py").read_text(encoding="utf-8")
lines = src.splitlines()
out = []
for i, line in enumerate(lines):
    if "class _SentinelTokenGenerator" in line:
        for j in range(i, min(len(lines), i + 120)):
            out.append(f"{j+1}|{lines[j]}")
        break
Path("tools/_browser_sentinel_gen.txt").write_text("\n".join(out), encoding="utf-8")
print("wrote", len(out))
