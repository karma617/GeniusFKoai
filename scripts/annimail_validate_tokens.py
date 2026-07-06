#!/usr/bin/env python3
"""annimail_orders 目录下邮箱 token 批量刷新测活脚本

功能：
  1. 并发读取所有 txt 文件中的凭证行（email----password----uuid----refresh_token）
  2. 对每条凭证调用 Microsoft OAuth2 token 端点刷新 access_token
  3. 刷新成功 → 保留（若返回新 refresh_token 则更新）
  4. 刷新失败 → 删除该行
  5. 文件全部失效则删除文件

用法:
    python scripts/annimail_validate_tokens.py
    python scripts/annimail_validate_tokens.py --workers 30
    python scripts/annimail_validate_tokens.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 常量（与 outlookEmailPlus 保持一致）
# ---------------------------------------------------------------------------

MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_TOKEN_SCOPE = "https://graph.microsoft.com/.default"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
INVALID_TOKEN_KEYWORDS = ("invalid_grant", "aadsts70000")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_text()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# token 刷新（单条）
# ---------------------------------------------------------------------------


def refresh_one(
    cred_line: str,
    session: requests.Session,
) -> tuple[str | None, str]:
    """刷新单条凭证的 token。

    Returns:
        (updated_cred_line | None, status)
        - 成功: (可能含新 token 的凭证行, "ok"/"ok(token更新)")
        - 失败: (None, 错误描述)
    """
    parts = cred_line.split("----", 3)
    if len(parts) < 4:
        return None, "格式错误"

    email, password, client_id, refresh_token = parts

    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": MS_TOKEN_SCOPE,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.post(
                MS_TOKEN_URL, data=data, headers=headers, timeout=REQUEST_TIMEOUT,
            )

            if resp.status_code == 200:
                payload = resp.json()
                new_rt = payload.get("refresh_token")
                if new_rt and new_rt != refresh_token:
                    return (
                        f"{email}----{password}----{client_id}----{new_rt}",
                        "ok(token更新)",
                    )
                return cred_line, "ok"

            if resp.status_code == 429:
                try:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                except ValueError:
                    retry_after = 5
                wait = min(retry_after, 30)
                if attempt < MAX_RETRIES:
                    time.sleep(wait)
                    continue
                return None, f"限流(429)"

            # 非 429 错误：解析错误信息
            try:
                err = resp.json()
            except Exception:
                err = {}
            err_msg = ""
            if isinstance(err, dict):
                err_msg = str(err.get("error_description") or err.get("error") or "")
            normalized = err_msg.lower()
            if any(kw in normalized for kw in INVALID_TOKEN_KEYWORDS):
                return None, f"token失效({err_msg[:60]})"
            # 其他错误也不重试
            return None, f"HTTP {resp.status_code}({err_msg[:60]})"

        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return None, f"网络异常({str(exc)[:60]})"

    return None, "重试耗尽"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="annimail_orders 邮箱 token 批量刷新测活")
    parser.add_argument(
        "--dir", type=Path,
        default=Path(__file__).resolve().parent / "annimail_orders",
        help="凭证文件目录",
    )
    parser.add_argument("--workers", type=int, default=20, help="并发线程数")
    parser.add_argument("--proxy", type=str, default="", help="代理地址")
    parser.add_argument("--dry-run", action="store_true", help="只统计不执行")
    args = parser.parse_args(argv)

    output_dir: Path = args.dir
    if not output_dir.exists():
        log(f"✗ 目录不存在: {output_dir}")
        return 1

    txt_files = sorted(output_dir.glob("*.txt"))
    if not txt_files:
        log("没有需要验证的文件")
        return 0

    # 读取所有凭证行，记录文件→行列表映射
    file_lines: dict[Path, list[str]] = {}
    total = 0
    for fp in txt_files:
        raw = fp.read_text(encoding="utf-8").strip()
        lines = [l for l in raw.split("\n") if l.strip()]
        file_lines[fp] = lines
        total += len(lines)

    log(f"{'=' * 60}")
    log(f"邮箱 token 刷新测活")
    log(f"{'=' * 60}")
    log(f"文件: {len(txt_files)} | 总凭证: {total} | 并发: {args.workers}")

    if args.dry_run:
        log("Dry run 模式，不执行刷新")
        return 0

    # 构建共享 session
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept": "application/json"})
    if args.proxy:
        session.proxies = {"http": args.proxy, "https": args.proxy}

    # 展开为 (file, line_idx, cred_line) 列表
    tasks: list[tuple[Path, int, str]] = []
    for fp, lines in file_lines.items():
        for idx, line in enumerate(lines):
            tasks.append((fp, idx, line))

    # 并发刷新
    results: dict[Path, list[str | None]] = {
        fp: [None] * len(lines) for fp, lines in file_lines.items()
    }
    valid_count = 0
    invalid_count = 0
    token_updated = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(refresh_one, cred, session): (fp, idx)
            for fp, idx, cred in tasks
        }
        done = 0
        for future in as_completed(futures):
            fp, idx = futures[future]
            updated_line, status = future.result()
            results[fp][idx] = updated_line
            done += 1

            if updated_line is not None:
                valid_count += 1
                if status == "ok(token更新)":
                    token_updated += 1
            else:
                invalid_count += 1

            if done % 200 == 0 or done == total:
                elapsed = time.time() - start_time
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                log(
                    f"  进度: {done}/{total} "
                    f"({done * 100 // total}%) "
                    f"| 有效 {valid_count} 无效 {invalid_count} "
                    f"| {rate:.1f}/s ETA {eta:.0f}s"
                )

    elapsed = time.time() - start_time
    log(f"\n刷新完成: {elapsed:.1f}s | 有效 {valid_count} | 无效 {invalid_count} | token更新 {token_updated}")

    # 重写文件
    files_updated = 0
    files_deleted = 0
    lines_removed = 0

    for fp, lines in file_lines.items():
        new_lines = [results[fp][i] for i in range(len(lines)) if results[fp][i] is not None]
        removed = len(lines) - len(new_lines)
        lines_removed += removed

        if len(new_lines) == 0:
            fp.unlink()
            files_deleted += 1
            log(f"  ✗ 删除(全失效): {fp.name} ({len(lines)} → 0)")
        elif removed > 0:
            fp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            files_updated += 1
            log(f"  → 更新: {fp.name} ({len(lines)} → {len(new_lines)}, 删除 {removed})")
        else:
            # 检查是否有 token 更新
            original = file_lines[fp]
            if new_lines != original:
                fp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                files_updated += 1

    log(f"\n{'=' * 60}")
    log(f"全部完成！")
    log(f"  有效保留: {valid_count} | 失效删除: {invalid_count}")
    log(f"  token 更新: {token_updated}")
    log(f"  文件更新: {files_updated} | 文件删除: {files_deleted}")
    log(f"  耗时: {elapsed:.1f}s")
    log(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
