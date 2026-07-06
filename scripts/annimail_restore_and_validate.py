#!/usr/bin/env python3
"""从 _html 目录重新提取全部凭证，并发验证 token 有效性，保存有效凭证。

功能：
  1. 遍历 _html/*.html，提取所有 email----password----uuid----refresh_token 行
  2. 并发调用 Microsoft OAuth2 token 端点刷新
  3. 先用 .default scope，失败再用 Mail.Read scope
  4. 有效凭证（含新 token）按原文件分组写入 txt
  5. 全部失效的文件不生成 txt
  6. 输出详细统计

用法:
    python scripts/annimail_restore_and_validate.py
    python scripts/annimail_restore_and_validate.py --workers 30
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
SCOPES_TO_TRY = [
    "https://graph.microsoft.com/.default",
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.ReadWrite",
]
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3

CRED_LINE_RE = re.compile(
    r"[\w.-]+@(?:outlook|hotmail)\.com"
    r"----[\w-]+"
    r"----[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    r"----.+",
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_text()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# token 刷新（多 scope 尝试）
# ---------------------------------------------------------------------------


def refresh_one(
    cred_line: str,
    session: requests.Session,
) -> tuple[str | None, str]:
    """刷新单条凭证，依次尝试多个 scope。

    Returns:
        (updated_cred_line | None, status)
    """
    parts = cred_line.split("----", 3)
    if len(parts) < 4:
        return None, "格式错误"

    email, password, client_id, refresh_token = parts

    for scope in SCOPES_TO_TRY:
        data = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": scope,
        }

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = session.post(
                    MS_TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT,
                )

                if resp.status_code == 200:
                    payload = resp.json()
                    new_rt = payload.get("refresh_token")
                    if new_rt and new_rt != refresh_token:
                        return (
                            f"{email}----{password}----{client_id}----{new_rt}",
                            f"ok({scope},token更新)",
                        )
                    return cred_line, f"ok({scope})"

                if resp.status_code == 429:
                    try:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                    except ValueError:
                        retry_after = 5
                    wait = min(retry_after, 30)
                    if attempt < MAX_RETRIES:
                        time.sleep(wait)
                        continue
                    # 限流耗尽，试下一个 scope 也没意义
                    break

                # 非 429 错误：解析错误信息
                try:
                    err = resp.json()
                except Exception:
                    err = {}
                err_msg = ""
                if isinstance(err, dict):
                    err_msg = str(
                        err.get("error_description") or err.get("error") or ""
                    )

                # 如果是 AADSTS90023（权限不足），换 scope 重试
                if "AADSTS90023" in err_msg:
                    break  # break inner loop, try next scope

                # 其他错误（invalid_grant, service abuse 等）→ token 确实无效
                return None, f"失效({err_msg[:80]})"

            except requests.RequestException as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                return None, f"网络异常({str(exc)[:60]})"

    # 所有 scope 都失败了
    return None, "所有scope失败"


# ---------------------------------------------------------------------------
# HTML 凭证提取
# ---------------------------------------------------------------------------


def extract_creds_from_html(html_path: Path) -> list[str]:
    """从 HTML 文件提取去重后的凭证行列表。"""
    html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator="\n", strip=True)

    seen: set[str] = set()
    cred_lines: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if CRED_LINE_RE.match(line) and line not in seen:
            seen.add(line)
            cred_lines.append(line)

    return cred_lines


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="从 HTML 重新提取凭证并验证 token"
    )
    parser.add_argument(
        "--html-dir", type=Path,
        default=Path(__file__).resolve().parent / "annimail_orders" / "_html",
        help="HTML 文件目录",
    )
    parser.add_argument(
        "--out-dir", type=Path,
        default=Path(__file__).resolve().parent / "annimail_orders",
        help="输出 txt 目录",
    )
    parser.add_argument("--workers", type=int, default=20, help="并发线程数")
    parser.add_argument("--proxy", type=str, default="", help="代理地址")
    args = parser.parse_args(argv)

    html_dir: Path = args.html_dir
    out_dir: Path = args.out_dir
    if not html_dir.exists():
        log(f"✗ HTML 目录不存在: {html_dir}")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    html_files = sorted(html_dir.glob("*.html"))
    if not html_files:
        log("没有 HTML 文件")
        return 0

    # 从所有 HTML 提取凭证，按文件分组
    file_creds: dict[Path, list[str]] = {}
    total = 0
    for hf in html_files:
        creds = extract_creds_from_html(hf)
        if creds:
            file_creds[hf] = creds
            total += len(creds)

    log(f"{'=' * 60}")
    log(f"从 HTML 重新提取并验证 token")
    log(f"{'=' * 60}")
    log(f"HTML 文件: {len(html_files)} | 有凭证: {len(file_creds)} | 总凭证: {total}")
    log(f"并发: {args.workers} | Scope 尝试: {', '.join(SCOPES_TO_TRY)}")

    # 构建共享 session
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    })
    if args.proxy:
        session.proxies = {"http": args.proxy, "https": args.proxy}

    # 展开为 (file, line_idx, cred_line) 列表
    tasks: list[tuple[Path, int, str]] = []
    for fp, creds in file_creds.items():
        for idx, line in enumerate(creds):
            tasks.append((fp, idx, line))

    # 并发刷新
    results: dict[Path, list[str | None]] = {
        fp: [None] * len(creds) for fp, creds in file_creds.items()
    }
    valid_count = 0
    invalid_count = 0
    token_updated = 0
    error_stats: dict[str, int] = {}
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
                if "token更新" in status:
                    token_updated += 1
            else:
                invalid_count += 1
                # 统计错误类型
                key = status.split("(")[0] if "(" in status else status
                error_stats[key] = error_stats.get(key, 0) + 1

            if done % 500 == 0 or done == total:
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
    log(f"\n刷新完成: {elapsed:.1f}s")
    log(f"  有效: {valid_count} | 无效: {invalid_count} | token更新: {token_updated}")
    log(f"  错误分布:")
    for k, v in sorted(error_stats.items(), key=lambda x: -x[1]):
        log(f"    {k}: {v}")

    # 写入有效凭证到 txt 文件
    files_written = 0
    files_empty = 0
    lines_written = 0

    for fp, creds in file_creds.items():
        new_lines = [
            results[fp][i]
            for i in range(len(creds))
            if results[fp][i] is not None
        ]

        if not new_lines:
            files_empty += 1
            continue

        # 确定域名后缀
        first_email = new_lines[0].split("----")[0].lower()
        domain = "hotmail" if "@hotmail.com" in first_email else "outlook"

        # 从 HTML 文件名构建 txt 文件名
        # HTML: 202602231617406916.html → txt: 2026-02-23_16-17-50-outlook.txt
        # 提取 datetime 部分
        stem = fp.stem  # e.g. 202602231617406916
        if len(stem) >= 14:
            dt_str = f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}_{stem[8:10]}-{stem[10:12]}-{stem[12:14]}"
        else:
            dt_str = stem

        out_name = f"{dt_str}-{domain}.txt"
        out_path = out_dir / out_name
        out_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        files_written += 1
        lines_written += len(new_lines)

    log(f"\n{'=' * 60}")
    log(f"全部完成！")
    log(f"  有效凭证: {valid_count} 条 → {files_written} 个文件")
    log(f"  全失效文件: {files_empty} 个（不生成 txt）")
    log(f"  token 更新: {token_updated}")
    log(f"  耗时: {elapsed:.1f}s")
    log(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
