#!/usr/bin/env python3
"""Merge SUB2API JSON files referenced by successful register task logs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUCCESS_EMAIL_RE = re.compile(r"注册成功:\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
SUB2API_PATH_RE = re.compile(r"SUB2API JSON 已保存[^:]*:\s*(.+?\.json)\s*$")


def _safe_json_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "_", str(value or "").strip())
    stem = stem.strip("._-")
    return stem or "account"


def _exported_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_output_path(repo_root: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return repo_root / "data" / "sub2api" / f"log-success-sub2api-{timestamp}-{uuid.uuid4().hex[:8]}.json"


def _resolve_logged_path(path_text: str, *, repo_root: Path, log_dir: Path) -> Path:
    normalized = path_text.strip().strip("\"'")
    path = Path(normalized)
    if path.is_absolute():
        return path

    repo_path = repo_root / path
    if repo_path.exists():
        return repo_path

    return log_dir / path


def _collect_log_data(log_path: Path, repo_root: Path) -> tuple[list[str], list[Path]]:
    success_emails: list[str] = []
    seen_emails: set[str] = set()
    sub2api_paths: list[Path] = []
    seen_paths: set[Path] = set()
    log_dir = log_path.parent

    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        success_match = SUCCESS_EMAIL_RE.search(line)
        if success_match:
            email = success_match.group(1).strip().lower()
            if email not in seen_emails:
                seen_emails.add(email)
                success_emails.append(email)

        path_match = SUB2API_PATH_RE.search(line)
        if path_match:
            path = _resolve_logged_path(path_match.group(1), repo_root=repo_root, log_dir=log_dir).resolve()
            if path not in seen_paths:
                seen_paths.add(path)
                sub2api_paths.append(path)

    return success_emails, sub2api_paths


def _path_matches_email(path: Path, email: str) -> bool:
    return path.name.startswith(f"{_safe_json_stem(email)}_")


def _load_accounts(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
        return [item for item in payload["accounts"] if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("credentials"), dict):
        return [payload]
    raise ValueError(f"不是有效的 SUB2API JSON: {path}")


def build_merged_payload(log_path: Path, repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    success_emails, logged_paths = _collect_log_data(log_path, repo_root)
    success_set = set(success_emails)
    paths_by_email: dict[str, list[Path]] = {email: [] for email in success_emails}
    skipped_unsuccessful_paths = 0
    missing_files: list[str] = []

    for path in logged_paths:
        matched_email = next((email for email in success_set if _path_matches_email(path, email)), "")
        if not matched_email:
            skipped_unsuccessful_paths += 1
            continue
        if path.exists():
            paths_by_email[matched_email].append(path)
        else:
            missing_files.append(str(path))

    accounts: list[dict[str, Any]] = []
    used_paths: list[str] = []
    missing_success_emails: list[str] = []
    for email in success_emails:
        paths = paths_by_email.get(email) or []
        if not paths:
            missing_success_emails.append(email)
            continue
        for path in paths:
            accounts.extend(_load_accounts(path))
            used_paths.append(str(path))

    payload = {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": _exported_at(),
        "proxies": [],
        "accounts": accounts,
    }
    summary = {
        "successful_emails": len(success_emails),
        "logged_sub2api_paths": len(logged_paths),
        "used_sub2api_paths": len(used_paths),
        "merged_accounts": len(accounts),
        "skipped_unsuccessful_paths": skipped_unsuccessful_paths,
        "missing_success_emails": missing_success_emails,
        "missing_files": missing_files,
        "used_paths": used_paths,
    }
    return payload, summary


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Merge SUB2API JSON files for emails that finished with '注册成功' in a register log.",
    )
    parser.add_argument("log", help="Path to the pasted register log text file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output merged SUB2API JSON path. Defaults to data/sub2api/log-success-sub2api-*.json",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative data/sub2api paths. Defaults to current directory.",
    )
    parser.add_argument(
        "--summary",
        help="Optional path to write a merge summary JSON file.",
    )
    args = parser.parse_args()

    log_path = Path(args.log).expanduser().resolve()
    repo_root = Path(args.repo_root).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve() if args.output else _default_output_path(repo_root)
    summary_path = Path(args.summary).expanduser().resolve() if args.summary else None

    payload, summary = build_merged_payload(log_path, repo_root)
    if not payload["accounts"]:
        raise RuntimeError("未找到可合并的成功账号 SUB2API JSON")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if summary_path:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "合并完成: "
        f"成功邮箱 {summary['successful_emails']} 个，"
        f"使用 JSON {summary['used_sub2api_paths']} 个，"
        f"合并账号 {summary['merged_accounts']} 个"
    )
    if summary["missing_success_emails"]:
        print(f"成功但未找到 SUB2API JSON 的邮箱: {len(summary['missing_success_emails'])} 个")
    if summary["missing_files"]:
        print(f"日志引用但文件不存在: {len(summary['missing_files'])} 个")
    print(f"output={output_path}")
    if summary_path:
        print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
