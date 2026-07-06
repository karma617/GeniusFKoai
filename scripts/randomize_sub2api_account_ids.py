#!/usr/bin/env python3
"""Randomize exact "account_id" JSON string values without reformatting the file."""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from pathlib import Path


ACCOUNT_ID_PATTERN = re.compile(rb'("account_id"\s*:\s*")((?:\\.|[^"\\])*)(")')


def randomize_account_ids(content: bytes) -> tuple[bytes, int]:
    count = 0

    def replace(match: re.Match[bytes]) -> bytes:
        nonlocal count
        count += 1
        return match.group(1) + str(uuid.uuid4()).encode("ascii") + match.group(3)

    return ACCOUNT_ID_PATTERN.sub(replace, content), count


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description='Replace every exact JSON key "account_id" string value with a random UUID.',
    )
    parser.add_argument("input", help="Path to the SUB2API JSON file")
    parser.add_argument(
        "-o",
        "--output",
        help="Optional output path. Defaults to overwriting the input file in place.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count matched account_id fields; do not write any file.",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve() if args.output else input_path

    content = input_path.read_bytes()
    randomized, count = randomize_account_ids(content)
    if count == 0:
        print("未找到可替换的 account_id 字段")
        return 1

    if args.dry_run:
        print(f"dry-run: 将替换 {count} 个 account_id 字段")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(randomized)
    print(f"已替换 {count} 个 account_id 字段")
    print(f"output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
