#!/usr/bin/env python3
"""Split validated annimail credentials into outlook and hotmail files.

Usage:
    python scripts/annimail_split_valid_by_domain.py
    python scripts/annimail_split_valid_by_domain.py path/to/validated_valid.txt
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


EMAIL_RE = re.compile(r"^([\w.-]+@(outlook|hotmail)\.com)----", re.IGNORECASE)
DEFAULT_INPUT = Path(__file__).resolve().parent / "annimail_orders" / "validated_valid.txt"


def split_valid_file(input_path: Path) -> tuple[Path, Path, int, int, int]:
    outlook_lines: list[str] = []
    hotmail_lines: list[str] = []
    skipped = 0

    with input_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            match = EMAIL_RE.match(line)
            if not match:
                skipped += 1
                continue

            domain = match.group(2).lower()
            if domain == "outlook":
                outlook_lines.append(line)
            else:
                hotmail_lines.append(line)

    outlook_path = input_path.with_name(f"{input_path.stem}_outlook{input_path.suffix}")
    hotmail_path = input_path.with_name(f"{input_path.stem}_hotmail{input_path.suffix}")

    outlook_path.write_text("\n".join(outlook_lines) + ("\n" if outlook_lines else ""), encoding="utf-8")
    hotmail_path.write_text("\n".join(hotmail_lines) + ("\n" if hotmail_lines else ""), encoding="utf-8")

    return outlook_path, hotmail_path, len(outlook_lines), len(hotmail_lines), skipped


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    input_path = Path(argv[0]) if argv else DEFAULT_INPUT
    if not input_path.exists():
        print(f"输入文件不存在: {input_path}")
        return 1

    outlook_path, hotmail_path, outlook_count, hotmail_count, skipped = split_valid_file(input_path)

    print(f"outlook: {outlook_count} -> {outlook_path}")
    print(f"hotmail: {hotmail_count} -> {hotmail_path}")
    if skipped:
        print(f"skipped: {skipped}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
