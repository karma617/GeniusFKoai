#!/usr/bin/env python3
from pathlib import Path

src = Path("platforms/chatgpt/register.py").read_text(encoding="utf-8")
lines = src.splitlines()
# find class _SentinelTokenGenerator and _check_sentinel
starts = []
for i, line in enumerate(lines):
    if "class _SentinelTokenGenerator" in line or "def _check_sentinel" in line or "def _quickjs_sentinel_payload" in line or "def _sentinel_payload_header" in line:
        starts.append(i)
out = []
for s in starts:
    e = min(len(lines), s + 220)
    out.append(f"\n===== start L{s+1} =====")
    for i in range(s, e):
        out.append(f"{i+1}|{lines[i]}")
Path("tools/_sentinel_core_dump.txt").write_text("\n".join(out), encoding="utf-8")
print("starts", [s+1 for s in starts], "wrote", len(out))

# also dump snip generator
snip = Path("tools/_register_snip.py").read_text(encoding="utf-8")
sl = snip.splitlines()
out2 = []
for i, line in enumerate(sl):
    if "class _SentinelTokenGenerator" in line or "def generate_requirements" in line or "def generate_token" in line or "def _check_sentinel" in line:
        out2.append(f"\n===== snip L{i+1} =====")
        for j in range(i, min(len(sl), i+80)):
            out2.append(f"{j+1}|{sl[j]}")
Path("tools/_snip_sentinel_dump.txt").write_text("\n".join(out2), encoding="utf-8")
print("snip wrote", len(out2))
