"""
ChatGPT / Codex 本地桌面端切号与状态查询。

当前实现面向本机 Electron 客户端 `Codex`，通过写入其 Chromium Cookies 数据库
完成 best-effort 本地登录态切换。
"""

from __future__ import annotations

import logging
import os
import platform
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from curl_cffi import requests as curl_requests

from core.desktop_apps import build_desktop_app_state

logger = logging.getLogger(__name__)

CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_PROBE_MODEL = "gpt-5.4"
CODEX_PROBE_VERSION = "0.125.0"
CODEX_PROBE_USER_AGENT = "codex_cli_rs/0.125.0 (Ubuntu 22.4.0; x86_64) xterm-256color"
CODEX_PROBE_INSTRUCTIONS = "You are Codex, a coding agent. Answer briefly."


def _build_proxies(proxy: Optional[str]) -> dict | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _header_value(headers: Any, key: str) -> str:
    if not headers:
        return ""
    for candidate in (key, key.lower(), key.title()):
        try:
            value = headers.get(candidate)
        except AttributeError:
            value = None
        if value not in (None, ""):
            return str(value)
    try:
        iterator = headers.items()
    except AttributeError:
        return ""
    key_lc = key.lower()
    for name, value in iterator:
        if str(name).lower() == key_lc and value not in (None, ""):
            return str(value)
    return ""


def _parse_codex_rate_limit_headers(headers: Any) -> dict[str, Any] | None:
    """解析 SUB2API 同款 Codex 响应头额度。"""

    fields = {
        "primary_used_percent": ("x-codex-primary-used-percent", _parse_float),
        "primary_reset_after_seconds": ("x-codex-primary-reset-after-seconds", _parse_int),
        "primary_window_minutes": ("x-codex-primary-window-minutes", _parse_int),
        "secondary_used_percent": ("x-codex-secondary-used-percent", _parse_float),
        "secondary_reset_after_seconds": ("x-codex-secondary-reset-after-seconds", _parse_int),
        "secondary_window_minutes": ("x-codex-secondary-window-minutes", _parse_int),
        "primary_over_secondary_percent": ("x-codex-primary-over-secondary-limit-percent", _parse_float),
    }
    snapshot: dict[str, Any] = {}
    for name, (header_name, parser) in fields.items():
        parsed = parser(_header_value(headers, header_name))
        if parsed is not None:
            snapshot[name] = parsed
    if not snapshot:
        return None
    snapshot["updated_at"] = _utc_iso(datetime.now(timezone.utc))
    return snapshot


def _normalize_codex_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """按窗口分钟数把 primary/secondary 归一到 5h/7d。"""

    if not snapshot:
        return None

    primary_mins = _parse_int(snapshot.get("primary_window_minutes"))
    secondary_mins = _parse_int(snapshot.get("secondary_window_minutes"))
    has_primary = primary_mins is not None
    has_secondary = secondary_mins is not None

    use_5h_from_primary = False
    use_7d_from_primary = False
    if has_primary and has_secondary:
        if int(primary_mins or 0) < int(secondary_mins or 0):
            use_5h_from_primary = True
        else:
            use_7d_from_primary = True
    elif has_primary:
        if int(primary_mins or 0) <= 360:
            use_5h_from_primary = True
        else:
            use_7d_from_primary = True
    elif has_secondary:
        if int(secondary_mins or 0) <= 360:
            use_7d_from_primary = True
        else:
            use_5h_from_primary = True
    else:
        use_7d_from_primary = True

    normalized: dict[str, Any] = {}
    if use_5h_from_primary:
        normalized.update(
            {
                "used_5h_percent": snapshot.get("primary_used_percent"),
                "reset_5h_seconds": snapshot.get("primary_reset_after_seconds"),
                "window_5h_minutes": snapshot.get("primary_window_minutes"),
                "used_7d_percent": snapshot.get("secondary_used_percent"),
                "reset_7d_seconds": snapshot.get("secondary_reset_after_seconds"),
                "window_7d_minutes": snapshot.get("secondary_window_minutes"),
            }
        )
    elif use_7d_from_primary:
        normalized.update(
            {
                "used_7d_percent": snapshot.get("primary_used_percent"),
                "reset_7d_seconds": snapshot.get("primary_reset_after_seconds"),
                "window_7d_minutes": snapshot.get("primary_window_minutes"),
                "used_5h_percent": snapshot.get("secondary_used_percent"),
                "reset_5h_seconds": snapshot.get("secondary_reset_after_seconds"),
                "window_5h_minutes": snapshot.get("secondary_window_minutes"),
            }
        )
    return {key: value for key, value in normalized.items() if value not in (None, "")}


def _codex_reset_at(base: datetime, seconds: Any) -> str | None:
    parsed = _parse_int(seconds)
    if parsed is None:
        return None
    return _utc_iso(base + timedelta(seconds=max(0, parsed)))


def _build_codex_usage_extra_updates(snapshot: dict[str, Any] | None, fallback_now: datetime | None = None) -> dict[str, Any]:
    if not snapshot:
        return {}
    now = fallback_now or datetime.now(timezone.utc)
    base_time = _parse_datetime(snapshot.get("updated_at")) or now
    updates: dict[str, Any] = {}

    # 保留原始响应头字段，便于前端日志或后续排错比对。
    raw_mapping = {
        "codex_primary_used_percent": "primary_used_percent",
        "codex_primary_reset_after_seconds": "primary_reset_after_seconds",
        "codex_primary_window_minutes": "primary_window_minutes",
        "codex_secondary_used_percent": "secondary_used_percent",
        "codex_secondary_reset_after_seconds": "secondary_reset_after_seconds",
        "codex_secondary_window_minutes": "secondary_window_minutes",
        "codex_primary_over_secondary_percent": "primary_over_secondary_percent",
    }
    for target, source in raw_mapping.items():
        if snapshot.get(source) not in (None, ""):
            updates[target] = snapshot[source]
    updates["codex_usage_updated_at"] = _utc_iso(base_time)

    normalized = _normalize_codex_snapshot(snapshot)
    if normalized:
        normalized_mapping = {
            "codex_5h_used_percent": "used_5h_percent",
            "codex_5h_reset_after_seconds": "reset_5h_seconds",
            "codex_5h_window_minutes": "window_5h_minutes",
            "codex_7d_used_percent": "used_7d_percent",
            "codex_7d_reset_after_seconds": "reset_7d_seconds",
            "codex_7d_window_minutes": "window_7d_minutes",
        }
        for target, source in normalized_mapping.items():
            if normalized.get(source) not in (None, ""):
                updates[target] = normalized[source]
        reset_5h_at = _codex_reset_at(base_time, normalized.get("reset_5h_seconds"))
        reset_7d_at = _codex_reset_at(base_time, normalized.get("reset_7d_seconds"))
        if reset_5h_at:
            updates["codex_5h_reset_at"] = reset_5h_at
        if reset_7d_at:
            updates["codex_7d_reset_at"] = reset_7d_at
    return updates


def _build_codex_usage_progress_from_extra(extra: dict[str, Any], window: str, now: datetime | None = None) -> dict[str, Any] | None:
    if not extra:
        return None
    now = now or datetime.now(timezone.utc)
    if window == "5h":
        used_key = "codex_5h_used_percent"
        reset_after_key = "codex_5h_reset_after_seconds"
        reset_at_key = "codex_5h_reset_at"
    elif window == "7d":
        used_key = "codex_7d_used_percent"
        reset_after_key = "codex_7d_reset_after_seconds"
        reset_at_key = "codex_7d_reset_at"
    else:
        return None

    used_percent = _parse_float(extra.get(used_key))
    if used_percent is None:
        return None

    reset_at = _parse_datetime(extra.get(reset_at_key))
    if reset_at is None:
        reset_after = _parse_int(extra.get(reset_after_key))
        if reset_after and reset_after > 0:
            base = _parse_datetime(extra.get("codex_usage_updated_at")) or now
            reset_at = base + timedelta(seconds=reset_after)

    remaining_seconds: int | None = None
    if reset_at is not None:
        remaining_seconds = max(0, int((reset_at - now).total_seconds()))
        if now >= reset_at:
            used_percent = 0.0

    used_percent = max(0.0, min(100.0, float(used_percent)))
    progress: dict[str, Any] = {
        "window": window,
        "utilization": used_percent,
        "used_percent": used_percent,
        "remaining_percent": max(0.0, 100.0 - used_percent),
    }
    if reset_at is not None:
        progress["resets_at"] = _utc_iso(reset_at)
    if remaining_seconds is not None:
        progress["remaining_seconds"] = remaining_seconds
    return progress


def _percent_text(value: Any) -> str:
    parsed = _parse_float(value)
    if parsed is None:
        return ""
    if parsed.is_integer():
        return f"{int(parsed)}%"
    return f"{parsed:.2f}".rstrip("0").rstrip(".") + "%"


def _codex_usage_breakdown(label: str, progress: dict[str, Any] | None) -> dict[str, Any] | None:
    if not progress:
        return None
    return {
        "display_name": label,
        "current_usage": _percent_text(progress.get("used_percent")),
        "usage_limit": "100%",
        "remaining_usage": _percent_text(progress.get("remaining_percent")),
        "next_reset_at": progress.get("resets_at", ""),
        "remaining_seconds": progress.get("remaining_seconds", ""),
    }


def _build_codex_probe_payload(model: str = CODEX_PROBE_MODEL) -> dict[str, Any]:
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ],
        "stream": True,
        "store": False,
        "instructions": CODEX_PROBE_INSTRUCTIONS,
    }


def _extract_codex_probe_updates(response: Any) -> dict[str, Any]:
    snapshot = _parse_codex_rate_limit_headers(getattr(response, "headers", None))
    if snapshot:
        return _build_codex_usage_extra_updates(snapshot, datetime.now(timezone.utc))
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        body = str(getattr(response, "text", "") or "")[:300]
        raise RuntimeError(f"openai codex probe returned status {status_code}: {body}")
    return {}


def _probe_codex_usage(
    *,
    access_token: str,
    account_id: str = "",
    proxy: str | None = None,
    model: str = CODEX_PROBE_MODEL,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按 SUB2API /usage active+force 逻辑，请求 Codex responses 并取额度响应头。"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "Originator": "codex_cli_rs",
        "Version": CODEX_PROBE_VERSION,
        "User-Agent": CODEX_PROBE_USER_AGENT,
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id

    started = time.monotonic()
    response = curl_requests.post(
        CODEX_RESPONSES_URL,
        headers=headers,
        json=_build_codex_probe_payload(model),
        proxies=_build_proxies(proxy),
        timeout=20,
        stream=True,
    )
    try:
        updates = _extract_codex_probe_updates(response)
        details = {
            "source": "active",
            "force": True,
            "url": CODEX_RESPONSES_URL,
            "model": model,
            "status_code": getattr(response, "status_code", None),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "account_id_present": bool(account_id),
            "proxy": proxy or "",
        }
        return updates, details
    finally:
        close_fn = getattr(response, "close", None)
        if callable(close_fn):
            close_fn()


def _extract_account_id_from_profile(profile: dict[str, Any]) -> str:
    for key in ("account_id", "chatgpt_account_id"):
        value = str(profile.get(key) or "").strip()
        if value:
            return value
    accounts = profile.get("accounts")
    if isinstance(accounts, list):
        for item in accounts:
            if not isinstance(item, dict):
                continue
            nested = item.get("account") if isinstance(item.get("account"), dict) else item
            for key in ("account_id", "chatgpt_account_id", "id"):
                value = str(nested.get(key) or "").strip()
                if value:
                    return value
    return ""


def _chromium_utc(dt: datetime) -> int:
    chromium_epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - chromium_epoch
    return int(delta.total_seconds() * 1_000_000)


def _cookie_targets(name: str) -> list[tuple[str, int]]:
    if name == "__Secure-next-auth.session-token":
        return [
            (".chatgpt.com", 1),
            ("chatgpt.com", 1),
            (".chat.openai.com", 1),
            ("chat.openai.com", 1),
        ]
    return [
        (".chatgpt.com", 0),
        ("chatgpt.com", 0),
    ]


def _parse_cookie_header(cookies: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in (cookies or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        parsed[name] = value.strip()
    return parsed


def extract_session_token(session_token: str = "", cookies: str = "") -> str:
    token = (session_token or "").strip()
    if token:
        return token
    return _parse_cookie_header(cookies).get("__Secure-next-auth.session-token", "")


def _get_codex_support_dir() -> str:
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Darwin":
        return os.path.join(home, "Library", "Application Support", "Codex")
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        return os.path.join(appdata, "Codex")
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    return os.path.join(config_home, "Codex")


def _get_codex_cookies_path() -> str:
    return os.path.join(_get_codex_support_dir(), "Cookies")


def _codex_install_paths() -> list[str]:
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Darwin":
        return [
            "/Applications/Codex.app",
            os.path.join(home, "Applications", "Codex.app"),
        ]
    if system == "Windows":
        localappdata = os.environ.get("LOCALAPPDATA", "")
        return [
            os.path.join(localappdata, "Programs", "Codex", "Codex.exe"),
            os.path.join(localappdata, "Codex", "Codex.exe"),
        ]
    return ["/usr/bin/codex", os.path.join(home, ".local", "bin", "codex")]


def _codex_process_patterns() -> list[str]:
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Darwin":
        return [
            "/Applications/Codex.app/Contents/MacOS/Codex",
            os.path.join(home, "Applications", "Codex.app", "Contents", "MacOS", "Codex"),
        ]
    if system == "Windows":
        return ["Codex.exe"]
    return ["codex"]


def close_codex_app() -> tuple[bool, str]:
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["osascript", "-e", 'quit app "Codex"'], capture_output=True, timeout=5)
            time.sleep(1.5)
            return True, "已尝试关闭 Codex"
        if system == "Windows":
            subprocess.run(
                ["taskkill", "/IM", "Codex.exe", "/F"],
                capture_output=True,
                creationflags=0x08000000,
                timeout=5,
            )
            time.sleep(1.5)
            return True, "已尝试关闭 Codex"
        subprocess.run(["pkill", "-f", "codex"], capture_output=True, timeout=5)
        time.sleep(1.5)
        return True, "已尝试关闭 Codex"
    except Exception as exc:
        logger.warning("关闭 Codex 失败: %s", exc)
        return False, f"关闭 Codex 失败: {exc}"


def restart_codex_app() -> tuple[bool, str]:
    system = platform.system()
    try:
        if system == "Darwin":
            if os.path.exists("/Applications/Codex.app"):
                subprocess.Popen(["open", "-a", "Codex"])
                return True, "Codex 已重启"
            return True, "未找到 /Applications/Codex.app，请手动启动 Codex"
        if system == "Windows":
            localappdata = os.environ.get("LOCALAPPDATA", "")
            for exe in (
                os.path.join(localappdata, "Programs", "Codex", "Codex.exe"),
                os.path.join(localappdata, "Codex", "Codex.exe"),
            ):
                if os.path.exists(exe):
                    subprocess.Popen([exe])
                    return True, "Codex 已重启"
            return True, "未找到 Codex.exe，请手动启动 Codex"
        for binary in ("/usr/bin/codex", os.path.expanduser("~/.local/bin/codex")):
            if os.path.exists(binary):
                subprocess.Popen([binary])
                return True, "Codex 已重启"
        subprocess.Popen(["codex"])
        return True, "Codex 已重启"
    except Exception as exc:
        logger.warning("启动 Codex 失败: %s", exc)
        return False, f"启动 Codex 失败: {exc}"


def switch_codex_account(session_token: str = "", cookies: str = "") -> tuple[bool, dict]:
    resolved_session = extract_session_token(session_token, cookies)
    if not resolved_session:
        return False, {"error": "缺少 __Secure-next-auth.session-token，无法切换本地 Codex 桌面端账号"}

    cookies_path = _get_codex_cookies_path()
    if not os.path.exists(cookies_path):
        return False, {"error": f"未找到 Codex Cookies 数据库: {cookies_path}"}

    cookie_map = _parse_cookie_header(cookies)
    cookie_map["__Secure-next-auth.session-token"] = resolved_session

    now = datetime.now(timezone.utc)
    creation_utc = _chromium_utc(now)
    expires_utc = _chromium_utc(now + timedelta(days=30))

    try:
        conn = sqlite3.connect(cookies_path, timeout=10)
        try:
            cursor = conn.cursor()
            for name, value in cookie_map.items():
                if not value:
                    continue
                for host_key, http_only in _cookie_targets(name):
                    cursor.execute(
                        """
                        DELETE FROM cookies
                        WHERE host_key = ? AND name = ? AND path = '/'
                        """,
                        (host_key, name),
                    )
                    cursor.execute(
                        """
                        INSERT INTO cookies (
                            creation_utc, host_key, top_frame_site_key, name, value, encrypted_value,
                            path, expires_utc, is_secure, is_httponly, last_access_utc, has_expires,
                            is_persistent, priority, samesite, source_scheme, source_port,
                            last_update_utc, source_type, has_cross_site_ancestor
                        ) VALUES (?, ?, '', ?, ?, ?, '/', ?, 1, ?, ?, 1, 1, 1, 0, 2, 443, ?, 1, 1)
                        """,
                        (
                            creation_utc,
                            host_key,
                            name,
                            value,
                            b"",
                            expires_utc,
                            http_only,
                            creation_utc,
                            creation_utc,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("写入 Codex Cookies 失败: %s", exc)
        return False, {"error": f"写入 Codex Cookies 失败: {exc}"}

    return True, {
        "message": "已写入 Codex 本地 Cookies，准备重启桌面端",
        "cookies_path": cookies_path,
        "cookie_names": sorted(cookie_map.keys()),
        "session_token_preview": _mask_secret(resolved_session),
    }


def read_current_codex_account() -> dict:
    cookies_path = _get_codex_cookies_path()
    if not os.path.exists(cookies_path):
        return {"available": False, "cookies_path": cookies_path}

    try:
        conn = sqlite3.connect(cookies_path, timeout=10)
        try:
            cursor = conn.cursor()
            rows = cursor.execute(
                """
                SELECT host_key, name, value
                FROM cookies
                WHERE name IN ('__Secure-next-auth.session-token', 'oai-did')
                ORDER BY host_key, name
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("读取 Codex Cookies 失败: %s", exc)
        return {
            "available": True,
            "cookies_path": cookies_path,
            "error": str(exc),
        }

    session_token = ""
    cookies_found = []
    for host_key, name, value in rows:
        if name == "__Secure-next-auth.session-token" and value and not session_token:
            session_token = value
        cookies_found.append({
            "host": host_key,
            "name": name,
            "value_preview": _mask_secret(value),
        })
    return {
        "available": True,
        "cookies_path": cookies_path,
        "session_token_present": bool(session_token),
        "session_token_preview": _mask_secret(session_token),
        "cookies": cookies_found,
    }


def get_codex_desktop_state() -> dict:
    cookies_path = _get_codex_cookies_path()
    current = read_current_codex_account()
    state = build_desktop_app_state(
        app_id="codex",
        app_name="Codex",
        process_patterns=_codex_process_patterns(),
        install_paths=_codex_install_paths(),
        binary_names=["codex"],
        config_paths=[_get_codex_support_dir(), cookies_path],
        current_account_present=bool((current or {}).get("session_token_present")),
        extra={
            "cookies_path": cookies_path,
        },
    )
    state["available"] = True
    return state


def _fetch_profile(access_token: str, proxy: str | None = None) -> tuple[bool, dict]:
    if not access_token:
        return False, {}
    try:
        response = curl_requests.get(
            "https://chatgpt.com/backend-api/me",
            headers={
                "authorization": f"Bearer {access_token}",
                "accept": "application/json",
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            },
            proxies=_build_proxies(proxy),
            timeout=20,
            impersonate="chrome124",
        )
        if response.status_code == 200:
            return True, response.json()
        return False, {"status_code": response.status_code, "body": response.text[:400]}
    except Exception as exc:
        return False, {"error": str(exc)}


def fetch_chatgpt_account_state(
    *,
    access_token: str = "",
    session_token: str = "",
    cookies: str = "",
    proxy: str | None = None,
    chatgpt_account_id: str = "",
    existing_extra: dict[str, Any] | None = None,
    force_usage: bool = True,
) -> dict:
    state = {
        "platform": "chatgpt",
        "desktop_app": "Codex",
        "session_token_present": bool(extract_session_token(session_token, cookies)),
        "quota_note": "Codex 额度按 SUB2API active+force 方式探测：请求 Codex responses 后解析 x-codex-* 响应头。",
    }
    usage_extra = dict(existing_extra or {})

    resolved_session = extract_session_token(session_token, cookies)
    resolved_access = access_token
    resolved_account_id = str(chatgpt_account_id or usage_extra.get("account_id") or usage_extra.get("chatgpt_account_id") or "").strip()
    token_refresh_attempted = False

    def _refresh_access_from_session() -> bool:
        nonlocal resolved_access, token_refresh_attempted
        if not resolved_session:
            return False
        token_refresh_attempted = True
        try:
            from platforms.chatgpt.token_refresh import TokenRefreshManager

            manager = TokenRefreshManager(proxy_url=proxy)
            refresh = manager.refresh_by_session_token(resolved_session)
            if refresh.success:
                resolved_access = refresh.access_token
                state["access_token"] = refresh.access_token
                return True
            state["token_refresh_error"] = refresh.error_message
            return False
        except Exception as exc:
            state["token_refresh_error"] = str(exc)
            return False

    if not resolved_access:
        _refresh_access_from_session()

    if resolved_access:
        ok, profile = _fetch_profile(resolved_access, proxy=proxy)
        if not ok and resolved_session and not token_refresh_attempted:
            if _refresh_access_from_session():
                ok, profile = _fetch_profile(resolved_access, proxy=proxy)
        state["valid"] = ok
        if ok:
            state["profile"] = profile
            state["remote_user"] = profile
            profile_account_id = _extract_account_id_from_profile(profile)
            if profile_account_id and not resolved_account_id:
                resolved_account_id = profile_account_id
            if resolved_account_id:
                state["account_id"] = resolved_account_id
            try:
                from platforms.chatgpt.payment import check_subscription_status

                class _A:
                    pass

                account = _A()
                account.access_token = resolved_access
                account.cookies = cookies
                state["subscription_status"] = check_subscription_status(account, proxy=proxy)
            except Exception as exc:
                state["subscription_error"] = str(exc)

            if force_usage:
                try:
                    updates, probe_details = _probe_codex_usage(
                        access_token=resolved_access,
                        account_id=resolved_account_id,
                        proxy=proxy,
                    )
                    state["codex_usage_probe"] = probe_details
                    if updates:
                        usage_extra.update(updates)
                        state["codex_usage_extra"] = updates
                except Exception as exc:
                    state["codex_usage_error"] = str(exc)

            five_hour = _build_codex_usage_progress_from_extra(usage_extra, "5h")
            seven_day = _build_codex_usage_progress_from_extra(usage_extra, "7d")
            if five_hour or seven_day:
                codex_usage = {
                    "source": "active" if state.get("codex_usage_probe") else "cached",
                    "updated_at": usage_extra.get("codex_usage_updated_at", ""),
                    "five_hour": five_hour,
                    "seven_day": seven_day,
                }
                state["codex_usage"] = codex_usage
                breakdowns = [
                    item
                    for item in (
                        _codex_usage_breakdown("Codex 5h", five_hour),
                        _codex_usage_breakdown("Codex 7d", seven_day),
                    )
                    if item
                ]
                if breakdowns:
                    state["usage_breakdowns"] = breakdowns
                if five_hour:
                    state["prompt_remaining_percent"] = five_hour.get("remaining_percent")
                    state["next_reset_at"] = five_hour.get("resets_at", "")
        else:
            state["profile_error"] = profile
    else:
        state["valid"] = False
        state["profile_error"] = "缺少 access_token，且无法通过 session_token 刷新"

    return state
