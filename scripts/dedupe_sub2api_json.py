#!/usr/bin/env python3
"""Deduplicate SUB2API export JSON accounts.

Duplicate key: accounts[].name + accounts[].credentials.chatgpt_account_id.
The script keeps the first account for each key and writes a new JSON file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _account_key(account: Any) -> tuple[str, str] | None:
    if not isinstance(account, dict):
        return None
    credentials = account.get("credentials")
    if not isinstance(credentials, dict):
        credentials = {}
    name = _text(account.get("name"))
    chatgpt_account_id = _text(credentials.get("chatgpt_account_id"))
    if not name or not chatgpt_account_id:
        return None
    return name, chatgpt_account_id


def dedupe_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("input JSON must contain an accounts list")

    seen: set[tuple[str, str]] = set()
    deduped_accounts: list[Any] = []
    duplicate_accounts: list[dict[str, Any]] = []
    missing_key_count = 0

    for index, account in enumerate(accounts):
        key = _account_key(account)
        if key is None:
            missing_key_count += 1
            deduped_accounts.append(account)
            continue
        if key in seen:
            duplicate_accounts.append(
                {
                    "index": index,
                    "name": key[0],
                    "chatgpt_account_id": key[1],
                }
            )
            continue
        seen.add(key)
        deduped_accounts.append(account)

    result = dict(payload)
    result["accounts"] = deduped_accounts
    summary = {
        "input_accounts": len(accounts),
        "output_accounts": len(deduped_accounts),
        "removed_duplicates": len(duplicate_accounts),
        "missing_key_kept": missing_key_count,
        "duplicates": duplicate_accounts,
    }
    return result, summary


def _default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}.deduped.json")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Remove duplicate accounts from a SUB2API JSON export.",
    )
    parser.add_argument("input", help="Path to the source SUB2API JSON file")
    parser.add_argument(
        "-o",
        "--output",
        help="Path to write the deduplicated JSON file. Defaults to <input>.deduped.json",
    )
    parser.add_argument(
        "--summary",
        help="Optional path to write a duplicate summary JSON file",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve() if args.output else _default_output_path(input_path)
    summary_path = Path(args.summary).expanduser().resolve() if args.summary else None

    with input_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("input JSON root must be an object")

    deduped_payload, summary = dedupe_payload(payload)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(deduped_payload, file, ensure_ascii=False, indent=2)
        file.write("\n")

    if summary_path:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(summary, file, ensure_ascii=False, indent=2)
            file.write("\n")

    print(
        f"已检查完毕，共 {summary['input_accounts']} 个帐号，"
        f"发现 {summary['removed_duplicates']} 个重复账号，已处理"
    )
    print(f"output={output_path}")
    if summary_path:
        print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
