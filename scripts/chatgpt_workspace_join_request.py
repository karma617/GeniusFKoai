"""ChatGPT workspace join request scanner.

The script is a Python rewrite of the browser UserScript workflow:
generate random workspace UUIDs, POST an invite route, and stop on HTTP 2xx.
It reads the ChatGPT accessToken from a JSON file placed beside this script.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests


CHATGPT_APP = "https://chatgpt.com"
DEFAULT_CONFIG_NAME = "chatgpt_workspace_join_config.json"
DEFAULT_CONCURRENCY = 100
DEFAULT_NETWORK_RETRIES = 3
DEFAULT_NETWORK_RETRY_DELAY_MS = 1000
SUPPORTED_ROUTES = {"request", "accept"}


@dataclass(frozen=True)
class JoinResult:
    workspace_id: str
    ok: bool
    status: int
    body: str
    error: str = ""


@dataclass(frozen=True)
class SuccessResult:
    workspace_id: str
    status: int
    body: str
    attempts: int
    route: str
    mode: str


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(message: str) -> None:
    print(f"[{now_text()}] {message}", flush=True)


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def default_config_path() -> Path:
    return script_dir() / DEFAULT_CONFIG_NAME


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是 JSON object")
    return data


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def get_access_token(config: dict[str, Any]) -> str:
    session = config.get("session")
    if not isinstance(session, dict):
        session = {}
    auth = config.get("auth")
    if not isinstance(auth, dict):
        auth = {}
    return first_text(
        config.get("accessToken"),
        config.get("access_token"),
        config.get("at"),
        config.get("AT"),
        session.get("accessToken"),
        session.get("access_token"),
        auth.get("accessToken"),
        auth.get("access_token"),
        auth.get("at"),
    )


def is_placeholder_token(token: str) -> bool:
    upper = token.strip().upper()
    return not token or upper.startswith("REPLACE_") or "替换" in token


def mask_secret(value: str, keep: int = 6) -> str:
    text = str(value or "")
    if len(text) <= keep * 2:
        return "***" if text else ""
    return f"{text[:keep]}...{text[-keep:]}"


def as_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return default


def build_headers(config: dict[str, Any], access_token: str, device_id: str) -> dict[str, str]:
    language = first_text(config.get("oai_language"), config.get("language")) or "en-US"
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "oai-device-id": device_id,
        "oai-language": language,
        "user-agent": first_text(config.get("user_agent"))
        or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
    }
    cookies = first_text(config.get("cookies"), config.get("cookie"))
    if cookies:
        headers["cookie"] = cookies
    return headers


def proxies_from_config(config: dict[str, Any]) -> dict[str, str] | None:
    proxy = first_text(config.get("proxy"), config.get("https_proxy"), config.get("http_proxy"))
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        padded = part + "=" * (-len(part) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def access_token_summary(token: str) -> str:
    payload = decode_jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        auth = {}
    profile = payload.get("https://api.openai.com/profile")
    if not isinstance(profile, dict):
        profile = {}
    email = first_text(profile.get("email"), payload.get("email")) or "unknown"
    plan_type = first_text(auth.get("chatgpt_plan_type")) or "unknown"
    account_id = first_text(auth.get("chatgpt_account_id"))
    exp = payload.get("exp")
    exp_text = "unknown"
    if isinstance(exp, (int, float)):
        remain_minutes = int((int(exp) - time.time()) / 60)
        exp_text = f"{remain_minutes} min remaining"
    if account_id:
        return f"{email} / {plan_type} / {account_id[:8]} / {exp_text}"
    return f"{email} / {plan_type} / {exp_text}"


def send_one(
    *,
    workspace_id: str,
    route: str,
    headers: dict[str, str],
    timeout_seconds: int,
    proxies: dict[str, str] | None,
    network_retries: int = DEFAULT_NETWORK_RETRIES,
    network_retry_delay_ms: int = DEFAULT_NETWORK_RETRY_DELAY_MS,
    retry_label: str = "",
) -> JoinResult:
    url = f"{CHATGPT_APP}/backend-api/accounts/{workspace_id}/invites/{route}"
    total_attempts = max(0, network_retries) + 1
    for attempt in range(1, total_attempts + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                data="",
                timeout=timeout_seconds,
                proxies=proxies,
            )
            return JoinResult(
                workspace_id=workspace_id,
                ok=response.ok,
                status=int(response.status_code),
                body=response.text or "",
            )
        except requests.Timeout:
            result = JoinResult(workspace_id=workspace_id, ok=False, status=-1, body="", error="timeout")
        except requests.RequestException as exc:
            result = JoinResult(workspace_id=workspace_id, ok=False, status=0, body="", error=str(exc))

        if attempt < total_attempts:
            label = f" {retry_label}" if retry_label else ""
            log(f"重试{label} 第 {attempt}/{network_retries}: {describe_failure(result)}")
            if network_retry_delay_ms > 0:
                time.sleep(network_retry_delay_ms / 1000)
            continue
        return result

    return JoinResult(workspace_id=workspace_id, ok=False, status=0, body="", error="network retry exhausted")


def describe_failure(result: JoinResult) -> str:
    if result.status == -1:
        return "timeout"
    if result.error:
        return f"network error: {result.error}"
    return f"HTTP {result.status}: {result.body[:180]}"


def run_sequential(
    *,
    config: dict[str, Any],
    route: str,
    limit: int,
    interval_ms: int,
    timeout_seconds: int,
    network_retries: int,
    network_retry_delay_ms: int,
    dry_run: bool,
) -> SuccessResult | None:
    access_token = get_access_token(config)
    device_id = first_text(config.get("device_id"), config.get("deviceId")) or str(uuid.uuid4())
    headers = build_headers(config, access_token, device_id)
    proxies = proxies_from_config(config)
    stop_on_non404 = as_bool(config.get("stop_on_non404"), False)

    for attempt in range(1, limit + 1):
        workspace_id = str(uuid.uuid4())
        url = f"{CHATGPT_APP}/backend-api/accounts/{workspace_id}/invites/{route}"
        if dry_run:
            log(f"dry-run: [{attempt}] POST {url}")
            continue

        result = send_one(
            workspace_id=workspace_id,
            route=route,
            headers=headers,
            timeout_seconds=timeout_seconds,
            proxies=proxies,
            network_retries=network_retries,
            network_retry_delay_ms=network_retry_delay_ms,
            retry_label=f"[{attempt}] {workspace_id[:8]}..",
        )
        if result.ok:
            log(f"[{attempt}] 成功: {workspace_id} | HTTP {result.status}")
            return SuccessResult(
                workspace_id=workspace_id,
                status=result.status,
                body=result.body,
                attempts=attempt,
                route=route,
                mode="sequential",
            )

        if result.status == 404:
            log(f"[{attempt}] {workspace_id[:8]}.. HTTP 404，继续")
        else:
            log(f"[{attempt}] {workspace_id[:8]}.. {describe_failure(result)}")
            if result.status in {0, -1}:
                pass
            elif result.status in {401, 403}:
                log("AT 鉴权失败，已停止。请更新同目录 JSON 里的 accessToken。")
                return None
            elif stop_on_non404:
                log("发现非 404 结果，按 stop_on_non404=true 停止。")
                return None

        if attempt < limit:
            time.sleep(interval_ms / 1000)
    return None


def run_concurrent(
    *,
    config: dict[str, Any],
    route: str,
    limit: int,
    concurrency: int,
    timeout_seconds: int,
    network_retries: int,
    network_retry_delay_ms: int,
    dry_run: bool,
) -> SuccessResult | None:
    access_token = get_access_token(config)
    device_id = first_text(config.get("device_id"), config.get("deviceId")) or str(uuid.uuid4())
    headers = build_headers(config, access_token, device_id)
    proxies = proxies_from_config(config)
    attempts = 0

    while attempts < limit:
        current_batch_size = min(concurrency, limit - attempts)
        batch_start = attempts + 1
        batch_ids = [str(uuid.uuid4()) for _ in range(current_batch_size)]
        attempts += current_batch_size
        batch_end = attempts

        if dry_run:
            for idx, workspace_id in enumerate(batch_ids, start=batch_start):
                url = f"{CHATGPT_APP}/backend-api/accounts/{workspace_id}/invites/{route}"
                log(f"dry-run: [{idx}] POST {url}")
            continue

        batch_items = list(enumerate(batch_ids, start=batch_start))
        log(f"并发批次 {batch_start}-{batch_end}: {current_batch_size} 个请求")
        network_failure_count = 0
        auth_failed_result: JoinResult | None = None
        blocking_result: JoinResult | None = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=current_batch_size) as executor:
            futures: list[concurrent.futures.Future[JoinResult]] = []
            future_meta: dict[concurrent.futures.Future[JoinResult], tuple[int, str]] = {}
            for attempt_idx, workspace_id in batch_items:
                log(f"提交 [{attempt_idx}] {workspace_id}")
                future = executor.submit(
                    send_one,
                    workspace_id=workspace_id,
                    route=route,
                    headers=headers,
                    timeout_seconds=timeout_seconds,
                    proxies=proxies,
                    network_retries=network_retries,
                    network_retry_delay_ms=network_retry_delay_ms,
                    retry_label=f"[{attempt_idx}] {workspace_id[:8]}..",
                )
                futures.append(future)
                future_meta[future] = (attempt_idx, workspace_id)

            for future in concurrent.futures.as_completed(futures):
                attempt_idx, workspace_id = future_meta[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = JoinResult(
                        workspace_id=workspace_id,
                        ok=False,
                        status=0,
                        body="",
                        error=str(exc),
                    )
                if result.ok:
                    for pending in futures:
                        pending.cancel()
                    log(f"完成 [{attempt_idx}] {result.workspace_id} 成功 HTTP {result.status} | {result.body[:180]}")
                    return SuccessResult(
                        workspace_id=result.workspace_id,
                        status=result.status,
                        body=result.body,
                        attempts=attempts,
                        route=route,
                        mode="concurrent",
                    )
                if result.status in {0, -1}:
                    network_failure_count += 1
                    log(f"完成 [{attempt_idx}] {result.workspace_id[:8]}.. {describe_failure(result)}，继续")
                elif result.status in {401, 403}:
                    auth_failed_result = result
                    log(f"完成 [{attempt_idx}] {result.workspace_id[:8]}.. HTTP {result.status} AT 鉴权失败")
                elif result.status != 404:
                    blocking_result = result
                    log(f"完成 [{attempt_idx}] {result.workspace_id[:8]}.. {describe_failure(result)}")
                else:
                    log(f"完成 [{attempt_idx}] {result.workspace_id[:8]}.. HTTP 404")

        if auth_failed_result is not None:
            log(f"本批次发现 AT 鉴权失败，已停止: {auth_failed_result.workspace_id[:8]}.. HTTP {auth_failed_result.status}")
            return None
        if blocking_result is not None:
            log(
                "本批次发现非 404 业务结果，已停止: "
                f"{blocking_result.workspace_id[:8]}.. {describe_failure(blocking_result)}"
            )
            return None
        if network_failure_count:
            log(f"批次 {batch_start}-{batch_end} 有 {network_failure_count} 个网络失败，已重试并跳过，继续下一批")
        else:
            log(f"批次 {batch_start}-{batch_end} 全为 404，继续")
    return None


def dingding_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("dingding")
    if not isinstance(value, dict):
        value = config.get("dingtalk")
    return value if isinstance(value, dict) else {}


def dingding_signed_url(webhook: str, secret: str) -> str:
    if not secret:
        return webhook
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={sign}"


def notify_dingding(config: dict[str, Any], success: SuccessResult, *, dry_run: bool) -> bool:
    ding = dingding_config(config)
    webhook = first_text(ding.get("webhook"), config.get("dingding_webhook"), config.get("dingtalk_webhook"))
    if not webhook:
        log("未配置钉钉 webhook，跳过通知。")
        return True

    secret = first_text(ding.get("secret"), config.get("dingding_secret"), config.get("dingtalk_secret"))
    at_mobiles = ding.get("at_mobiles") or ding.get("atMobiles") or []
    if not isinstance(at_mobiles, list):
        at_mobiles = []
    is_at_all = as_bool(ding.get("is_at_all", ding.get("isAtAll")), False)
    title = "ChatGPT Workspace Join 成功"
    text = (
        f"### {title}\n\n"
        f"- route: {success.route}\n"
        f"- mode: {success.mode}\n"
        f"- workspace: `{success.workspace_id}`\n"
        f"- HTTP: {success.status}\n"
        f"- attempts: {success.attempts}\n"
        f"- time: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}"
    )
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
        "at": {"atMobiles": [str(item) for item in at_mobiles], "isAtAll": is_at_all},
    }
    if dry_run:
        log(f"dry-run: 将通知钉钉 webhook={mask_secret(webhook, keep=12)}")
        return True

    try:
        response = requests.post(
            dingding_signed_url(webhook, secret),
            json=payload,
            timeout=as_int(ding.get("timeout_seconds"), 10),
        )
        response.raise_for_status()
        data: Any
        try:
            data = response.json()
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get("errcode") not in (None, 0):
            log(f"钉钉通知失败: {data}")
            return False
        log("钉钉通知已发送。")
        return True
    except requests.RequestException as exc:
        log(f"钉钉通知失败: {exc}")
        return False


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="随机 workspace UUID 并发送 ChatGPT workspace join request")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="配置 JSON，默认读取脚本同目录")
    parser.add_argument("--route", choices=sorted(SUPPORTED_ROUTES), help="invite 路由，默认读配置或 request")
    parser.add_argument("--mode", choices=["sequential", "concurrent"], help="运行模式，默认读配置或 concurrent")
    parser.add_argument("--limit", type=int, help="最多尝试次数，默认读配置或 1000000")
    parser.add_argument("--interval-ms", type=int, help="顺序模式每次请求间隔，默认读配置或 500")
    parser.add_argument("--timeout-seconds", type=int, help="单请求超时秒数，默认读配置或 15")
    parser.add_argument("--concurrency", type=int, help="并发模式线程数，默认读配置或 100")
    parser.add_argument("--batch-size", type=int, help="兼容旧参数，等同于 --concurrency")
    parser.add_argument("--network-retries", type=int, help="单请求网络错误重试次数，默认读配置或 3")
    parser.add_argument("--network-retry-delay-ms", type=int, help="网络错误重试间隔毫秒，默认读配置或 1000")
    parser.add_argument("--dry-run", action="store_true", help="只验证配置和打印目标请求，不真正发送")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args(argv)
    config_path = args.config.resolve()
    try:
        config = load_json(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log(str(exc))
        if config_path == default_config_path():
            log(
                "首次使用请先复制示例配置: "
                "Copy-Item scripts\\chatgpt_workspace_join_config.example.json "
                "scripts\\chatgpt_workspace_join_config.json"
            )
        return 2
    access_token = get_access_token(config)
    dry_run = bool(args.dry_run)

    if is_placeholder_token(access_token) and not dry_run:
        log("配置里的 accessToken 为空或仍是占位值，已停止。")
        return 2

    route = args.route or first_text(config.get("route")) or "request"
    if route not in SUPPORTED_ROUTES:
        log(f"不支持的 route: {route}")
        return 2
    mode = args.mode or first_text(config.get("mode")) or "concurrent"
    if mode not in {"sequential", "concurrent"}:
        log(f"不支持的 mode: {mode}")
        return 2

    limit = args.limit or as_int(config.get("max_total_attempts"), 1_000_000)
    interval_ms = args.interval_ms or as_int(config.get("interval_ms"), 500, minimum=0)
    timeout_seconds = args.timeout_seconds or as_int(config.get("timeout_seconds"), 15)
    concurrency = (
        args.concurrency
        or args.batch_size
        or as_int(config.get("concurrency"), as_int(config.get("batch_size"), DEFAULT_CONCURRENCY))
    )
    if concurrency < 1:
        log("concurrency 必须大于等于 1")
        return 2
    network_retries = (
        args.network_retries
        if args.network_retries is not None
        else as_int(config.get("network_retries"), DEFAULT_NETWORK_RETRIES, minimum=0)
    )
    network_retry_delay_ms = (
        args.network_retry_delay_ms
        if args.network_retry_delay_ms is not None
        else as_int(config.get("network_retry_delay_ms"), DEFAULT_NETWORK_RETRY_DELAY_MS, minimum=0)
    )
    if network_retries < 0:
        log("network_retries 必须大于等于 0")
        return 2
    if network_retry_delay_ms < 0:
        log("network_retry_delay_ms 必须大于等于 0")
        return 2

    log(f"配置文件: {config_path}")
    if access_token:
        log(f"AT: {mask_secret(access_token)} | {access_token_summary(access_token)}")
    else:
        log("AT: 未配置")
    if mode == "concurrent":
        log(
            f"route={route}, mode={mode}, limit={limit}, timeout={timeout_seconds}s, "
            f"concurrency={concurrency}, network_retries={network_retries}"
        )
    else:
        log(f"route={route}, mode={mode}, limit={limit}, timeout={timeout_seconds}s")

    if mode == "concurrent":
        success = run_concurrent(
            config=config,
            route=route,
            limit=limit,
            concurrency=concurrency,
            timeout_seconds=timeout_seconds,
            network_retries=network_retries,
            network_retry_delay_ms=network_retry_delay_ms,
            dry_run=dry_run,
        )
    else:
        success = run_sequential(
            config=config,
            route=route,
            limit=limit,
            interval_ms=interval_ms,
            timeout_seconds=timeout_seconds,
            network_retries=network_retries,
            network_retry_delay_ms=network_retry_delay_ms,
            dry_run=dry_run,
        )

    if dry_run:
        notify_dingding(
            config,
            SuccessResult(
                workspace_id="dry-run-workspace-id",
                status=200,
                body="",
                attempts=limit,
                route=route,
                mode=mode,
            ),
            dry_run=True,
        )
        return 0

    if success is None:
        log("未命中成功 workspace。")
        return 1

    log(f"完成: {success.workspace_id}，累计尝试 {success.attempts} 次。")
    notify_dingding(config, success, dry_run=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
