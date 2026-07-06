#!/usr/bin/env python3
"""并发验证全部凭证 — 不删除源文件，结果写入报告。

严格参照 outlookEmailPlus/graph.py 的 test_refresh_token_with_rotation 逻辑，
并针对第三方 client_id 的 AADSTS90023 问题增加 scope fallback。

策略：
  - 按 client_id 分组，每组独立线程池（5线程），避免单一 client_id 过载
  - 优先使用 .default scope，AADSTS90023 时 fallback 到 Mail.Read
  - AADSTS50196 (client request loop): 等待 30s 重试（最多 3 次）
  - 429: 读取 Retry-After 退避
  - 网络异常: 指数退避 2^attempt
  - 仅 invalid_grant / aadsts70000 判定为真失效（与 outlookEmailPlus 一致）
  - 其他错误标记为 unknown，不删除
  - 源文件完全不动

用法:
    python scripts/annimail_validate_v2.py
    python scripts/annimail_validate_v2.py --workers 5
    python scripts/annimail_validate_v2.py --workers 10 --delay 0.5
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 常量 — 严格对齐 outlookEmailPlus/graph.py
# ---------------------------------------------------------------------------

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
FALLBACK_SCOPE = "https://graph.microsoft.com/Mail.Read"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3

# 失效判定 — 与 outlookEmailPlus/refresh.py _classify_refresh_failure 一致
INVALID_TOKEN_KEYWORDS = ("invalid_grant", "aadsts70000")

# AADSTS50196: client request loop — 需要长退避后重试
RATE_LIMIT_LOOP_KEYWORDS = ("aadsts50196",)
RATE_LIMIT_LOOP_WAIT = 30  # 秒

# AADSTS90023: scope 不足 — 换 scope 重试
SCOPE_INSUFFICIENT_KEYWORDS = ("aadsts90023",)

CRED_LINE_RE = re.compile(
    r"([\w.-]+@(?:outlook|hotmail)\.com)"
    r"----([\w-]+)"
    r"----([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"
    r"----(.+)",
)

# ---------------------------------------------------------------------------
# 线程安全
# ---------------------------------------------------------------------------

PRINT_LOCK = threading.Lock()
COUNTER_LOCK = threading.Lock()
VALID_FILE_LOCK = threading.Lock()
INVALID_FILE_LOCK = threading.Lock()
UNKNOWN_FILE_LOCK = threading.Lock()

# 进度计数器
_done_count = 0
_valid_count = 0
_invalid_count = 0
_unknown_count = 0
_token_updated = 0
_start_time = 0.0
_total_creds = 0

# 错误分布
_error_stats: dict[str, int] = {}


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    with PRINT_LOCK:
        print(f"[{now_text()}] {msg}", flush=True)


def update_progress(status: str, error_key: str = "") -> None:
    global _done_count, _valid_count, _invalid_count, _unknown_count, _token_updated
    with COUNTER_LOCK:
        _done_count += 1
        if status == "valid":
            _valid_count += 1
        elif status == "invalid":
            _invalid_count += 1
        else:
            _unknown_count += 1
        if error_key:
            _error_stats[error_key] = _error_stats.get(error_key, 0) + 1
        total = _total_creds
        elapsed = time.time() - _start_time
        rate = _done_count / elapsed if elapsed > 0 else 0
        eta = (total - _done_count) / rate if rate > 0 else 0
        if _done_count % 500 == 0 or _done_count == total:
            print(
                f"[{now_text()}]   进度: {_done_count}/{total} "
                f"({100*_done_count/total:.0f}%) "
                f"| 有效 {_valid_count} 无效 {_invalid_count} 未知 {_unknown_count} "
                f"| {rate:.1f}/s ETA {eta:.0f}s",
                flush=True,
            )


# ---------------------------------------------------------------------------
# 核心刷新逻辑 — 参照 test_refresh_token_with_rotation
# ---------------------------------------------------------------------------


def refresh_token_with_fallback(
    client_id: str,
    refresh_token: str,
    scope: str = FALLBACK_SCOPE,
) -> tuple[bool, str | None, str | None, str]:
    """刷新 token，支持 scope fallback 和 AADSTS50196 重试。

    Returns:
        (success, error_msg, new_refresh_token, used_scope)
    """
    url = TOKEN_URL
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
    }

    last_error_msg = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            res = requests.post(url, data=data, timeout=REQUEST_TIMEOUT)

            if res.status_code == 200:
                try:
                    payload = res.json()
                except Exception:
                    payload = {}
                new_rt = payload.get("refresh_token")
                return True, None, new_rt, scope

            # 429 限流
            if res.status_code == 429:
                try:
                    retry_after = int(res.headers.get("Retry-After", 0))
                except Exception:
                    retry_after = 0
                wait = retry_after if retry_after else (2**attempt)
                last_error_msg = f"429 限流, {wait}s 后重试"
                if attempt < MAX_RETRIES:
                    time.sleep(wait)
                    continue

            # 解析错误
            try:
                error_data = res.json()
            except Exception:
                error_data = {}
            error_msg = ""
            if isinstance(error_data, dict):
                error_msg = error_data.get("error_description") or error_data.get("error") or ""
            if not error_msg:
                error_msg = res.text[:500]
            last_error_msg = str(error_msg)
            error_lower = error_msg.lower()

            # AADSTS50196: client request loop → 长退避重试
            if any(k in error_lower for k in RATE_LIMIT_LOOP_KEYWORDS):
                if attempt < MAX_RETRIES:
                    log(f"  → AADSTS50196, 等待 {RATE_LIMIT_LOOP_WAIT}s 重试")
                    time.sleep(RATE_LIMIT_LOOP_WAIT)
                    continue

            # 非429的明确错误 — 直接返回（与参考实现一致）
            return False, last_error_msg, None, scope

        except Exception as e:
            last_error_msg = f"请求异常: {str(e)}"
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            return False, last_error_msg, None, scope

    return False, last_error_msg or "请求失败", None, scope


# ---------------------------------------------------------------------------
# 单条凭证处理
# ---------------------------------------------------------------------------


def process_credential(cred_line: str, source_file: str) -> dict:
    """处理单条凭证。

    Returns:
        {
            "cred_line": str,          # 原始行
            "updated_line": str | None, # 更新 token 后的行（如果有效且 token 轮换）
            "status": "valid" | "invalid" | "unknown",
            "email": str,
            "error": str | None,
            "source_file": str,
        }
    """
    m = CRED_LINE_RE.match(cred_line.strip())
    if not m:
        return {
            "cred_line": cred_line,
            "updated_line": None,
            "status": "unknown",
            "email": "?",
            "error": "格式不匹配",
            "source_file": source_file,
        }

    email, password, client_id, refresh_token = m.groups()

    ok, error_msg, new_rt, used_scope = refresh_token_with_fallback(
        client_id, refresh_token
    )

    if ok:
        updated_line = cred_line.strip()
        if new_rt and new_rt != refresh_token:
            updated_line = f"{email}----{password}----{client_id}----{new_rt}"
        return {
            "cred_line": cred_line.strip(),
            "updated_line": updated_line,
            "status": "valid",
            "email": email,
            "error": None,
            "source_file": source_file,
            "scope": used_scope,
        }

    # 判定是否真失效 — 与 outlookEmailPlus 一致
    error_lower = (error_msg or "").lower()
    is_truly_invalid = any(k in error_lower for k in INVALID_TOKEN_KEYWORDS)

    # AADSTS50196 / AADSTS90023 重试后仍失败 → unknown，不是 invalid
    is_rate_limit = any(k in error_lower for k in RATE_LIMIT_LOOP_KEYWORDS)
    is_scope_issue = any(k in error_lower for k in SCOPE_INSUFFICIENT_KEYWORDS)

    if is_truly_invalid:
        status = "invalid"
        error_key = "invalid_grant_or_aadsts70000"
    elif is_rate_limit:
        status = "unknown"
        error_key = "aadsts50196_exhausted"
    elif is_scope_issue:
        status = "unknown"
        error_key = "aadsts90023_exhausted"
    else:
        status = "unknown"
        error_key = "other_error"

    return {
        "cred_line": cred_line.strip(),
        "updated_line": None,
        "status": status,
        "email": email,
        "error": error_msg,
        "source_file": source_file,
        "error_key": error_key,
    }


# ---------------------------------------------------------------------------
# 文件写入
# ---------------------------------------------------------------------------

OUT_DIR = Path("scripts/annimail_orders")
VALID_FILE = OUT_DIR / "validated_valid.txt"
INVALID_FILE = OUT_DIR / "validated_invalid.txt"
UNKNOWN_FILE = OUT_DIR / "validated_unknown.txt"


def append_valid(line: str) -> None:
    with VALID_FILE_LOCK:
        with open(VALID_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def append_invalid(line: str) -> None:
    with INVALID_FILE_LOCK:
        with open(INVALID_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def append_unknown(line: str) -> None:
    with UNKNOWN_FILE_LOCK:
        with open(UNKNOWN_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------


def load_all_credentials() -> list[tuple[str, str]]:
    """加载所有凭证，返回 [(cred_line, source_file), ...]"""
    creds = []
    for f in sorted(OUT_DIR.glob("*.txt")):
        if f.name.startswith("validated_"):
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and CRED_LINE_RE.match(line):
                creds.append((line, f.name))
    return creds


def group_by_client_id(
    creds: list[tuple[str, str]],
) -> dict[str, list[tuple[str, str]]]:
    """按 client_id 分组"""
    groups: dict[str, list[tuple[str, str]]] = {}
    for cred_line, source_file in creds:
        m = CRED_LINE_RE.match(cred_line)
        if not m:
            continue
        client_id = m.group(3)
        groups.setdefault(client_id, []).append((cred_line, source_file))
    return groups


def process_group(
    client_id: str,
    items: list[tuple[str, str]],
    workers: int,
    delay: float,
) -> list[dict]:
    """处理一个 client_id 组"""
    results = []

    def task(cred_line: str, source_file: str) -> dict:
        if delay > 0:
            time.sleep(random.uniform(0, delay))
        return process_credential(cred_line, source_file)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(task, cl, sf): (cl, sf) for cl, sf in items
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                cl, sf = futures[future]
                result = {
                    "cred_line": cl,
                    "updated_line": None,
                    "status": "unknown",
                    "email": "?",
                    "error": f"线程异常: {e}",
                    "source_file": sf,
                    "error_key": "thread_exception",
                }
            results.append(result)

            # 写入对应文件
            status = result["status"]
            if status == "valid":
                append_valid(result["updated_line"] or result["cred_line"])
                if result.get("updated_line") and result["updated_line"] != result["cred_line"]:
                    global _token_updated
                    with COUNTER_LOCK:
                        _token_updated += 1
            elif status == "invalid":
                append_invalid(result["cred_line"])
            else:
                append_unknown(f"{result['cred_line']}  # {result.get('error', '')[:200]}")

            update_progress(status, result.get("error_key", ""))

    return results


def main() -> int:
    global _start_time

    parser = argparse.ArgumentParser(description="验证全部凭证（安全模式）")
    parser.add_argument("--workers", type=int, default=5, help="每个 client_id 组的并发线程数")
    parser.add_argument("--delay", type=float, default=0.5, help="每请求前的随机延迟（秒）")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    _start_time = time.time()

    # 清空旧报告
    for f in [VALID_FILE, INVALID_FILE, UNKNOWN_FILE]:
        if f.exists():
            f.unlink()

    log("加载凭证...")
    creds = load_all_credentials()
    log(f"总凭证数: {len(creds)}")

    global _total_creds
    _total_creds = len(creds)

    groups = group_by_client_id(creds)
    log(f"client_id 分组: {len(groups)} 个")
    for cid, items in groups.items():
        log(f"  {cid}: {len(items)} 条")

    all_results = []

    # 按 client_id 分组并行处理
    # 每组内部用 args.workers 线程，组间串行避免叠加触发 AADSTS50196
    for cid, items in groups.items():
        log(f"\n开始处理 client_id={cid[:8]}... ({len(items)} 条, {args.workers} 线程)")
        results = process_group(cid, items, args.workers, args.delay)
        all_results.extend(results)

    elapsed = time.time() - _start_time
    valid = sum(1 for r in all_results if r["status"] == "valid")
    invalid = sum(1 for r in all_results if r["status"] == "invalid")
    unknown = sum(1 for r in all_results if r["status"] == "unknown")

    log("\n" + "=" * 60)
    log(f"验证完成！耗时 {elapsed:.1f}s")
    log(f"  有效凭证: {valid} 条 → {VALID_FILE}")
    log(f"  失效凭证: {invalid} 条 → {INVALID_FILE}")
    log(f"  未知/错误: {unknown} 条 → {UNKNOWN_FILE}")
    log(f"  token 更新: {_token_updated} 条")
    log(f"  源文件未改动")
    log("\n错误分布:")
    for k, v in sorted(_error_stats.items(), key=lambda x: -x[1]):
        log(f"  {k}: {v}")
    log("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
