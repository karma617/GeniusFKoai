#!/usr/bin/env python3
from pathlib import Path

SRC = Path("platforms/chatgpt/register.py")
OUT = Path("tools/_register_headers_dump2.txt")
text = SRC.read_text(encoding="utf-8")
lines = text.splitlines()
ranges = [
    (1550, 1750),
    (1850, 2100),
    (2170, 2450),
    (2800, 3200),
    (4200, 4300),
    (4850, 5150),
    (5600, 6000),
]
out = [f"total={len(lines)}"]
for a, b in ranges:
    out.append(f"\n===== {a}-{b} =====")
    for i in range(a - 1, min(b, len(lines))):
        out.append(f"{i+1}|{lines[i]}")
OUT.write_text("\n".join(out), encoding="utf-8")
print("wrote", OUT, "lines", len(out))
