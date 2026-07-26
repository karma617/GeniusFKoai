#!/usr/bin/env python3
"""Dump key header/sentinel sections from register.py for offline comparison."""
from __future__ import annotations

from pathlib import Path

SRC = Path("platforms/chatgpt/register.py")
OUT = Path("tools/_register_headers_dump.txt")


def main() -> None:
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines()
    ranges = [
        (1, 120),
        (240, 460),
        (600, 700),
        (980, 1220),
        (1280, 1550),
        (1550, 1850),
        (1850, 2200),
        (2200, 2550),
        (2550, 2900),
        (2900, 3300),
    ]
    out: list[str] = []
    out.append(f"total_lines={len(lines)} size={SRC.stat().st_size}")
    for start, end in ranges:
        out.append(f"\n===== {start}-{end} =====")
        for i in range(start - 1, min(end, len(lines))):
            out.append(f"{i + 1}|{lines[i]}")
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT} lines={len(out)}")


if __name__ == "__main__":
    main()
