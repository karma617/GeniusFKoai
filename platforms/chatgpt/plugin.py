"""ChatGPT / Codex CLI 平台插件"""
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from core.proxy_pool import get_proxy_runtime_config, proxy_pool, resolve_runtime_proxy
from platforms._browser_backend import BrowserBackendConfig


def _result_text(result, key: str) -> str:
    if isinstance(result, dict):
        return str(result.get(key, "") or "")
    return str(getattr(result, key, "") or "")


def _assert_complete_oauth_callback(result) -> None:
    # NextAuth 流程只返回 account_id + access_token (+ session_token)
    # 传统 Codex CLI 流程返回全部 4 个字段
    required = ("account_id", "access_token")
    missing = [key for key in required if not _result_text(result, key)]
    if missing:
        raise RuntimeError(
            "ChatGPT 注册未完成完整 OAuth callback，缺少: " + ", ".join(missing)
        )


def _bool_param(params: dict, key: str, default: bool) -> bool:
    value = params.get(key)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _int_param(params: dict, key: str, default: int) -> int:
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _optional_int_param(params: dict, key: str) -> int | None:
    value = params.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_dict(value):
    return value if isinstance(value, dict) else {}


def _registration_mode_metadata(executor_type: str | None, browser_mode: str | None = "") -> dict:
    normalized = str(executor_type or "protocol").strip().lower() or "protocol"
    if normalized == "headless":
        mode = "headless_browser"
        label = "无头浏览器"
    elif normalized == "headed":
        mode = "headed_browser"
        label = "有头浏览器"
    else:
        mode = "protocol"
        label = "协议模式"
    return {
        "registration_mode": mode,
        "registration_mode_label": label,
        "registration_executor_type": normalized,
        "browser_mode": str(browser_mode or "").strip(),
    }


def _first_non_empty(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _account_secret_value(account: Account, key: str) -> str:
    extra = _safe_dict(getattr(account, "extra", None))
    overview = _safe_dict(extra.get("account_overview"))
    legacy_extra = _safe_dict(overview.get("legacy_extra"))
    return _first_non_empty(extra.get(key), overview.get(key), legacy_extra.get(key))


def _mask_proxy(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value or "@" not in value:
        return value
    prefix, _, host = value.rpartition("@")
    scheme, sep, _credentials = prefix.partition("://")
    return f"{scheme}{sep}***@{host}" if sep else f"***@{host}"


def _proxy_log_value(proxy: str | None) -> str:
    """代理日志脱敏；空值显示为直连。"""
    return _mask_proxy(proxy) if str(proxy or "").strip() else "直连"


def _chatgpt_token_backup_dir() -> Path:
    """ChatGPT OAuth token 本地备份目录。"""
    return Path(__file__).resolve().parents[2] / "data" / "chatgpt_token_backups"


def _safe_backup_filename_part(value: str, fallback: str = "account") -> str:
    text = re.sub(r"[^A-Za-z0-9._@+-]+", "_", str(value or "").strip()).strip("._-")
    return text[:80] or fallback


def _save_get_rt_token_backup(account: Account, result: dict, *, action_label: str = "get_rt") -> str:
    """保存一次 OAuth token 交换结果，便于本地追溯与恢复。"""
    if not isinstance(result, dict):
        return ""
    if not (str(result.get("access_token") or "").strip() or str(result.get("refresh_token") or "").strip()):
        return ""

    saved_at = datetime.now(timezone.utc).isoformat()
    email = str(result.get("email") or account.email or "").strip()
    account_id = str(result.get("account_id") or account.user_id or "").strip()
    backup_dir = _chatgpt_token_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{_safe_backup_filename_part(email)}.json"
    backup_path = backup_dir / filename
    payload = {
        "saved_at": saved_at,
        "action": action_label,
        "platform": "chatgpt",
        "email": email,
        "account_id": account_id,
        "tokens": {
            "access_token": str(result.get("access_token") or ""),
            "refresh_token": str(result.get("refresh_token") or ""),
            "id_token": str(result.get("id_token") or ""),
        },
        "result": result,
    }
    backup_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return str(backup_path)


def _is_chatgpt_deleted_or_deactivated_error(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "deleted or deactivated",
            "account has been deleted",
            "account was deleted",
            "account is disabled",
            "account disabled",
            "account suspended",
            "suspended",
            "banned",
        )
    )


def _is_validate_otp_http_403_error(message: str) -> bool:
    text = str(message or "").lower()
    return "validate_otp_http_403_body" in text or "email_otp_validate_http_403" in text


def _is_cloudflare_managed_challenge_error(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    return (
        "platform_authorize_cloudflare_managed_challenge" in text
        or (
            "just a moment" in text
            and "challenges.cloudflare.com" in text
            and "window._cf_chl_opt" in text
            and "managed" in text
        )
    )


def _is_chatgpt_session_stale_error(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "invalid_state",
            "sign-in session is no longer valid",
            "session is no longer valid",
            "session expired",
            "session token is invalid",
            "refresh token is invalid",
            "unauthorized",
            "invalid or expired token",
        )
    )


def _refresh_chatgpt_session_after_get_rt(account: Account, *, proxy: str | None = None) -> dict:
    class _A:
        pass

    extra = account.extra or {}
    target = _A()
    target.email = account.email
    target.access_token = extra.get("access_token") or account.token
    target.refresh_token = extra.get("refresh_token", "")
    target.session_token = extra.get("session_token", "")
    target.cookies = extra.get("cookies", "")
    from .constants import OAUTH_CLIENT_ID

    target.client_id = extra.get("client_id") or extra.get("clientId") or OAUTH_CLIENT_ID

    from platforms.chatgpt.token_refresh import TokenRefreshManager

    manager = TokenRefreshManager(proxy_url=proxy)
    result = manager.refresh_account(target)
    if not result.success:
        return {"ok": False, "error": result.error_message, "error_type": "session_refresh_failed"}
    data = {
        "access_token": result.access_token,
        "refresh_token": result.refresh_token,
        "session_token": target.session_token,
        "account_state": None,
    }
    try:
        from platforms.chatgpt.switch import fetch_chatgpt_account_state

        data["account_state"] = fetch_chatgpt_account_state(
            access_token=result.access_token,
            session_token=target.session_token,
            cookies=target.cookies,
            proxy=proxy,
        )
    except Exception:
        pass
    return {"ok": True, "data": data}


def _resolve_action_proxy(
    configured_proxy: str | None,
    *,
    region: str = "",
    log_fn=None,
    action_label: str = "操作",
) -> str | None:
    """按代理页配置解析平台动作代理，并输出可排查日志。"""
    try:
        runtime_config = get_proxy_runtime_config()
    except Exception as exc:
        runtime_config = {
            "strategy": "fallback",
            "fallback_url": configured_proxy or "",
        }
        if callable(log_fn):
            log_fn(f"  {action_label}: 代理配置读取失败，改用降级策略: {exc}")
    try:
        resolved = resolve_runtime_proxy(
            explicit_proxy=configured_proxy,
            proxy_getter=lambda: proxy_pool.get_next(region=region),
            region=region,
        )
    except Exception as exc:
        resolved = str(configured_proxy or runtime_config.get("fallback_url") or "").strip() or None
        if callable(log_fn):
            log_fn(f"  {action_label}: 代理解析失败，改用 fallback={_proxy_log_value(resolved)}: {exc}")
    if callable(log_fn):
        source = "显式代理" if str(configured_proxy or "").strip() else (
            f"全局策略={runtime_config['strategy']}"
        )
        log_fn(
            f"  {action_label}: 代理解析 {source} "
            f"fallback={_proxy_log_value(runtime_config.get('fallback_url'))} "
            f"region={region or '(未指定)'} actual={_proxy_log_value(resolved)}"
        )
    return resolved


def _build_checkout_har_path(email: str) -> str:
    """为 Camoufox checkout 生成 HAR 文件路径：tools/captures/checkout-<ts>-<email-slug>.har"""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    capture_dir = os.path.join(project_root, "tools", "captures")
    os.makedirs(capture_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(email or "anon")).strip("_") or "anon"
    return os.path.join(capture_dir, f"checkout-{timestamp}-{slug}.har")


def _build_get_rt_har_path(email: str) -> str:
    """Build a HAR output path for get_rt Camoufox OAuth captures."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    capture_dir = os.path.join(project_root, "tools", "captures")
    os.makedirs(capture_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(email or "anon")).strip("_") or "anon"
    return os.path.join(capture_dir, f"get-rt-{timestamp}-{slug}.har")


def _normalize_get_rt_sms_provider(value) -> str:
    """规范化 get_rt 手机接码 provider：空值走默认，none/off 才显式禁用。"""
    provider = str(value or "").strip().lower()
    if provider in {"none", "off", "disabled", "disable", "false", "0", "不启用"}:
        return ""
    return provider or "default"


def _run_sync_checkout_isolated(checkout_fn, **kwargs):
    """把 checkout 函数丢进独立线程跑，避免阻塞外层 asyncio loop / 任务线程。

    **subtask 标签透传**：外层 ``logger.log`` 用 thread-local 标签把日志
    分组到对应的 worker（前端按这个折叠）。子线程是新线程，thread-local
    天然是空的，所以这里在父线程从 ``log_fn`` 上抠出当前绑定的
    subtask（如果是 ``TaskLogger.log``），子线程进去再 set 一遍，最后
    finally 清掉。
    """
    result_box = {}
    error_box = {}

    # 尝试从 log_fn 上抠出 TaskLogger 实例和当前 subtask（best-effort）
    log_fn = kwargs.get("log_fn")
    parent_logger = getattr(log_fn, "__self__", None)
    parent_subtask: tuple[str, str] | None = None
    if parent_logger is not None and hasattr(parent_logger, "_current_subtask"):
        try:
            parent_subtask = parent_logger._current_subtask()
        except Exception:
            parent_subtask = None

    def _target():
        # 把父线程的 subtask 标签复制到子线程的 thread-local，确保子线程里
        # 调 ``logger.log`` 也能正确分组。
        if parent_logger is not None and parent_subtask and parent_subtask[0]:
            try:
                parent_logger.set_subtask(parent_subtask[0], parent_subtask[1])
            except Exception:
                pass
        try:
            result_box["result"] = checkout_fn(**kwargs)
        except BaseException as exc:
            error_box["error"] = exc
        finally:
            if parent_logger is not None and parent_subtask and parent_subtask[0]:
                try:
                    parent_logger.clear_subtask()
                except Exception:
                    pass

    thread = threading.Thread(target=_target, name="chatgpt-paypal-checkout")
    thread.start()
    thread.join()
    if error_box:
        raise error_box["error"]
    return result_box.get("result")


def _run_sync_browser_register_isolated(worker, *, email: str, password: str):
    """在 asyncio loop 内调用同步 Playwright 注册器时切到独立线程。"""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        return worker.run(email=email, password=password)

    log_fn = getattr(worker, "log", None)
    if callable(log_fn):
        log_fn("浏览器注册检测到 asyncio loop，已切到独立线程执行同步 Playwright")

    result_box = {}
    error_box = {}
    parent_logger = getattr(log_fn, "__self__", None)
    parent_subtask: tuple[str, str] | None = None
    if parent_logger is not None and hasattr(parent_logger, "_current_subtask"):
        try:
            parent_subtask = parent_logger._current_subtask()
        except Exception:
            parent_subtask = None

    def _target():
        if parent_logger is not None and parent_subtask and parent_subtask[0]:
            try:
                parent_logger.set_subtask(parent_subtask[0], parent_subtask[1])
            except Exception:
                pass
        try:
            result_box["result"] = worker.run(email=email, password=password)
        except BaseException as exc:
            error_box["error"] = exc
        finally:
            if parent_logger is not None and parent_subtask and parent_subtask[0]:
                try:
                    parent_logger.clear_subtask()
                except Exception:
                    pass

    thread = threading.Thread(target=_target, name="chatgpt-browser-register")
    thread.start()
    thread.join()
    if error_box:
        raise error_box["error"]
    return result_box.get("result")


def _generate_chatgpt_registration_password(length: int = 16) -> str:
    """生成更稳定通过 OpenAI 注册页校验的密码。

    旧协议流已经验证过：至少带小写、数字、符号时，成功率明显更稳。
    这里再补一个大写字符，避免浏览器流随机生成出“看起来够长但组合不够强”的密码。
    """
    specials = "!"
    minimum_length = 12
    size = max(int(length or minimum_length), minimum_length)
    required = [
        secrets.choice("abcdefghijklmnopqrstuvwxyz"),
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        secrets.choice("0123456789"),
        secrets.choice(specials),
    ]
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    required.extend(secrets.choice(pool) for _ in range(size - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser", "sms_oauth"]
    supported_oauth_providers = ["google", "microsoft"]
    protocol_captcha_order = ("yescaptcha_api", "twocaptcha_api", "local_solver")

    # Declarative capabilities
    capabilities = [
        "query_state",      # Query account state/quota
        "refresh_token",    # Refresh auth token
        "generate_link",    # Generate payment link
        "switch_desktop",   # Switch to Codex desktop
        "upload_cpa",       # Upload to CPA system
        "upload_sub2api",   # Upload to SUB2API
        "upload_tm",        # Upload to Team Manager
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        self._last_check_overview = {}
        try:
            from platforms.chatgpt.payment import fetch_subscription_status_details
            class _A: pass
            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.id_token = extra.get("id_token", "")
            a.cookies = extra.get("cookies", "")
            a.extra = extra

            region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
            # 账号检测同样遵守代理资源页策略，避免代理池为空时误走本机直连。
            proxy = _resolve_action_proxy(
                self.config.proxy if self.config else None,
                region=region,
                log_fn=None,
                action_label="账号检测",
            )

            details = fetch_subscription_status_details(a, proxy=proxy)
            if proxy:
                try:
                    proxy_pool.report_success(proxy)
                except Exception:
                    pass
            status = details.get("status")
            # 把订阅状态同步映射成前端能用的 plan_state / chips
            # 来源（避免老 chips 还带 "Plus" 但实际已 free）。
            if status == "plus":
                plan_state = "subscribed"
                chips = ["Plus"]
            elif status == "team":
                plan_state = "subscribed"
                chips = ["Team"]
            elif status == "free":
                plan_state = "free"
                chips = ["Free"]
            elif status in ("expired", "invalid", "banned"):
                plan_state = "expired"
                chips = []
            else:
                plan_state = "unknown"
                chips = []
            overview = {
                "plan": status,
                "plan_name": status,
                "plan_state": plan_state,
                "chips": chips,
                "check_source": details.get("source"),
            }
            if isinstance(details.get("usage"), dict):
                overview["chatgpt_usage"] = details["usage"]
            self._last_check_overview = overview
            return status not in ("expired", "invalid", "banned", None)
        except Exception:
            try:
                if "proxy" in locals() and proxy:
                    proxy_pool.report_fail(proxy)
            except Exception:
                pass
            return False
        return False

    def get_last_check_overview(self) -> dict:
        return dict(getattr(self, "_last_check_overview", {}) or {})

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        return _generate_chatgpt_registration_password()

    def _map_chatgpt_result(
        self,
        result: dict,
        *,
        password: str = "",
        user_id: str = "",
        require_oauth: bool = False,
        executor_type: str | None = None,
        browser_mode: str | None = "",
        set_password_requested: bool = False,
    ) -> RegistrationResult:
        if require_oauth:
            _assert_complete_oauth_callback(result)
        refresh_token_source = str(result.get("refresh_token_source") or "")
        oauth_refresh_token = str(result.get("refresh_token") or "")
        raw_refresh_token = str(
            oauth_refresh_token
            or result.get("registration_refresh_token")
            or ""
        )
        refresh_token = oauth_refresh_token if require_oauth else ""
        password_set_after_register = bool(result.get("password_set_after_register", False))
        post_register_password_error = str(result.get("post_register_password_error", "") or "")
        manual_post_register_capture = bool(result.get("manual_post_register_capture", False))
        if (
            set_password_requested
            and not password_set_after_register
            and not post_register_password_error
            and not manual_post_register_capture
        ):
            post_register_password_error = "browser_post_register_password_not_supported"
        account_overview = _registration_mode_metadata(executor_type, browser_mode)
        mfa_enabled = bool(result.get("mfa_enabled", False))
        if mfa_enabled:
            chips = list(account_overview.get("chips") or [])
            if "2FA已绑" not in [str(item) for item in chips]:
                chips.append("2FA已绑")
            account_overview.update(
                {
                    "mfa_enabled": True,
                    "mfa_type": str(result.get("mfa_type") or "totp"),
                    "mfa_factor_id": str(result.get("mfa_factor_id") or ""),
                    "chips": chips,
                }
            )
        elif result.get("mfa_error"):
            account_overview.update({"mfa_enabled": False, "mfa_error": str(result.get("mfa_error") or "")[:240]})
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=user_id or result.get("account_id", ""),
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "account_id": result.get("account_id", ""),
                "access_token": result.get("access_token", ""),
                "refresh_token": refresh_token,
                "registration_refresh_token": raw_refresh_token,
                "refresh_token_source": refresh_token_source if require_oauth and refresh_token else "",
                "registration_refresh_token_usable": False,
                "id_token": result.get("id_token", ""),
                "session_token": result.get("session_token", ""),
                "workspace_id": result.get("workspace_id", ""),
                "cookies": result.get("cookies", ""),
                "login_state_cookie": result.get("login_state_cookie") or result.get("cookies", ""),
                "profile": result.get("profile", {}),
                "expires_at": result.get("expires_at", ""),
                "session": result.get("session", {}),
                "oauth_error": result.get("oauth_error", ""),
                "chatgpt_user_agent": result.get("chatgpt_user_agent", ""),
                "chatgpt_accept_language": result.get("chatgpt_accept_language", ""),
                "chatgpt_oai_client_version": result.get("chatgpt_oai_client_version", ""),
                "chatgpt_oai_client_build_number": result.get("chatgpt_oai_client_build_number", ""),
                "chatgpt_oai_device_id": result.get("chatgpt_oai_device_id", ""),
                "chatgpt_oai_session_id": result.get("chatgpt_oai_session_id", ""),
                "password_set_after_register": password_set_after_register,
                "post_register_password_error": post_register_password_error,
                "mfa_enabled": mfa_enabled,
                "mfa_type": str(result.get("mfa_type") or "") if result.get("mfa_type") else "",
                "mfa_error": str(result.get("mfa_error") or ""),
                "mfa_factor_id": str(result.get("mfa_factor_id") or ""),
                "mfa_session_id": str(result.get("mfa_session_id") or ""),
                "mfa_info": result.get("mfa_info", {}),
                "totp_secret": str(result.get("totp_secret") or ""),
                "account_overview": account_overview,
                # 短链物理复用：浏览器内 PayPal checkout 结果透传给上层任务判定。
                "_shortlink_checkout": result.get("_shortlink_checkout", None),
                "record_har_path": result.get("record_har_path", ""),
                "manual_post_register_capture": manual_post_register_capture,
                "manual_post_register_har_path": result.get("manual_post_register_har_path", ""),
                "manual_post_register_cancelled": bool(result.get("manual_post_register_cancelled", False)),
            },
        )

    def _run_protocol_oauth(self, ctx) -> dict:
        from platforms.chatgpt.browser_oauth import register_with_browser_oauth

        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=ctx.identity.oauth_provider,
            email_hint=ctx.identity.email,
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
        )

    def build_browser_registration_adapter(self):
        def _build_post_register_in_browser(ctx, artifacts):
            extra = ctx.extra or {}
            original_callback = extra.get("_post_register_in_browser")
            set_password = _bool_param(extra, "set_password_after_register", False)
            enable_2fa = _bool_param(extra, "enable_2fa_after_register", False)
            manual_capture = _bool_param(extra, "_manual_post_register_security_capture", False)
            if not (set_password or enable_2fa):
                return original_callback
            if manual_capture:
                task_id = str(extra.get("_task_id") or "").strip()
                finish_path = Path(
                    str(extra.get("_manual_post_register_capture_finish_path") or "").strip()
                    or f"tools/captures/manual-post-register-finish-{task_id or 'task'}.signal"
                )
                cancel_check = extra.get("_cancel_check")

                def _manual_callback(_page, result):
                    from platforms.chatgpt.browser_register import collect_post_register_security_state_from_browser

                    har_path = str((result or {}).get("record_har_path") or "").strip()
                    ctx.log("注册已完成，浏览器和 HAR 录制保持打开")
                    ctx.log("请在当前有头浏览器里手动完成设置帐号密码和绑定 2FA")
                    if har_path:
                        ctx.log(f"当前后置抓包 HAR: {har_path}")
                    if task_id:
                        ctx.log(
                            "手动操作完成后调用: "
                            f"POST /api/tasks/{task_id}/manual-post-register-capture/finish"
                        )
                    ctx.log(f"完成信号文件: {finish_path}")
                    last_notice = 0.0
                    while True:
                        if finish_path.exists():
                            try:
                                finish_path.unlink()
                            except Exception:
                                pass
                            ctx.log("已收到手动后置抓包完成信号，正在停止 HAR 录制并返回注册结果")
                            refreshed = collect_post_register_security_state_from_browser(
                                _page,
                                dict(result or {}),
                                ctx.log,
                            )
                            refreshed.update({
                                "manual_post_register_capture": True,
                                "manual_post_register_har_path": har_path,
                            })
                            return refreshed
                        if callable(cancel_check) and cancel_check():
                            ctx.log("检测到任务取消请求，停止手动后置 HAR 录制等待")
                            return {
                                "manual_post_register_capture": True,
                                "manual_post_register_cancelled": True,
                                "manual_post_register_har_path": har_path,
                            }
                        now = time.time()
                        if now - last_notice >= 30:
                            last_notice = now
                            ctx.log("手动后置抓包等待中：完成密码/2FA 操作后发送完成信号")
                        time.sleep(1.0)

                return _manual_callback

            def _callback(page, result):
                from platforms.chatgpt.browser_register import run_post_register_security_in_browser

                merged = {}
                security_extra = run_post_register_security_in_browser(
                    page,
                    dict(result or {}),
                    password=str((result or {}).get("password") or getattr(ctx, "password", "") or ""),
                    set_password=set_password,
                    enable_2fa=enable_2fa,
                    otp_callback=getattr(artifacts, "otp_callback", None),
                    log_fn=ctx.log,
                )
                if isinstance(security_extra, dict):
                    merged.update(security_extra)
                if callable(original_callback):
                    callback_extra = original_callback(page, {**dict(result or {}), **merged})
                    if isinstance(callback_extra, dict):
                        merged.update(callback_extra)
                return merged

            return _callback

        def _build_browser_worker(ctx, artifacts):
            browser_register = __import__(
                "platforms.chatgpt.browser_register",
                fromlist=["ChatGPTBrowserRegister"],
            )
            return browser_register.ChatGPTBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                phone_callback=artifacts.phone_callback,
                log_fn=ctx.log,
                # 短链复用流程：通过 RegisterConfig.extra 注入 backend_config
                # （决定注册用 Camoufox 还是 BitBrowser）和 post_register_in_browser
                # 回调（注册完不关浏览器，在同一 page 里执行后置任务）。
                # 普通注册这两个 key 不存在，行为不变（默认 Camoufox、无回调）。
                backend_config=(ctx.extra or {}).get("_reuse_backend_config"),
                post_register_in_browser=_build_post_register_in_browser(ctx, artifacts),
                phone_first_oauth=getattr(ctx.identity, "identity_provider", "") == "sms_oauth",
                bind_email_after_phone_signup=bool(
                    (ctx.extra or {}).get("phone_signup_relogin_after_bind_email", True)
                ),
                record_har=_bool_param(ctx.extra, "record_har", False)
                or _bool_param(ctx.extra or {}, "_manual_post_register_security_capture", False),
                phone_change_limit=max(_int_param(ctx.extra or {}, "phone_change_limit", 10), 1),
                phone_first_full_rounds=_int_param(ctx.extra or {}, "phone_first_full_rounds", 0) or None,
            )

        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                require_oauth=getattr(ctx.identity, "identity_provider", "")
                in {"oauth_browser", "sms_oauth"},
                executor_type=getattr(ctx, "executor_type", None),
                browser_mode=(getattr(ctx, "extra", {}) or {}).get("browser_mode", ""),
                set_password_requested=_bool_param(getattr(ctx, "extra", {}) or {}, "set_password_after_register", False),
            ),
            browser_worker_builder=_build_browser_worker,
            browser_register_runner=lambda worker, ctx, artifacts: _run_sync_browser_register_isolated(
                worker,
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            oauth_runner=self._run_protocol_oauth,
            capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
            otp_spec=OtpSpec(wait_message="等待验证码...", timeout=30),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_protocol_oauth,
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                user_id=result.get("account_id", ""),
                require_oauth=True,
                executor_type=getattr(ctx, "executor_type", None),
                browser_mode=(getattr(ctx, "extra", {}) or {}).get("browser_mode", ""),
            ),
        )

    def build_protocol_mailbox_adapter(self):
        def _use_authflow_experimental(ctx) -> bool:
            extra = ctx.extra or {}
            variant = str(
                extra.get("chatgpt_protocol_variant")
                or extra.get("protocol_variant")
                or ""
            ).strip().lower()
            if variant in {"authflow_experimental", "outlook_authflow", "gpt_outlook_authflow"}:
                return True
            return _bool_param(extra, "chatgpt_authflow_experimental", False)

        def _build_worker(ctx, artifacts):
            if getattr(ctx.identity, "identity_provider", "") == "sms_oauth":
                from platforms.chatgpt.protocol_sms_oauth import ChatGPTProtocolSmsOAuthWorker

                return ChatGPTProtocolSmsOAuthWorker(
                    phone_callback=artifacts.phone_callback,
                    proxy_url=ctx.proxy,
                    log_fn=ctx.log,
                    phone_change_limit=max(_int_param(ctx.extra or {}, "phone_change_limit", 10), 1),
                )

            if _use_authflow_experimental(ctx):
                from platforms.chatgpt.protocol_authflow import ChatGPTAuthFlowProtocolWorker

                ctx.log("启用实验 AuthFlow 协议注册链路（gpt-outlook-register 移植版）")
                return ChatGPTAuthFlowProtocolWorker(
                    mailbox=self.mailbox,
                    mailbox_account=ctx.identity.mailbox_account,
                    proxy_url=ctx.proxy,
                    log_fn=ctx.log,
                    metadata_extra={"protocol_variant": "authflow_experimental"},
                )

            from platforms.chatgpt.protocol_mailbox import ChatGPTProtocolMailboxWorker

            return ChatGPTProtocolMailboxWorker(
                mailbox=self.mailbox,
                mailbox_account=ctx.identity.mailbox_account,
                provider=(self.config.extra or {}).get("mail_provider", ""),
                proxy_url=ctx.proxy,
                log_fn=ctx.log,
                initial_before_ids=(
                    None
                    if (getattr(ctx.identity, "metadata", {}) or {}).get("mailbox_baseline_error")
                    else set(getattr(ctx.identity, "before_ids", set()) or set())
                ),
                skip_post_register_oauth=_bool_param(ctx.extra or {}, "k12_join", False),
                k12_workspace_ids=str((ctx.extra or {}).get("k12_workspace_ids", "") or "").strip(),
                remote_upload_enabled=_bool_param(ctx.extra or {}, "remote_upload_enabled", False),
                k12_batch_upload_enabled=_bool_param(ctx.extra or {}, "k12_batch_upload_enabled", True),
                set_password_after_register=_bool_param(ctx.extra or {}, "set_password_after_register", False),
            )

        def _map_result(ctx, result):
            is_sms_oauth = getattr(getattr(ctx, "identity", None), "identity_provider", "") == "sms_oauth"
            if not is_sms_oauth:
                _assert_complete_oauth_callback(result)
            access_token = result.access_token or ""
            refresh_token = result.refresh_token or ""
            session_token = result.session_token or ""
            metadata = getattr(result, "metadata", None) or {}
            registration_refresh_token = str(
                refresh_token
                or metadata.get("registration_refresh_token")
                or ""
            )
            refresh_token_source = (
                str(metadata.get("refresh_token_source") or "")
                if is_sms_oauth and refresh_token
                else ""
            )

            return RegistrationResult(
                email=result.email,
                password=result.password or (ctx.password or ""),
                user_id=result.account_id,
                token=access_token,
                status=AccountStatus.REGISTERED,
                extra={
                    "account_overview": _registration_mode_metadata(
                        getattr(ctx, "executor_type", "protocol"),
                        (getattr(ctx, "extra", {}) or {}).get("browser_mode", ""),
                    ),
                    "access_token": access_token,
                    "refresh_token": "",
                    "registration_refresh_token": registration_refresh_token,
                    "refresh_token_source": refresh_token_source,
                    "registration_refresh_token_usable": False,
                    "id_token": result.id_token,
                    "session_token": session_token,
                    "workspace_id": result.workspace_id,
                    "cookies": metadata.get("cookies", ""),
                    "login_state_cookie": metadata.get("login_state_cookie") or metadata.get("cookies", ""),
                    "profile": metadata.get("profile", {}),
                    "expires_at": metadata.get("expires_at", ""),
                    "session": metadata.get("session", {}),
                    "chatgpt_user_agent": metadata.get("chatgpt_user_agent", ""),
                    "chatgpt_accept_language": metadata.get("chatgpt_accept_language", ""),
                    "chatgpt_oai_client_version": metadata.get("chatgpt_oai_client_version", ""),
                    "chatgpt_oai_client_build_number": metadata.get("chatgpt_oai_client_build_number", ""),
                    "chatgpt_oai_device_id": metadata.get("chatgpt_oai_device_id", ""),
                    "chatgpt_oai_session_id": metadata.get("chatgpt_oai_session_id", ""),
                    "k12_session": metadata.get("k12_session", {}),
                    "k12_workspace_id": metadata.get("k12_workspace_id", ""),
                    "k12_workspace_sessions": metadata.get("k12_workspace_sessions", []),
                    "k12_workspace_ids": metadata.get("k12_workspace_ids", []),
                    "k12_sub2api_paths": metadata.get("k12_sub2api_paths", []),
                    "k12_deferred_sub2api_upload_enabled": metadata.get("k12_deferred_sub2api_upload_enabled", False),
                    "k12_remote_upload_handled": metadata.get("k12_remote_upload_handled", False),
                    "oauth_error": metadata.get("oauth_error", ""),
                    "password_set_after_register": metadata.get("password_set_after_register", False),
                    "post_register_password_error": metadata.get("post_register_password_error", ""),
                },
            )

        return ProtocolMailboxAdapter(
            result_mapper=_map_result,
            worker_builder=_build_worker,
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password,
            ),
            use_phone_callback=True,
        )

    def get_platform_actions(self) -> list:
        return [
            {"id": "switch_account", "label": "切换到 Codex 桌面端", "params": []},
            {"id": "get_account_state", "label": "查询账号状态/订阅", "params": [], "sync": True},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {"id": "get_rt", "label": "获取rt",
             "params": [
                 {"key": "browser_mode", "label": "浏览器模式", "type": "select",
                  "options": ["camoufox_headed", "camoufox_headless"]},
             ]},
            {"id": "get_rt_bypass", "label": "获取rt(绕过手机号)",
             "params": [
                 {"key": "browser_mode", "label": "浏览器模式", "type": "select",
                  "options": ["camoufox_headed", "camoufox_headless"]},
             ]},
            {"id": "refresh_session", "label": "重新登录获取session/at", "params": []},
            {"id": "payment_link", "label": "打开支付链接",
             "params": [
                 {"key": "country", "label": "地区", "type": "select",
                  "options": ["ID","US","SG","TR","HK","JP","GB","AU","CA","IN","BR","MX","EU"]},
                 {"key": "currency", "label": "币种", "type": "select",
                  "options": ["IDR","USD","SGD","TRY","HKD","JPY","GBP","AUD","CAD","INR","BRL","MXN","EUR"]},
                 {"key": "plan", "label": "套餐", "type": "select",
                  "options": ["plus", "team"]},
                 {"key": "auto_checkout", "label": "自动提交 PayPal", "type": "select",
                  "options": ["true", "false"]},
                 {"key": "use_stripe_init", "label": "Stripe协议长链(accessToken直生成)", "type": "select",
                  "options": ["false", "true"]},
                 {"key": "use_short_link", "label": "短链(checkout_ui_mode=custom)", "type": "select",
                  "options": ["false", "true"]},
                 {"key": "payment_method", "label": "支付方式", "type": "select",
                  "options": ["paypal"]},
                 {"key": "headless", "label": "后台模式", "type": "select",
                  "options": ["false", "true"]},
                 # checkout_mode 决定 PayPal checkout 浏览器后端：
                 #   - protocol: 走 Stripe API 协议链，无浏览器
                 #   - camoufox_headed / camoufox_headless: 老 Camoufox 路径
                 #   - bitbrowser_headed / bitbrowser_hidden / bitbrowser_headless:
                 #     新 BitBrowser 路径，profile ID 通过 bit_profile_id 字段传入
                 {"key": "checkout_mode", "label": "Checkout 后端模式", "type": "select",
                  "options": [
                      "",
                      "protocol",
                      "camoufox_headed",
                      "camoufox_headless",
                      "bitbrowser_headed",
                      "bitbrowser_hidden",
                      "bitbrowser_headless",
                  ]},
                 # bitbrowser_* 模式下必填：BitBrowser 客户端里手工创建好的 profile ID
                 # （比特浏览器 → 浏览器列表 → 编辑那一栏看到的 ID 字符串）。
                 # 留空时回退到 BIT_PROFILE_ID 环境变量。
                 {"key": "bit_profile_id", "label": "BitBrowser Profile ID", "type": "text",
                  "placeholder": "比特浏览器 profile ID（仅 bitbrowser_* 模式下生效）"},
                 {"key": "checkout_timeout", "label": "结账超时秒数", "type": "number"},
                 {"key": "checkout_hold_seconds", "label": "前台保留秒数", "type": "number"},
                 # SMS 号码池：批量手机号 + 短信中转 URL，PayPal OTP 用
                 # 每行 `+phone----relay_url`，多行批量。空行 / # 注释行自动忽略。
                 {"key": "sms_pool", "label": "SMS 号码池 (+phone----relay_url 每行一条)",
                  "type": "textarea", "placeholder": "+15822057201----https://mail-api.yuecheng.shop/api/text-relay/eca_tr_xxx"},
             ]},
            {"id": "extract_ba_link", "label": "提取BA链",
             "params": [
                 {"key": "billing_proxy", "label": "账单IP/代理（URL 或两位地区码）", "type": "text",
                  "placeholder": "US 或 http://user:pass@host:port"},
                 {"key": "promo_proxy", "label": "优惠IP/代理（URL 或两位地区码）", "type": "text",
                  "placeholder": "TR 或 http://user:pass@host:port"},
                 {"key": "billing_country", "label": "账单国家", "type": "select",
                  "options": ["US","TR","VN","BR","JP","IE","NL","DE","ID","SG","HK","GB","AU","CA","IN","MX"]},
                 {"key": "promo_country", "label": "优惠国家", "type": "select",
                  "options": ["TR","VN","BR","JP","US","IE","NL","DE"]},
                 {"key": "billing_currency", "label": "账单币种（可空）", "type": "text"},
                 {"key": "confirm_mode", "label": "Confirm模式", "type": "select",
                  "options": ["pm", "direct"]},
                 {"key": "promo_create_mode", "label": "优惠应用模式", "type": "select",
                  "options": ["update_after_checkout", "create_with_promo"]},
                 {"key": "max_attempts", "label": "最大重试次数", "type": "number"},
             ]},
            {"id": "upload_cpa", "label": "上传 CPA",
             "params": [
                 {"key": "api_url", "label": "CPA API URL", "type": "text"},
                 {"key": "api_key", "label": "CPA API Key", "type": "text"},
             ]},
            {"id": "upload_sub2api", "label": "上传 SUB2API",
             "params": [
                 {"key": "force_upload_without_rt", "label": "无RT强制上传", "type": "checkbox"},
                 {"key": "api_url", "label": "SUB2API 后台地址（留空用全局配置）", "type": "text"},
                 {"key": "email", "label": "SUB2API 登录邮箱（留空用全局配置）", "type": "text"},
                 {"key": "password", "label": "SUB2API 登录密码（留空用全局配置）", "type": "text"},
                 {"key": "group_name", "label": "OpenAI 分组（留空默认 codex）", "type": "text"},
                 {"key": "account_priority", "label": "账号优先级（留空默认 1）", "type": "number"},
                 {"key": "default_proxy_name", "label": "默认代理名称 / ID（留空不用代理）", "type": "text"},
             ]},
            {"id": "k12_join_upload", "label": "强入K12空间",
             "params": [
                 {"key": "workspace_ids", "label": "母号 Workspace ID（逗号或换行分隔）", "type": "textarea"},
             ]},
            {"id": "upload_tm", "label": "上传 Team Manager",
             "params": [
                 {"key": "api_url", "label": "TM API URL", "type": "text"},
                 {"key": "api_key", "label": "TM API Key", "type": "text"},
             ]},
        ]

    def get_desktop_state(self) -> dict:
        from platforms.chatgpt.switch import get_codex_desktop_state

        return get_codex_desktop_state()

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "get_account_state":
            return self._handle_query_state(account, params)
        if action_id == "payment_link":
            return self._handle_generate_link(account, params)
        if action_id == "extract_ba_link":
            return self._handle_extract_ba_link(account, params)
        if action_id == "get_rt":
            result = self._handle_get_rt(account, params)
            if not bool(result.get("ok")):
                return self._postprocess_get_rt_failure(account, result)
            return result
        if action_id == "get_rt_bypass":
            result = self._handle_get_rt_bypass(account, params)
            if not bool(result.get("ok")):
                return self._postprocess_get_rt_failure(account, result)
            return result
        if action_id == "refresh_session":
            return self._handle_refresh_session(account, params)
        if action_id in {"upload_cpa", "upload_sub2api", "upload_tm", "k12_join_upload"}:
            return self._execute_platform_action(action_id, account, params)
        return super().execute_action(action_id, account, params)

    def _postprocess_get_rt_failure(self, account: Account, result: dict) -> dict:
        error_text = str((result or {}).get("error") or "")
        if _is_chatgpt_deleted_or_deactivated_error(error_text):
            next_result = dict(result or {})
            next_result.setdefault("lifecycle_status", "banned")
            next_result.setdefault("summary_updates", {})
            next_result["summary_updates"].setdefault("deactivated_reason", error_text)
            next_result.setdefault("error_type", "account_banned")
            return next_result
        if not _is_chatgpt_session_stale_error(error_text):
            return result
        proxy = self.config.proxy if self.config else None
        refreshed = _refresh_chatgpt_session_after_get_rt(account, proxy=proxy)
        if not refreshed.get("ok"):
            return result
        refreshed_data = dict(refreshed.get("data") or {})
        next_result = dict(result or {})
        next_result["error_type"] = "session_stale_refreshed"
        next_result["data"] = {
            "access_token": str(refreshed_data.get("access_token") or ""),
            "refresh_token": str(refreshed_data.get("refresh_token") or ""),
            "id_token": str(refreshed_data.get("access_token") or ""),
            "session_token": str(refreshed_data.get("session_token") or ""),
            "account_state": refreshed_data.get("account_state") or {},
            "session_refresh_status": "refreshed",
        }
        return next_result

    def _execute_platform_action(self, action_id: str, account: Account, params: dict) -> dict:
        """Handle ChatGPT-specific actions."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        from .constants import OAUTH_CLIENT_ID
        a.client_id = extra.get("client_id", OAUTH_CLIENT_ID)
        a.workspace_id = extra.get("workspace_id", "")
        a.expires_at = extra.get("expires_at", "")
        overview = _safe_dict(extra.get("account_overview"))
        legacy_extra = _safe_dict(overview.get("legacy_extra"))
        a.session = extra.get("session") or overview.get("session") or overview.get("chatgpt_session") or legacy_extra.get("session") or {}
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id or ""
        a.account_id = account.user_id or ""
        a.extra = extra

        if action_id == "switch_desktop":
            from platforms.chatgpt.switch import (
                close_codex_app,
                extract_session_token,
                fetch_chatgpt_account_state,
                get_codex_desktop_state,
                read_current_codex_account,
                restart_codex_app,
                switch_codex_account,
            )

            session_token = extract_session_token(a.session_token, a.cookies)
            if not session_token:
                return {"ok": False, "error": "Switch to Codex desktop requires session_token"}

            close_ok, close_msg = close_codex_app()
            switch_ok, switch_data = switch_codex_account(session_token=session_token, cookies=a.cookies)
            if not switch_ok:
                return {"ok": False, "error": switch_data.get("error", "Switch failed")}

            remote_state = fetch_chatgpt_account_state(
                access_token=a.access_token,
                session_token=session_token,
                cookies=a.cookies,
                proxy=proxy,
            )
            local_state = read_current_codex_account()
            restart_ok, restart_msg = restart_codex_app()
            message_parts = [switch_data.get("message", "Codex credentials written")]
            if close_msg:
                message_parts.append(close_msg)
            if restart_msg:
                message_parts.append(restart_msg)
            data = {
                "message": ".".join(part for part in message_parts if part),
                "close": {"ok": close_ok, "message": close_msg},
                "restart": {"ok": restart_ok, "message": restart_msg},
                "local_app_account": local_state,
                "desktop_app_state": get_codex_desktop_state(),
                "remote_state": remote_state,
                "switch_details": switch_data,
            }
            if remote_state.get("access_token"):
                data["access_token"] = remote_state["access_token"]
            if remote_state.get("refresh_token"):
                data["refresh_token"] = remote_state["refresh_token"]
            return {"ok": True, "data": data}

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import upload_to_cpa, generate_token_json
            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(token_data, api_url=params.get("api_url"),
                                    api_key=params.get("api_key"))
            return {"ok": ok, "data": {"message": msg, "upload_target": "cpa", "upload_status": "uploaded" if ok else "failed"}}

        if action_id == "upload_sub2api":
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api
            force_without_rt = _bool_param(params, "force_upload_without_rt", False)
            ok, msg = upload_to_sub2api(
                a,
                api_url=params.get("api_url") or params.get("sub2api_url"),
                email=params.get("email") or params.get("sub2api_email"),
                password=params.get("password") or params.get("sub2api_password"),
                group_name=params.get("group_name") or params.get("sub2api_group_name"),
                account_priority=params.get("account_priority") or params.get("sub2api_account_priority"),
                default_proxy_name=params.get("default_proxy_name") or params.get("sub2api_default_proxy_name"),
                force_upload_without_rt=force_without_rt,
            )
            return {
                "ok": ok,
                "data": {
                    "message": msg,
                    "upload_target": "sub2api",
                    "upload_status": "uploaded" if ok else "failed",
                    "force_upload_without_rt": force_without_rt,
                    "uploaded_without_rt": bool(ok and force_without_rt and not a.refresh_token),
                },
            }

        if action_id == "k12_join_upload":
            return self._handle_k12_join_upload(a, params, proxy=proxy)

        if action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager
            ok, msg = upload_to_team_manager(a, api_url=params.get("api_url"),
                                             api_key=params.get("api_key"))
            return {"ok": ok, "data": {"message": msg, "upload_target": "team_manager", "upload_status": "uploaded" if ok else "failed"}}

        if action_id == "payment_link":
            return self._handle_generate_link(account, params)
        if action_id == "extract_ba_link":
            return self._handle_extract_ba_link(account, params)

        raise NotImplementedError(f"Unknown action: {action_id}")

    def _handle_k12_join_upload(self, account, params: dict, *, proxy: str | None = None) -> dict:
        from platforms.chatgpt.k12_join import (
            ensure_chatgpt_session_cookie,
            exchange_workspace_session,
            parse_workspace_ids,
            send_workspace_join_requests,
            upload_session_to_sub2api,
        )

        workspace_ids = str(params.get("workspace_ids") or params.get("k12_workspace_ids") or "").strip()
        workspace_list = parse_workspace_ids(workspace_ids)
        if not workspace_list:
            return {"ok": False, "error": "请输入母号 Workspace ID"}

        registration_session = account.session if isinstance(account.session, dict) else {}
        access_token = _first_non_empty(
            registration_session.get("accessToken"),
            registration_session.get("access_token"),
            account.access_token,
        )
        session_token = _first_non_empty(
            registration_session.get("sessionToken"),
            registration_session.get("session_token"),
            account.session_token,
        )
        cookies = ensure_chatgpt_session_cookie(str(account.cookies or ""), session_token)
        if not access_token:
            return {"ok": False, "error": "账号缺少 ChatGPT Web session accessToken，无法强入 K12"}
        if "__Secure-next-auth.session-token=" not in cookies:
            return {"ok": False, "error": "账号缺少 ChatGPT Web session cookie，无法强入 K12"}

        chosen_ws = ""
        k12_session = None
        for ws_id in workspace_list:
            join_results = send_workspace_join_requests(
                access_token=access_token,
                cookies=cookies,
                workspace_ids=ws_id,
                proxy=proxy,
                log=self.log,
            )
            join_ok = any(isinstance(item, dict) and item.get("ok") for item in join_results)
            if not join_ok:
                self.log(f"[K12] workspace {ws_id[:8]}... join 未成功，继续尝试下一个 workspace")
                continue
            candidate_session = exchange_workspace_session(
                cookies=cookies,
                workspace_id=ws_id,
                access_token=access_token,
                proxy=proxy,
                log=self.log,
            )
            if candidate_session:
                chosen_ws = ws_id
                k12_session = candidate_session
                break
            self.log(f"[K12] workspace {ws_id[:8]}... exchange 校验失败，继续尝试下一个 workspace")

        if not chosen_ws or not k12_session:
            return {"ok": False, "error": "所有 workspace 均未完成 join + exchange 校验，停止 K12 上传"}

        upload_ok, upload_msg = upload_session_to_sub2api(k12_session, log=self.log, proxy=proxy)
        k12_access_token = str(k12_session.get("accessToken") or k12_session.get("access_token") or "")
        k12_session_token = str(k12_session.get("sessionToken") or k12_session.get("session_token") or "")
        upload_status = "uploaded" if upload_ok else "failed"
        message = f"K12 强入成功，SUB2API 上传{'成功' if upload_ok else '失败'}: {upload_msg}"
        return {
            "ok": True,
            "data": {
                "message": message,
                "upload_target": "k12_sub2api",
                "upload_status": upload_status,
                "k12_workspace_id": chosen_ws,
                "k12_session": k12_session,
                "access_token": k12_access_token,
                "session_token": k12_session_token,
                "plan_type": "k12",
            },
        }

    # Override specific capability handlers
    def _handle_query_state(self, account: Account, params: dict) -> dict:
        """Handle query_state capability for ChatGPT."""
        extra = account.extra or {}
        region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
        proxy = _resolve_action_proxy(
            self.config.proxy if self.config else None,
            region=region,
            log_fn=getattr(self, "_log_fn", None),
            action_label="查询账号状态",
        )

        class _A: pass
        a = _A()
        a.access_token = extra.get("access_token") or account.token
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")

        from platforms.chatgpt.switch import fetch_chatgpt_account_state, get_codex_desktop_state, read_current_codex_account

        data = fetch_chatgpt_account_state(
            access_token=a.access_token,
            session_token=a.session_token,
            cookies=a.cookies,
            proxy=proxy,
            chatgpt_account_id=str(account.user_id or extra.get("account_id") or extra.get("chatgpt_account_id") or ""),
            existing_extra=extra,
            force_usage=True,
        )
        data["local_app_account"] = read_current_codex_account()
        data["desktop_app_state"] = get_codex_desktop_state()
        return {"ok": True, "data": data}

    def _handle_refresh_token(self, account: Account, params: dict) -> dict:
        """Handle refresh_token capability for ChatGPT."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")
        from .constants import OAUTH_CLIENT_ID
        a.client_id = extra.get("client_id") or extra.get("clientId") or OAUTH_CLIENT_ID

        from platforms.chatgpt.token_refresh import TokenRefreshManager
        manager = TokenRefreshManager(proxy_url=proxy)
        result = manager.refresh_account(a)
        if result.success:
            data = {"access_token": result.access_token, "refresh_token": result.refresh_token}
            try:
                from platforms.chatgpt.switch import fetch_chatgpt_account_state
                data["account_state"] = fetch_chatgpt_account_state(
                    access_token=result.access_token,
                    session_token=a.session_token,
                    cookies=a.cookies,
                    proxy=proxy,
                )
            except Exception:
                pass
            return {"ok": True, "data": data}
        return {"ok": False, "error": result.error_message}

    def _build_get_rt_mailbox_otp_callback(self, account: Account, log_fn, proxy: str | None):
        """Build an OTP callback from the mailbox resource attached to account."""
        from core.base_mailbox import MailboxAccount, create_mailbox

        def _text(value) -> str:
            return str(value or "").strip()

        def _safe_dict(value) -> dict:
            return dict(value) if isinstance(value, dict) else {}

        def _safe_list(value) -> list:
            return list(value) if isinstance(value, (list, tuple)) else []

        def _mailbox_provider_key(value: str, metadata: dict | None = None) -> str:
            raw = _text(value)
            api_mode = _text((metadata or {}).get("api_mode")).lower()
            if raw in {"cloud_mail", "cfworker"} or api_mode in {"cloud_mail", "cfworker"}:
                return "cfworker_admin_api"
            if raw == "outlook_email":
                return "outlook_email_api"
            return raw

        def _apply_provider_compat_settings(provider_key: str, runtime_extra: dict, metadata: dict) -> None:
            if provider_key == "cfworker_admin_api":
                if metadata.get("api_url") and not runtime_extra.get("cfworker_api_url"):
                    runtime_extra["cfworker_api_url"] = metadata.get("api_url")
                if metadata.get("domain") and not runtime_extra.get("cfworker_domain"):
                    runtime_extra["cfworker_domain"] = metadata.get("domain")
                token = (
                    metadata.get("admin_token")
                    or metadata.get("public_token")
                    or metadata.get("api_token")
                    or metadata.get("token")
                )
                if token and not runtime_extra.get("cfworker_admin_token"):
                    runtime_extra["cfworker_admin_token"] = token

        def _alias_parent_from(metadata: dict, mailbox_email: str) -> tuple[str, str, bool]:
            alias_meta = _safe_dict(metadata.get("email_alias"))
            metadata_email = _text(metadata.get("email"))
            metadata_email_lc = metadata_email.lower()
            mailbox_email_lc = mailbox_email.lower()
            metadata_email_is_parent = (
                "@" in metadata_email
                and metadata_email_lc != mailbox_email_lc
                and "+" not in metadata_email.split("@", 1)[0]
            )
            parent_email = _text(
                (metadata_email if metadata_email_is_parent else "")
                or metadata.get("alias_parent_email")
                or metadata.get("email_alias_parent")
                or metadata.get("parent_email")
                or alias_meta.get("parent_email")
                or alias_meta.get("alias_parent_email")
            )
            parent_account_id = _text(
                metadata.get("alias_parent_account_id")
                or alias_meta.get("parent_account_id")
                or alias_meta.get("alias_parent_account_id")
            )
            is_alias = bool(parent_email or parent_account_id or alias_meta.get("enabled"))
            if not parent_email and "+" in mailbox_email and "@" in mailbox_email:
                local_part, domain = mailbox_email.split("@", 1)
                parent_email = f"{local_part.split('+', 1)[0]}@{domain}"
                is_alias = True
                log_fn(f"  获取rt: 检测到 plus 别名邮箱但缺少父邮箱元数据，按邮箱地址推断父邮箱={parent_email}")
            return parent_email, parent_account_id, is_alias

        extra = _safe_dict(account.extra)
        resources = [dict(item) for item in _safe_list(extra.get("provider_resources")) if isinstance(item, dict)]
        mailbox_resources = []
        for item in resources:
            if _text(item.get("resource_type") or "mailbox").lower() == "mailbox":
                mailbox_resources.append(item)

        if not mailbox_resources:
            mailbox = _safe_dict(extra.get("verification_mailbox"))
            if not mailbox:
                mailbox = _safe_dict(_safe_dict(extra.get("identity")).get("mailbox"))
            if mailbox:
                mailbox_resources.append({
                    "provider_type": "mailbox",
                    "provider_name": mailbox.get("provider"),
                    "resource_type": "mailbox",
                    "resource_identifier": mailbox.get("account_id"),
                    "handle": mailbox.get("email"),
                    "display_name": mailbox.get("email"),
                    "metadata": {
                        "account_id": mailbox.get("account_id"),
                        "email": mailbox.get("email"),
                    },
                })

        if not mailbox_resources:
            return None, "账号没有绑定邮箱 provider 资源，无法自动读取真实邮箱 OTP"

        provider_accounts = [
            dict(item) for item in _safe_list(extra.get("provider_accounts")) if isinstance(item, dict)
        ]
        last_error = ""
        selected_provider_name = ""
        selected_mailbox_email = ""
        mailbox = None
        mailbox_account = None

        for mailbox_resource in mailbox_resources:
            metadata = _safe_dict(mailbox_resource.get("metadata"))
            raw_provider_name = _text(mailbox_resource.get("provider_name") or mailbox_resource.get("provider"))
            provider_name = _mailbox_provider_key(raw_provider_name, metadata)
            mailbox_email = _text(
                mailbox_resource.get("handle")
                or mailbox_resource.get("display_name")
                or metadata.get("email")
                or account.email
            )
            account_id = _text(
                mailbox_resource.get("resource_identifier")
                or metadata.get("account_id")
                or metadata.get("id")
                or mailbox_email
            )

            if not provider_name:
                last_error = "账号邮箱资源缺少 provider_name"
                continue
            if not mailbox_email:
                last_error = "账号邮箱资源缺少 email"
                continue

            accepted_providers = {provider_name, raw_provider_name}
            if provider_name == "cfworker_admin_api":
                accepted_providers.update({"cloud_mail", "cfworker"})
            if provider_name == "outlook_email_api":
                accepted_providers.add("outlook_email")
            accepted_providers = {item for item in accepted_providers if item}

            same_provider_account = None
            matched_provider_account = None
            parent_email, parent_account_id, is_alias = _alias_parent_from(metadata, mailbox_email)
            email_candidates = {mailbox_email.lower(), parent_email.lower()}
            id_candidates = {account_id.lower(), parent_account_id.lower()}
            email_candidates.discard("")
            id_candidates.discard("")
            for item in provider_accounts:
                item_provider = _mailbox_provider_key(
                    _text(item.get("provider_name") or item.get("provider")),
                    _safe_dict(item.get("metadata")),
                )
                raw_item_provider = _text(item.get("provider_name") or item.get("provider"))
                if (item_provider or raw_item_provider) and not ({item_provider, raw_item_provider} & accepted_providers):
                    continue
                if same_provider_account is None:
                    same_provider_account = item
                item_metadata = _safe_dict(item.get("metadata"))
                item_credentials = _safe_dict(item.get("credentials"))
                candidates = {
                    _text(item.get("login_identifier")).lower(),
                    _text(item.get("display_name")).lower(),
                    _text(item_metadata.get("email")).lower(),
                    _text(item_metadata.get("account_id")).lower(),
                    _text(item_credentials.get("email")).lower(),
                    _text(item_credentials.get("login_account")).lower(),
                    _text(item.get("id")).lower(),
                }
                if (email_candidates & candidates) or (id_candidates & candidates):
                    matched_provider_account = item
                    break

            provider_account = matched_provider_account or same_provider_account
            runtime_extra = dict(metadata)
            _apply_provider_compat_settings(provider_name, runtime_extra, metadata)
            runtime_resource = dict(mailbox_resource)
            runtime_metadata = dict(metadata)
            if is_alias:
                if not parent_email:
                    last_error = f"别名邮箱 {mailbox_email} 缺少父邮箱，无法读取 OTP"
                    continue
                parent_account_id = parent_account_id or account_id or parent_email
                runtime_metadata["alias_parent_email"] = parent_email
                runtime_metadata["alias_parent_account_id"] = parent_account_id
                runtime_metadata["email_alias"] = {
                    **_safe_dict(runtime_metadata.get("email_alias")),
                    "enabled": True,
                    "alias_email": mailbox_email,
                    "parent_email": parent_email,
                    "parent_account_id": parent_account_id,
                }
                runtime_resource["metadata"] = runtime_metadata
                runtime_resource["handle"] = mailbox_email
                runtime_resource["display_name"] = mailbox_email
                runtime_resource["resource_identifier"] = parent_account_id
                runtime_extra = dict(runtime_metadata)
                _apply_provider_compat_settings(provider_name, runtime_extra, runtime_metadata)
            runtime_extra["provider_resource"] = runtime_resource
            if provider_account:
                runtime_extra["provider_account"] = provider_account

            mailbox_account_extra = dict(runtime_extra)
            mailbox_account_extra["mailbox_provider_key"] = provider_name
            if is_alias:
                mailbox_account_extra["email_alias"] = {
                    "enabled": True,
                    "alias_email": mailbox_email,
                    "parent_email": parent_email,
                    "parent_account_id": parent_account_id or account_id or parent_email,
                }
            mailbox_account = MailboxAccount(
                email=parent_email if is_alias else mailbox_email,
                account_id=(parent_account_id or account_id or parent_email) if is_alias else account_id,
                extra=mailbox_account_extra,
            )
            try:
                mailbox = create_mailbox(provider_name, extra=runtime_extra, proxy=None)
            except Exception as exc:
                last_error = f"{raw_provider_name or provider_name} -> {provider_name}: {exc}"
                log_fn(f"  获取rt: 跳过不可用邮箱资源 {last_error}")
                mailbox = None
                mailbox_account = None
                continue
            selected_provider_name = provider_name
            selected_mailbox_email = mailbox_email
            if raw_provider_name and raw_provider_name != provider_name:
                log_fn(f"  获取rt: 邮箱 provider 兼容映射 {raw_provider_name} -> {provider_name}")
            if is_alias:
                log_fn(f"  获取rt: 检测到别名邮箱 alias={mailbox_email} parent={parent_email}")
            break

        if mailbox is None or mailbox_account is None:
            return None, f"无法初始化账号邮箱 provider: {last_error or '没有可用邮箱资源'}"

        before_ids = set()
        try:
            before_ids = set(mailbox.get_current_ids(mailbox_account) or set())
            log_fn(
                f"  获取rt: 邮箱 OTP 基线已读取 provider={selected_provider_name} "
                f"email={selected_mailbox_email} before_ids={len(before_ids)}"
            )
        except Exception as exc:
            log_fn(f"  获取rt: 邮箱 OTP 基线读取失败，继续等待新验证码: {exc}")

        def _otp_callback():
            log_fn(f"  获取rt: 等待真实邮箱 OTP provider={selected_provider_name} email={selected_mailbox_email}")
            return mailbox.wait_for_code(
                mailbox_account,
                keyword="",
                timeout=600,
                before_ids=before_ids or None,
            )

        def _refresh_before_ids():
            nonlocal before_ids
            try:
                before_ids = set(mailbox.get_current_ids(mailbox_account) or set())
                log_fn(
                    f"  get_rt: email OTP baseline refreshed provider={selected_provider_name} "
                    f"email={selected_mailbox_email} before_ids={len(before_ids)}"
                )
            except Exception as exc:
                before_ids = set()
                log_fn(f"  get_rt: email OTP baseline refresh failed: {exc}")
            return set(before_ids or set())

        _otp_callback.refresh_before_ids = _refresh_before_ids  # type: ignore[attr-defined]
        return _otp_callback, ""

    def _build_refresh_session_mailbox_email_service(self, account: Account, log_fn, proxy: str | None):
        """Build RegistrationEngine email_service for an existing account login."""
        from core.base_mailbox import MailboxAccount, create_mailbox
        from core.email_alias_mailbox import EmailAliasMailbox, normalize_email_address
        from platforms.chatgpt.protocol_mailbox import _MailboxEmailService

        def _text(value) -> str:
            return str(value or "").strip()

        def _safe_list(value) -> list:
            return list(value) if isinstance(value, (list, tuple)) else []

        def _mailbox_provider_key(value: str, metadata: dict | None = None) -> str:
            raw = _text(value)
            api_mode = _text((metadata or {}).get("api_mode")).lower()
            if raw in {"cloud_mail", "cfworker"} or api_mode in {"cloud_mail", "cfworker"}:
                return "cfworker_admin_api"
            if raw == "outlook_email":
                return "outlook_email_api"
            return raw

        def _apply_provider_compat_settings(provider_key: str, runtime_extra: dict, metadata: dict) -> None:
            if provider_key == "cfworker_admin_api":
                if metadata.get("api_url") and not runtime_extra.get("cfworker_api_url"):
                    runtime_extra["cfworker_api_url"] = metadata.get("api_url")
                if metadata.get("domain") and not runtime_extra.get("cfworker_domain"):
                    runtime_extra["cfworker_domain"] = metadata.get("domain")
                token = (
                    metadata.get("admin_token")
                    or metadata.get("public_token")
                    or metadata.get("api_token")
                    or metadata.get("token")
                )
                if token and not runtime_extra.get("cfworker_admin_token"):
                    runtime_extra["cfworker_admin_token"] = token

        def _parent_from_plus_alias(email: str) -> str:
            if "+" not in email or "@" not in email:
                return ""
            local_part, domain = email.split("@", 1)
            return f"{local_part.split('+', 1)[0]}@{domain}"

        def _alias_parent_from(metadata: dict, mailbox_email: str) -> tuple[str, str, bool]:
            alias_meta = _safe_dict(metadata.get("email_alias"))
            metadata_email = _text(metadata.get("email"))
            metadata_email_lc = metadata_email.lower()
            mailbox_email_lc = mailbox_email.lower()
            metadata_email_is_parent = (
                "@" in metadata_email
                and metadata_email_lc != mailbox_email_lc
                and "+" not in metadata_email.split("@", 1)[0]
            )
            parent_email = _text(
                (metadata_email if metadata_email_is_parent else "")
                or metadata.get("alias_parent_email")
                or metadata.get("email_alias_parent")
                or metadata.get("parent_email")
                or alias_meta.get("parent_email")
                or alias_meta.get("alias_parent_email")
            )
            parent_account_id = _text(
                metadata.get("alias_parent_account_id")
                or alias_meta.get("parent_account_id")
                or alias_meta.get("alias_parent_account_id")
            )
            is_alias = bool(parent_email or parent_account_id or alias_meta.get("enabled"))
            if not parent_email:
                parent_email = _parent_from_plus_alias(mailbox_email)
                if parent_email:
                    is_alias = True
                    log_fn(f"  重新登录: 检测到 plus 别名邮箱，按邮箱地址推断母邮箱={parent_email}")
            return parent_email, parent_account_id, is_alias

        extra = _safe_dict(account.extra)
        resources = [dict(item) for item in _safe_list(extra.get("provider_resources")) if isinstance(item, dict)]
        mailbox_resources = [
            item
            for item in resources
            if _text(item.get("resource_type") or "mailbox").lower() == "mailbox"
        ]

        if not mailbox_resources:
            mailbox = _safe_dict(extra.get("verification_mailbox"))
            if not mailbox:
                mailbox = _safe_dict(_safe_dict(extra.get("identity")).get("mailbox"))
            if mailbox:
                mailbox_resources.append({
                    "provider_type": "mailbox",
                    "provider_name": mailbox.get("provider"),
                    "resource_type": "mailbox",
                    "resource_identifier": mailbox.get("account_id"),
                    "handle": mailbox.get("email"),
                    "display_name": mailbox.get("email"),
                    "metadata": {
                        "account_id": mailbox.get("account_id"),
                        "email": mailbox.get("email"),
                    },
                })

        if not mailbox_resources:
            return None, "账号没有绑定邮箱 provider 资源，无法自动读取真实邮箱 OTP"

        def _mailbox_resource_score(item: dict) -> tuple[int, int]:
            metadata = _safe_dict(item.get("metadata"))
            alias_meta = _safe_dict(metadata.get("email_alias"))
            has_alias_parent = bool(
                metadata.get("alias_parent_email")
                or metadata.get("email_alias_parent")
                or metadata.get("parent_email")
                or alias_meta.get("parent_email")
            )
            return (1 if has_alias_parent else 0, len(metadata))

        mailbox_resources.sort(key=_mailbox_resource_score, reverse=True)
        provider_accounts = [
            dict(item) for item in _safe_list(extra.get("provider_accounts")) if isinstance(item, dict)
        ]
        account_email = _text(account.email)
        last_error = ""

        for mailbox_resource in mailbox_resources:
            metadata = _safe_dict(mailbox_resource.get("metadata"))
            alias_meta = _safe_dict(metadata.get("email_alias"))
            raw_provider_name = _text(mailbox_resource.get("provider_name") or mailbox_resource.get("provider"))
            provider_name = _mailbox_provider_key(raw_provider_name, metadata)
            resource_email = _text(
                mailbox_resource.get("handle")
                or mailbox_resource.get("display_name")
                or metadata.get("email")
                or account_email
            )
            mailbox_email = _text(
                metadata.get("alias_email")
                or alias_meta.get("alias_email")
                or resource_email
                or account_email
            )
            account_id = _text(
                mailbox_resource.get("resource_identifier")
                or metadata.get("account_id")
                or metadata.get("id")
                or resource_email
                or mailbox_email
            )
            if account_email and account_email.lower() != mailbox_email.lower():
                inferred_parent = _parent_from_plus_alias(account_email)
                resource_parent_candidates = {
                    resource_email.lower(),
                    _text(metadata.get("email")).lower(),
                }
                resource_parent_candidates.discard("")
                if inferred_parent and inferred_parent.lower() in resource_parent_candidates:
                    mailbox_email = account_email

            if not provider_name:
                last_error = "账号邮箱资源缺少 provider_name"
                continue
            if not mailbox_email:
                last_error = "账号邮箱资源缺少 email"
                continue

            accepted_providers = {provider_name, raw_provider_name}
            if provider_name == "cfworker_admin_api":
                accepted_providers.update({"cloud_mail", "cfworker"})
            if provider_name == "outlook_email_api":
                accepted_providers.add("outlook_email")
            accepted_providers = {item for item in accepted_providers if item}

            parent_email, parent_account_id, is_alias = _alias_parent_from(metadata, mailbox_email)
            if is_alias and not parent_email:
                last_error = f"别名邮箱 {mailbox_email} 缺少母邮箱，无法读取 OTP"
                continue
            if is_alias:
                parent_account_id = parent_account_id or account_id or parent_email

            email_candidates = {mailbox_email.lower(), parent_email.lower(), resource_email.lower()}
            id_candidates = {account_id.lower(), parent_account_id.lower()}
            email_candidates.discard("")
            id_candidates.discard("")

            same_provider_account = None
            matched_provider_account = None
            for item in provider_accounts:
                item_provider = _mailbox_provider_key(
                    _text(item.get("provider_name") or item.get("provider")),
                    _safe_dict(item.get("metadata")),
                )
                raw_item_provider = _text(item.get("provider_name") or item.get("provider"))
                if (item_provider or raw_item_provider) and not ({item_provider, raw_item_provider} & accepted_providers):
                    continue
                if same_provider_account is None:
                    same_provider_account = item
                item_metadata = _safe_dict(item.get("metadata"))
                item_credentials = _safe_dict(item.get("credentials"))
                candidates = {
                    _text(item.get("login_identifier")).lower(),
                    _text(item.get("display_name")).lower(),
                    _text(item_metadata.get("email")).lower(),
                    _text(item_metadata.get("account_id")).lower(),
                    _text(item_credentials.get("email")).lower(),
                    _text(item_credentials.get("login_account")).lower(),
                    _text(item.get("id")).lower(),
                }
                if (email_candidates & candidates) or (id_candidates & candidates):
                    matched_provider_account = item
                    break

            provider_account = matched_provider_account or same_provider_account
            runtime_extra = dict(metadata)
            _apply_provider_compat_settings(provider_name, runtime_extra, metadata)
            runtime_resource = dict(mailbox_resource)
            runtime_metadata = dict(metadata)
            if is_alias:
                runtime_metadata["alias_parent_email"] = parent_email
                runtime_metadata["alias_parent_account_id"] = parent_account_id
                runtime_metadata["email_alias"] = {
                    **_safe_dict(runtime_metadata.get("email_alias")),
                    "enabled": True,
                    "alias_email": mailbox_email,
                    "parent_email": parent_email,
                    "parent_account_id": parent_account_id,
                }
                runtime_resource["metadata"] = runtime_metadata
                runtime_resource["handle"] = mailbox_email
                runtime_resource["display_name"] = mailbox_email
                runtime_resource["resource_identifier"] = parent_account_id
                runtime_extra = dict(runtime_metadata)
                _apply_provider_compat_settings(provider_name, runtime_extra, runtime_metadata)
            runtime_extra["provider_resource"] = runtime_resource
            if provider_account:
                runtime_extra["provider_account"] = provider_account

            mailbox_account_extra = dict(runtime_extra)
            mailbox_account_extra["mailbox_provider_key"] = provider_name
            if is_alias:
                mailbox_account_extra["email_alias"] = {
                    "enabled": True,
                    "alias_email": mailbox_email,
                    "parent_email": parent_email,
                    "parent_account_id": parent_account_id or account_id or parent_email,
                }
            mailbox_account = MailboxAccount(
                email=mailbox_email,
                account_id=(parent_account_id or account_id or mailbox_email) if is_alias else (account_id or mailbox_email),
                extra=mailbox_account_extra,
            )
            try:
                mailbox = create_mailbox(provider_name, extra=runtime_extra, proxy=None)
            except Exception as exc:
                last_error = f"{raw_provider_name or provider_name} -> {provider_name}: {exc}"
                log_fn(f"  重新登录: 邮箱资源不可用，跳过: {last_error}")
                continue

            if raw_provider_name and raw_provider_name != provider_name:
                log_fn(f"  重新登录: 邮箱 provider 兼容映射 {raw_provider_name} -> {provider_name}")
            if is_alias:
                normalized_parent = normalize_email_address(parent_email)
                if not normalized_parent:
                    return None, f"别名邮箱 {mailbox_email} 缺少母邮箱，无法读取 OTP"
                log_fn(f"  重新登录: 检测到别名邮箱 alias={mailbox_email} parent={normalized_parent}")
                alias_mailbox = EmailAliasMailbox(mailbox, platform="chatgpt", log_fn=log_fn)
                parent_extra = dict(mailbox_account_extra)
                parent_resource = dict(parent_extra.get("provider_resource") or {})
                if parent_resource:
                    parent_resource["handle"] = parent_email
                    parent_resource["display_name"] = parent_email
                    parent_resource["resource_identifier"] = parent_account_id or account_id or parent_email
                    parent_extra["provider_resource"] = parent_resource
                alias_mailbox._parents_by_alias[normalize_email_address(mailbox_email)] = MailboxAccount(
                    email=parent_email,
                    account_id=parent_account_id or account_id or parent_email,
                    extra=parent_extra,
                )
                mailbox = alias_mailbox

            log_fn(
                "  重新登录: 使用本地邮箱资源读取 OTP "
                f"provider={provider_name} login_email={mailbox_email} "
                f"mailbox_email={parent_email if is_alias else mailbox_email}"
            )
            return (
                _MailboxEmailService(
                    mailbox=mailbox,
                    mailbox_account=mailbox_account,
                    provider=provider_name,
                    log_fn=log_fn,
                ),
                "",
            )

        return None, f"无法初始化账号邮箱 provider: {last_error or '没有可用邮箱资源'}"

    def _refresh_session_failed_result(
        self,
        account: Account,
        message: str,
        *,
        account_banned: bool = False,
        error_type: str = "",
        delete_local_account: bool = True,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        resolved_error_type = error_type or ("account_banned" if account_banned else "session_refresh_failed")
        result = {
            "ok": False,
            "error": message,
            "error_type": resolved_error_type,
            "summary_updates": {
                "session_refresh_status": "failed",
                "last_session_refresh_at": now,
            },
            "data": {
                "email": account.email,
                "account_id": account.user_id or "",
                "session_refresh_status": "failed",
                "error_type": resolved_error_type,
            },
        }
        if account_banned:
            result["lifecycle_status"] = "banned"
            result["summary_updates"].update(
                {
                    "deactivated_reason": message,
                    "valid": False,
                    "validity_status": "invalid",
                    "display_status": "banned",
                }
            )
            result["data"]["mark_local_account_banned"] = True
            if delete_local_account:
                result["data"]["delete_local_account"] = True
        return result

    def _handle_refresh_session(self, account: Account, params: dict) -> dict:
        log_fn = getattr(self, "log", print)
        cancel_fn = getattr(self, "_cancel_check_fn", None)
        extra = account.extra or {}
        region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
        proxy = _resolve_action_proxy(
            self.config.proxy if self.config else None,
            region=region,
            log_fn=log_fn,
            action_label="重新登录",
        )

        if not account.password:
            return self._refresh_session_failed_result(account, "账号缺少密码，无法重新登录")

        if callable(cancel_fn) and cancel_fn():
            return {"ok": False, "error": "任务已取消", "data": {"email": account.email}}

        totp_secret = _account_secret_value(account, "totp_secret")
        prefer_password_totp_login = bool(str(account.password or "").strip() and totp_secret)
        email_service = None
        if prefer_password_totp_login:
            log_fn("重新登录检测到账号已保存密码和 2FA，本次使用密码 + TOTP，不初始化邮箱 OTP 服务")
        else:
            email_service, mailbox_error = self._build_refresh_session_mailbox_email_service(account, log_fn, proxy)
            if email_service is None:
                return self._refresh_session_failed_result(account, f"邮箱服务不可用: {mailbox_error}")

        try:
            from platforms.chatgpt.register import RegistrationEngine

            log_fn(f"重新登录获取session/at: {account.email}, proxy={_proxy_log_value(proxy)}")
            engine = RegistrationEngine(
                email_service=email_service,
                proxy_url=proxy,
                callback_logger=log_fn,
            )
            engine.email = account.email
            engine.password = account.password
            engine.totp_secret = totp_secret
            engine.prefer_password_totp_login = prefer_password_totp_login
            engine.k12_join_enabled = False
            engine.set_password_after_register = False
            result = engine.run_chatgpt_refresh_session_latest()
        except Exception as exc:
            message = f"重新登录异常: {exc}"
            cloudflare_challenge = _is_cloudflare_managed_challenge_error(message)
            validate_otp_http_403 = _is_validate_otp_http_403_error(message)
            return self._refresh_session_failed_result(
                account,
                message,
                account_banned=validate_otp_http_403 or _is_chatgpt_deleted_or_deactivated_error(message),
                error_type="cloudflare_managed_challenge" if cloudflare_challenge else "",
                delete_local_account=not validate_otp_http_403,
            )

        if callable(cancel_fn) and cancel_fn():
            return {"ok": False, "error": "任务已取消", "data": {"email": account.email}}

        if not result or not getattr(result, "success", False):
            error = str(getattr(result, "error_message", "") or "重新登录失败")
            cloudflare_challenge = _is_cloudflare_managed_challenge_error(error)
            validate_otp_http_403 = _is_validate_otp_http_403_error(error)
            return self._refresh_session_failed_result(
                account,
                error,
                account_banned=validate_otp_http_403 or _is_chatgpt_deleted_or_deactivated_error(error),
                error_type="cloudflare_managed_challenge" if cloudflare_challenge else "",
                delete_local_account=not validate_otp_http_403,
            )

        metadata = getattr(result, "metadata", None) or {}
        session = metadata.get("session") if isinstance(metadata.get("session"), dict) else {}
        access_token = _first_non_empty(
            getattr(result, "access_token", ""),
            session.get("accessToken") if isinstance(session, dict) else "",
            session.get("access_token") if isinstance(session, dict) else "",
        )
        session_token = _first_non_empty(
            getattr(result, "session_token", ""),
            session.get("sessionToken") if isinstance(session, dict) else "",
            session.get("session_token") if isinstance(session, dict) else "",
        )
        if not session or not access_token:
            return self._refresh_session_failed_result(account, "重新登录成功但没有拿到有效 session/at")

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log_fn(
            "重新登录成功: "
            f"{account.email} session=有 accessToken=有 sessionToken={'有' if session_token else '无'}"
        )
        return {
            "ok": True,
            "data": {
                "message": "重新登录成功，session/at 已更新",
                "email": account.email,
                "account_id": _first_non_empty(getattr(result, "account_id", ""), account.user_id),
                "access_token": access_token,
                "session_token": session_token,
                "cookies": metadata.get("cookies", ""),
                "login_state_cookie": metadata.get("login_state_cookie") or metadata.get("cookies", ""),
                "session": session,
                "session_refreshed_at": now,
                "session_refresh_status": "refreshed",
            },
        }

    def _handle_get_rt(self, account: Account, params: dict) -> dict:
        """通过浏览器 OAuth 获取 refresh_token（真实邮箱 OTP + 真实手机号 OTP）。

        参数：
          browser_mode: 浏览器模式
          sms_provider: 手机接码渠道（smspool / smsapi，空=不启用手机验证）
          smspool_api_key: SMSPool API key
          smspool_max_price: SMSPool 价格上限 USD
          smsapi_phone: smsapi 固定手机号
          smsapi_url: smsapi 查询短信 API URL
        """
        log_fn = getattr(self, "log", print)
        cancel_fn = getattr(self, "_cancel_check_fn", None)

        executor_type = str(params.get("executor_type") or "browser").strip().lower() or "browser"
        browser_mode = str(params.get("browser_mode") or "camoufox_headed")
        record_har = _bool_param(params, "record_har", False)
        try:
            phone_change_limit = max(int(params.get("phone_change_limit") or 10), 1)
        except Exception:
            phone_change_limit = 10
        extra = account.extra or {}
        region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
        proxy = _resolve_action_proxy(
            self.config.proxy if self.config else None,
            region=region,
            log_fn=log_fn,
            action_label="获取rt",
        )

        if not account.password:
            return {"ok": False, "error": "账号缺少密码，无法进行 OAuth 登录"}

        acquired_profile_id = ""
        bit_profile_id = ""
        phone_callback = None

        try:
            from platforms.chatgpt.browser_get_rt import (
                build_get_rt_phone_callback,
            )
            totp_secret = _account_secret_value(account, "totp_secret")
            prefer_password_totp_login = bool(str(account.password or "").strip() and totp_secret)
            if prefer_password_totp_login:
                otp_callback = None
                log_fn("  获取rt: 检测到账号已保存密码和 2FA，本次使用密码 + TOTP，不初始化邮箱 OTP 服务")
            else:
                otp_callback, otp_error = self._build_get_rt_mailbox_otp_callback(account, log_fn, proxy)
                if not otp_callback:
                    return {"ok": False, "error": f"获取rt失败: {otp_error}"}

            # ★ 手机号 OTP 回调（可选）
            phone_callback = None
            sms_provider = _normalize_get_rt_sms_provider(params.get("sms_provider"))
            supplied_phone_callback = params.get("phone_callback")
            if callable(supplied_phone_callback):
                phone_callback = supplied_phone_callback
                log_fn(f"  获取rt: 使用任务级手机号复用 callback provider={sms_provider or '(unknown)'}")
            elif sms_provider:
                phone_callback, phone_error = build_get_rt_phone_callback(
                    sms_provider=sms_provider,
                    smspool_api_key=str(params.get("smspool_api_key") or ""),
                    smspool_max_price=str(params.get("smspool_max_price") or "0.13"),
                    smspool_country=str(params.get("smspool_country") or ""),
                    smspool_service=str(params.get("smspool_service") or ""),
                    smspool_base_url=str(params.get("smspool_base_url") or ""),
                    smspool_compat_base_url=str(params.get("smspool_compat_base_url") or ""),
                    smspool_pricing_option=str(params.get("smspool_pricing_option") or ""),
                    smspool_poll_interval=str(params.get("smspool_poll_interval") or ""),
                    smsapi_phone=str(params.get("smsapi_phone") or ""),
                    smsapi_url=str(params.get("smsapi_url") or ""),
                    phone_change_limit=phone_change_limit,
                    log_fn=log_fn,
                )
                if not phone_callback:
                    log_fn(f"  获取rt: 手机 OTP 回调创建失败: {phone_error}，继续仅邮箱流程")
                else:
                    log_fn(f"  获取rt: 手机 OTP 已就绪 provider={sms_provider}")

            if executor_type == "protocol":
                log_fn(
                    f"获取rt: {account.email}, executor=protocol, "
                    f"sms={sms_provider or '(无)'}, proxy={_proxy_log_value(proxy)}"
                )
            else:
                log_fn(
                    f"获取rt: {account.email}, executor={executor_type}, browser_mode={browser_mode}, "
                    f"sms={sms_provider or '(无)'}, proxy={_proxy_log_value(proxy)}"
                )

            if executor_type == "protocol":
                from platforms.chatgpt.protocol_get_rt import run_protocol_get_rt

                result = run_protocol_get_rt(
                    email=account.email,
                    password=account.password,
                    proxy=proxy,
                    otp_callback=otp_callback,
                    log_fn=log_fn,
                    sms_provider=sms_provider,
                    smspool_api_key=str(params.get("smspool_api_key") or ""),
                    smspool_max_price=str(params.get("smspool_max_price") or "0.13"),
                    smspool_country=str(params.get("smspool_country") or ""),
                    smspool_service=str(params.get("smspool_service") or ""),
                    smspool_base_url=str(params.get("smspool_base_url") or ""),
                    smspool_compat_base_url=str(params.get("smspool_compat_base_url") or ""),
                    smspool_pricing_option=str(params.get("smspool_pricing_option") or ""),
                    smspool_poll_interval=str(params.get("smspool_poll_interval") or ""),
                    smsapi_phone=str(params.get("smsapi_phone") or ""),
                    smsapi_url=str(params.get("smsapi_url") or ""),
                    phone_callback=phone_callback,
                    phone_change_limit=phone_change_limit,
                    totp_secret=totp_secret,
                )
                refresh_token = str(result.get("refresh_token") or "")
                access_token = str(result.get("access_token") or "")
                backup_json_path = _save_get_rt_token_backup(account, result, action_label="get_rt_protocol")
                if backup_json_path:
                    log_fn(f"  获取rt token 本地备份: {backup_json_path}")
                return {
                    "ok": True,
                    "data": {
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "id_token": str(result.get("id_token") or ""),
                        "account_id": str(result.get("account_id") or ""),
                        "email": account.email,
                        "record_har_path": "",
                        "token_backup_path": backup_json_path,
                        "message": "refresh_token 获取成功" if refresh_token else "access_token 获取成功（无 refresh_token）",
                    },
                }

            from platforms._browser_backend import parse_checkout_mode
            from platforms.chatgpt.browser_register import (
                ChatGPTBrowserRegister,
                _build_proxy_config,
                _do_codex_oauth,
            )
            from platforms.chatgpt.browser_get_rt import setup_oauth_state_capture

            # ★ BitBrowser 模式：自动从 Profile 池获取可用的 profile ID
            if str(browser_mode or "").startswith("bitbrowser_"):
                from application.bitbrowser_profiles import (
                    acquire_profile_for_browser_mode,
                )
                bit_profile_id, acquired_profile_id = acquire_profile_for_browser_mode(
                    browser_mode,
                    fallback=bit_profile_id,
                    log_fn=log_fn,
                )

            backend_config = parse_checkout_mode(browser_mode, bit_profile_id=bit_profile_id)
            record_har_path = _build_get_rt_har_path(account.email) if record_har else None
            if record_har and not backend_config.is_camoufox:
                log_fn(
                    f"  get_rt HAR capture skipped: browser_mode={browser_mode} "
                    "does not support Playwright record_har_path"
                )
                record_har_path = None

            # 创建一个只用于 get_rt 的轻量 register 实例
            reg = ChatGPTBrowserRegister(
                headless=backend_config.is_headless,
                proxy=proxy,
                log_fn=log_fn,
                backend_config=backend_config,
                totp_secret=totp_secret,
            )

            if reg.backend_config.is_bitbrowser:
                launch_opts = {"headless": reg.backend_config.is_headless}
            else:
                cam_proxy = _build_proxy_config(reg.proxy)
                launch_opts = {"headless": reg.headless}
                if cam_proxy:
                    launch_opts["proxy"] = cam_proxy

            with reg._open_browser(launch_opts) as browser:
                har_context = None
                if record_har_path:
                    try:
                        os.makedirs(os.path.dirname(record_har_path), exist_ok=True)
                        har_context = browser.new_context(
                            record_har_path=record_har_path,
                            record_har_url_filter="**/*",
                        )
                        page = har_context.new_page()
                        log_fn(f"  get_rt HAR capture enabled: {record_har_path}")
                    except Exception as exc:
                        log_fn(f"  get_rt HAR capture init failed, continue without HAR: {exc}")
                        record_har_path = None
                        har_context = None
                        page = browser.new_page()
                else:
                    page = browser.new_page()

                try:
                    setup_oauth_state_capture(page, log=log_fn)
                    log_fn("  获取rt: 浏览器已打开，开始 OAuth...")

                    if callable(cancel_fn) and cancel_fn():
                        return {"ok": False, "error": "任务已取消"}

                    result = _do_codex_oauth(
                        page, {}, account.email, account.password,
                        otp_callback,
                        phone_callback,
                        proxy, log_fn,
                        max_phone_attempts=phone_change_limit,
                        totp_secret=totp_secret,
                    )

                    if not isinstance(result, dict) or not result.get("access_token"):
                        error_detail = "OAuth 未返回 token"
                        if isinstance(result, dict):
                            error_detail = str(result.get("error") or result.get("detail") or error_detail)
                        if _is_chatgpt_deleted_or_deactivated_error(error_detail):
                            return {
                                "ok": False,
                                "error": f"get_rt failed: {error_detail}",
                                "lifecycle_status": "banned",
                                "summary_updates": {"deactivated_reason": error_detail},
                                "error_type": "account_banned",
                            }
                        if _is_chatgpt_session_stale_error(error_detail):
                            refreshed = _refresh_chatgpt_session_after_get_rt(account, proxy=proxy)
                            if refreshed.get("ok"):
                                refreshed_data = dict(refreshed.get("data") or {})
                                return {
                                    "ok": False,
                                    "error": f"get_rt failed: {error_detail}",
                                    "error_type": "session_stale_refreshed",
                                    "data": {
                                        "access_token": str(refreshed_data.get("access_token") or ""),
                                        "refresh_token": str(refreshed_data.get("refresh_token") or ""),
                                        "id_token": str(refreshed_data.get("access_token") or ""),
                                        "session_token": str(refreshed_data.get("session_token") or ""),
                                        "account_id": str(account.user_id or extra.get("account_id") or extra.get("chatgpt_account_id") or ""),
                                        "email": account.email,
                                        "record_har_path": record_har_path or "",
                                        "token_backup_path": "",
                                        "session_refresh_status": "refreshed",
                                        "account_state": refreshed_data.get("account_state") or {},
                                    },
                                }
                        return {"ok": False, "error": f"获取rt失败: {error_detail}"}

                    refresh_token = str(result.get("refresh_token") or "")
                    access_token = str(result.get("access_token") or "")
                    log_fn(
                        f"  获取rt成功: {account.email}"
                        f" access_token={access_token[:20]}..."
                        f" refresh_token={'有' if refresh_token else '无'}"
                    )
                    backup_json_path = _save_get_rt_token_backup(account, result, action_label="get_rt")
                    if backup_json_path:
                        log_fn(f"  获取rt token 本地备份: {backup_json_path}")

                    return {
                        "ok": True,
                        "data": {
                            "access_token": access_token,
                            "refresh_token": refresh_token,
                            "id_token": str(result.get("id_token") or ""),
                            "account_id": str(result.get("account_id") or ""),
                            "email": account.email,
                            "record_har_path": record_har_path or "",
                            "token_backup_path": backup_json_path,
                            "message": "refresh_token 获取成功" if refresh_token else "access_token 获取成功（无 refresh_token）",
                        },
                    }
                finally:
                    if har_context is not None:
                        try:
                            har_context.close()
                            log_fn(f"  get_rt HAR saved: {record_har_path}")
                        except Exception as exc:
                            log_fn(f"  get_rt HAR context close failed: {exc}")

        except Exception as exc:
            log_fn(f"  获取rt异常: {exc}")
            return {"ok": False, "error": f"获取rt异常: {exc}"}
        finally:
            if phone_callback is not None and hasattr(phone_callback, "cleanup"):
                try:
                    phone_callback.cleanup()
                except Exception as exc:
                    log_fn(f"  get_rt phone callback cleanup failed: {exc}")
            if acquired_profile_id:
                try:
                    from application.bitbrowser_profiles import release_acquired_profile
                    release_acquired_profile(acquired_profile_id, log_fn=log_fn)
                except Exception:
                    pass

    def _handle_get_rt_bypass(self, account: Account, params: dict) -> dict:
        """通过浏览器 OAuth 获取 refresh_token（session/select 拦截绕过手机验证）。

        与 _handle_get_rt 的区别：
          - 不接真实手机号，不调 smspool/smsapi
          - 用 Playwright route 拦截 POST session/select 响应，
            把 phone_otp_* 替换为 consent 类型，让浏览器直接跳 consent
          - 邮箱 OTP 仍需真实接码

        参数：
          browser_mode: 浏览器模式
        """
        log_fn = getattr(self, "log", print)
        cancel_fn = getattr(self, "_cancel_check_fn", None)

        browser_mode = str(params.get("browser_mode") or "camoufox_headed")
        extra = account.extra or {}
        region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
        proxy = _resolve_action_proxy(
            self.config.proxy if self.config else None,
            region=region,
            log_fn=log_fn,
            action_label="获取rt(绕过)",
        )

        if not account.password:
            return {"ok": False, "error": "账号缺少密码，无法进行 OAuth 登录"}

        acquired_profile_id = ""
        bit_profile_id = ""

        try:
            from platforms._browser_backend import parse_checkout_mode
            from platforms.chatgpt.browser_register import (
                ChatGPTBrowserRegister,
                _build_proxy_config,
                _do_codex_oauth,
            )
            from platforms.chatgpt.browser_get_rt import setup_phone_otp_skip_interception
            from platforms.chatgpt.oauth import generate_oauth_url
            from platforms.chatgpt.constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE

            # 预生成 OAuth 参数（curl fallback 用）
            oauth_start = generate_oauth_url(
                redirect_uri=CODEX_REDIRECT_URI,
                scope=CODEX_SCOPE,
                client_id=CODEX_CLIENT_ID,
            )

            if str(browser_mode or "").startswith("bitbrowser_"):
                from application.bitbrowser_profiles import (
                    acquire_profile_for_browser_mode,
                )
                bit_profile_id, acquired_profile_id = acquire_profile_for_browser_mode(
                    browser_mode,
                    fallback=bit_profile_id,
                    log_fn=log_fn,
                )

            backend_config = parse_checkout_mode(browser_mode, bit_profile_id=bit_profile_id)
            totp_secret = _account_secret_value(account, "totp_secret")
            if str(account.password or "").strip() and totp_secret:
                otp_callback = None
                log_fn("  获取rt(绕过): 检测到账号已保存密码和 2FA，本次使用密码 + TOTP，不初始化邮箱 OTP 服务")
            else:
                otp_callback, otp_error = self._build_get_rt_mailbox_otp_callback(account, log_fn, proxy)
                if not otp_callback:
                    return {"ok": False, "error": f"获取rt失败: {otp_error}"}

            log_fn(
                f"获取rt(绕过): {account.email}, browser_mode={browser_mode}, "
                f"proxy={_proxy_log_value(proxy)}"
            )

            reg = ChatGPTBrowserRegister(
                headless=backend_config.is_headless,
                proxy=proxy,
                log_fn=log_fn,
                backend_config=backend_config,
                totp_secret=totp_secret,
            )

            if reg.backend_config.is_bitbrowser:
                launch_opts = {"headless": reg.backend_config.is_headless}
            else:
                cam_proxy = _build_proxy_config(reg.proxy)
                launch_opts = {"headless": reg.headless}
                if cam_proxy:
                    launch_opts["proxy"] = cam_proxy

            with reg._open_browser(launch_opts) as browser:
                page = browser.new_page()
                setup_phone_otp_skip_interception(page, log=log_fn)
                log_fn("  获取rt(绕过): session/select 拦截器已就绪（phone_otp→consent）")

                if callable(cancel_fn) and cancel_fn():
                    return {"ok": False, "error": "任务已取消"}

                result = _do_codex_oauth(
                    page, {}, account.email, account.password,
                    otp_callback,
                    None,
                    proxy, log_fn,
                    oauth_start=oauth_start,
                    totp_secret=totp_secret,
                )

                # ★ Fallback: curl 补全会话 (workspace/select → callback)
                if not isinstance(result, dict) or not result.get("access_token"):
                    import time as _time, json as _json, re as _re
                    from platforms.chatgpt.browser_register import _get_cookies
                    cookies_dict = _get_cookies(page)
                    log_fn("  获取rt(绕过): _do_codex_oauth 退出，curl 补全...")
                    try:
                        import curl_cffi.requests as _curl_requests
                        s = _curl_requests.Session()
                        if proxy:
                            s.proxies = {"http": proxy, "https": proxy}
                            log_fn(f"  获取rt(绕过): curl 补全使用代理 {_proxy_log_value(proxy)}")
                        cookie_parts = [f'{k}={v}' for k, v in cookies_dict.items() if v]
                        cookie_header = '; '.join(cookie_parts)
                        headers = {
                            "accept": "application/json",
                            "origin": "https://auth.openai.com",
                            "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            "cookie": cookie_header,
                        }
                        workspace_id = ""
                        # Try 1: client_auth_session_dump
                        dump_resp = s.get("https://auth.openai.com/api/accounts/client_auth_session_dump",
                            headers=headers, timeout=30, impersonate="chrome")
                        log_fn(f"  获取rt(绕过): client_auth_session_dump -> {dump_resp.status_code}")
                        if dump_resp.status_code < 400:
                            dump_data = dump_resp.json() if dump_resp.text else {}
                            workspaces = dump_data.get("workspaces") or []
                            if workspaces:
                                workspace_id = str(workspaces[0].get("id") or "")
                            log_fn(f"  获取rt(绕过): dump workspaces={len(workspaces)}")

                        # Try 2: use account's user_id as workspace_id
                        if not workspace_id and account.user_id:
                            workspace_id = account.user_id
                            log_fn(f"  获取rt(绕过): 尝试 user_id={workspace_id[:20]}...")

                        # Try 3: POST workspace/select with each candidate
                        for ws_candidate in [workspace_id] if workspace_id else []:
                            ws_resp = s.post(
                                "https://auth.openai.com/api/accounts/workspace/select",
                                data=_json.dumps({"workspace_id": ws_candidate}),
                                headers={**headers, "content-type": "application/json"},
                                allow_redirects=False, timeout=30, impersonate="chrome",
                            )
                            log_fn(f"  获取rt(绕过): workspace/select({ws_candidate[:16]}...) -> {ws_resp.status_code}")
                            if ws_resp.status_code < 400:
                                ws_data = ws_resp.json() if ws_resp.text else {}
                                cb_url = str(ws_data.get("continue_url") or "")
                                # Also check Location header
                                if not cb_url:
                                    cb_url = str(ws_resp.headers.get("Location") or "")
                                if "code=" in cb_url or "localhost:1455" in cb_url:
                                    m = _re.search(r'state=([^&\s]+)', cb_url)
                                    cb_state = m.group(1) if m else oauth_start.state
                                    from platforms.chatgpt.oauth import submit_callback_url
                                    result_json = submit_callback_url(
                                        callback_url=cb_url, expected_state=cb_state,
                                        code_verifier=oauth_start.code_verifier,
                                        redirect_uri=oauth_start.redirect_uri,
                                        client_id=oauth_start.client_id, proxy_url=proxy,
                                    )
                                    result = _json.loads(result_json)
                                    log_fn("  获取rt(绕过): curl workspace/select 补全成功!")
                                    break
                    except Exception as curl_exc:
                        log_fn(f"  获取rt(绕过): curl 补全异常: {curl_exc}")

                if not isinstance(result, dict) or not result.get("access_token"):
                    error_detail = "OAuth 未返回 token"
                    if isinstance(result, dict):
                        error_detail = str(result.get("error") or result.get("detail") or error_detail)
                    return {"ok": False, "error": f"获取rt失败: {error_detail}"}

                refresh_token = str(result.get("refresh_token") or "")
                access_token = str(result.get("access_token") or "")
                id_token = str(result.get("id_token") or "")
                result_data = dict(result)
                id_token_claims = {}
                try:
                    from platforms.chatgpt.oauth import _jwt_claims_no_verify
                    id_token_claims = _jwt_claims_no_verify(id_token)
                    if id_token_claims:
                        result_data["id_token_claims"] = id_token_claims
                except Exception:
                    id_token_claims = {}
                profile = {}
                try:
                    from platforms.chatgpt.browser_oauth import _fetch_profile
                    profile = _fetch_profile(access_token, proxy=proxy)
                    if profile:
                        result_data["profile"] = profile
                        result_data["remote_user"] = profile
                except Exception as exc:
                    log_fn(f"  获取rt: profile 拉取失败（忽略）: {exc}")
                resolved_email = str(
                    result_data.get("email")
                    or (profile.get("email") if isinstance(profile, dict) else "")
                    or account.email
                )
                result_data.update(
                    {
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "id_token": id_token,
                        "account_id": str(result.get("account_id") or ""),
                        "email": resolved_email,
                        "message": "refresh_token 获取成功" if refresh_token else "access_token 获取成功（无 refresh_token）",
                    }
                )
                log_fn(
                    f"  获取rt成功: {account.email}"
                    f" access_token={access_token[:20]}..."
                    f" refresh_token={'有' if refresh_token else '无'}"
                )
                backup_json_path = _save_get_rt_token_backup(account, result_data, action_label="get_rt_bypass")
                if backup_json_path:
                    result_data["token_backup_path"] = backup_json_path
                    log_fn(f"  获取rt token 本地备份: {backup_json_path}")

                return {
                    "ok": True,
                    "data": result_data,
                }

        except Exception as exc:
            log_fn(f"  获取rt异常: {exc}")
            return {"ok": False, "error": f"获取rt异常: {exc}"}
        finally:
            if acquired_profile_id:
                try:
                    from application.bitbrowser_profiles import release_acquired_profile
                    release_acquired_profile(acquired_profile_id, log_fn=log_fn)
                except Exception:
                    pass

    def _build_turnstile_solver_for_checkout(self):
        """构造给 Camoufox checkout 用的验证码求解回调。

        PayPal security challenge 只使用 YesCaptcha；如未配置可用 YesCaptcha，则返回
        None，让 checkout 流程退化为人工等待。
        """
        log_fn = getattr(self, "_log_fn", print)
        try:
            if not self._has_configured_captcha("yescaptcha_api"):
                log_fn("未启用验证码自动求解（YesCaptcha 未配置）")
                return None
            captcha_solver = self._make_captcha(provider_key="yescaptcha_api")
        except Exception as exc:
            log_fn(f"未启用验证码自动求解（YesCaptcha 初始化失败: {exc}）")
            return None
        log_fn("已启用验证码自动求解，provider: YesCaptcha")

        def _solver(page_url: str, site_key: str, challenge_type: str = "turnstile") -> str:
            if challenge_type == "recaptcha_v2":
                return captcha_solver.solve_recaptcha_v2(page_url, site_key)
            # **PayPal 实战证据** (`@tools/captures/checkout-20260526-003842-z6qrov0qi0_edu.hsxhome.com.har`
            # entry 347)：``paypal.com/pay/`` 风控页是 hCaptcha (iframe src 含
            # ``hcaptcha_fph.html?siteKey=...``)，必须走 ``solve_hcaptcha`` 才能拿到
            # 可注入到 ``form[name=challenge]`` 里的 ``g-recaptcha-response`` token。
            if challenge_type == "hcaptcha":
                return captcha_solver.solve_hcaptcha(page_url, site_key)
            return captcha_solver.solve_turnstile(page_url, site_key)

        return _solver


    def _handle_extract_ba_link(self, account: Account, params: dict) -> dict:
        """双 IP 撞链提取 PayPal BA，成功后写回账号 pp_ba_token。"""
        self.raise_if_cancelled()
        extra = account.extra or {}
        access_token = str(extra.get("access_token") or account.token or "").strip()
        cookies = str(extra.get("cookies") or "").strip()
        email = str(account.email or "").strip()
        log_fn = getattr(self, "_log_fn", print)

        from application.ba_link_extract import extract_ba_link
        from application.pp_plus_ba import get_pp_plus_worker
        from core.db import AccountModel, engine
        from sqlmodel import Session, select

        result = extract_ba_link(
            access_token=access_token,
            cookies=cookies,
            email=email,
            billing_proxy=str(params.get("billing_proxy") or params.get("billing_ip") or ""),
            promo_proxy=str(params.get("promo_proxy") or params.get("promo_ip") or ""),
            billing_country=str(params.get("billing_country") or params.get("country") or "US"),
            promo_country=str(params.get("promo_country") or "TR"),
            billing_currency=str(params.get("billing_currency") or params.get("currency") or ""),
            confirm_mode=str(params.get("confirm_mode") or "pm"),
            promo_create_mode=str(params.get("promo_create_mode") or "update_after_checkout"),
            max_attempts=_int_param(params, "max_attempts", 20),
            log_fn=log_fn,
            cancel_check=getattr(self, "_cancel_check_fn", None),
        )
        if not result.get("ok"):
            return {
                "ok": False,
                "error": str(result.get("error") or "提取 BA 链失败"),
                "data": result.get("data") or {"steps": result.get("steps") or {}},
            }

        ba_token = str(result.get("ba_token") or "").strip()
        ba_url = str(result.get("ba_url") or result.get("redirect_url") or "").strip()
        saved = {}
        account_id = 0
        try:
            account_id = int(params.get("account_id") or params.get("_account_id") or 0)
        except Exception:
            account_id = 0
        if account_id <= 0:
            with Session(engine) as session:
                model = session.exec(
                    select(AccountModel).where(
                        AccountModel.platform == "chatgpt",
                        AccountModel.email == email,
                    )
                ).first()
                if model is not None:
                    account_id = int(model.id or 0)
        if ba_token:
            if account_id <= 0:
                return {
                    "ok": False,
                    "error": "提取成功但无法定位账号 ID 写回 BA",
                    "data": {
                        "ba_token": ba_token,
                        "ba_url": ba_url,
                        "url": ba_url,
                        "steps": result.get("steps") or {},
                    },
                }
            try:
                saved = get_pp_plus_worker().save_ba_token(account_id, ba_token)
                log_fn(f"[BA提取] 已写回账号#{account_id} BA: {ba_token}")
            except Exception as exc:
                log_fn(f"[BA提取] 写回 BA 失败: {exc}")
                return {
                    "ok": False,
                    "error": f"提取成功但写回失败: {exc}",
                    "data": {
                        "ba_token": ba_token,
                        "ba_url": ba_url,
                        "url": ba_url,
                        "steps": result.get("steps") or {},
                    },
                }

        data = dict(result.get("data") or {})
        data.update(
            {
                "ba_token": ba_token,
                "pp_ba_token": ba_token,
                "ba_url": ba_url,
                "url": ba_url,
                "cashier_url": ba_url,
                "message": f"已提取并保存 BA 链 {ba_token}",
                "saved": saved,
                "account_id": account_id,
                "steps": result.get("steps") or data.get("steps") or {},
                "billing_country": result.get("billing_country"),
                "promo_country": result.get("promo_country"),
                "amount": result.get("amount"),
                "cs_id": result.get("cs_id"),
            }
        )
        return {"ok": True, "data": data, "error": ""}

    def _handle_generate_link(self, account: Account, params: dict) -> dict:
        """Handle generate_link capability for ChatGPT.

        **行为变更**（"打开支付链接"语义）：账号 ``extra`` 里已存了
        ``cashier_url`` 时优先把它**直接返回**——前端拿到 URL 就在新标签
        页打开。这样"打开支付链接"按钮就跟字面意思一致了：注册阶段已生成
        过的链接直接复用，不再每次都重新打 ChatGPT 后端 API 创建新会话。

        ``params`` 里若显式传 ``regenerate=true`` 则跳过这条路径，强制重新
        生成（用于链接过期 / 想要换 country/currency 等场景）。
        """
        self.raise_if_cancelled()
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        regenerate = _bool_param(params, "regenerate", False)
        if not regenerate:
            existing_url = str(
                extra.get("cashier_url")
                or (extra.get("account_overview") or {}).get("cashier_url")
                or ""
            ).strip()
            if existing_url:
                getattr(self, "_log_fn", print)(
                    f"复用账号已有 cashier_url（不重新生成）: {existing_url}"
                )
                return {
                    "ok": True,
                    "data": {
                        "url": existing_url,
                        "checkout_url": existing_url,
                        "cashier_url": existing_url,
                        "plan": params.get("plan", "plus"),
                        "auto_checkout": False,
                        "message": "支付链接已存在，直接打开",
                        "reused": True,
                    },
                }

        class _A: pass
        a = _A()
        a.email = account.email
        a.password = account.password
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")

        record_har = _bool_param(params, "record_har", False)
        record_har_path = _build_checkout_har_path(account.email) if record_har else None

        if _bool_param(params, "use_ppboom", False) or _bool_param(params, "ppboom_enabled", False):
            try:
                from application.ppboom import run_ppboom_paypal_link

                ppboom_params = dict(params)
                if record_har_path:
                    ppboom_params["record_har_path"] = record_har_path
                data = run_ppboom_paypal_link(
                    account,
                    ppboom_params,
                    log_fn=getattr(self, "_log_fn", print),
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "data": {},
                    "error": f"PPBoom payment link failed: {exc}",
                }
            return {
                "ok": bool(data.get("ok")),
                "data": data,
                "error": str(data.get("error") or ""),
            }

        from platforms.chatgpt import payment as payment_module
        plan = params.get("plan", "plus")
        country = params.get("country", "ID")
        currency = params.get("currency") or None
        # 用 Stripe payment_pages/init 协议生成 cashier_url（accessToken →
        # pay.openai.com 长链，纯协议、不开浏览器拿 cashier 链）。仅 plus 生效。
        use_stripe_init = _bool_param(params, "use_stripe_init", False)
        # 短链：checkout_ui_mode=custom → chatgpt.com/checkout/openai_llc 短链。仅 plus。
        use_short_link = _bool_param(params, "use_short_link", False)
        # 账单地址来源（meiguodizhi.com 接口）："US" 走 ``/``，"JP" 走 ``/jp-address``。
        # 默认 US 保持向下兼容；其它值在 fetch_billing_address 里 fallback US。
        address_region = str(params.get("address_region") or "US").strip().upper() or "US"
        auto_checkout = _bool_param(params, "auto_checkout", True)
        payment_method = str(params.get("payment_method") or "paypal").strip().lower()
        headless = _bool_param(params, "headless", False)
        checkout_timeout = _int_param(params, "checkout_timeout", 180)
        checkout_hold_seconds = _optional_int_param(params, "checkout_hold_seconds")
        checkout_mode = str(params.get("checkout_mode") or "").strip().lower()
        if not checkout_mode:
            checkout_mode = "camoufox_headless" if headless else "camoufox_headed"
        # bitbrowser_* 模式下必须有 profile ID。表单输入优先于环境变量。
        bit_profile_id = str(params.get("bit_profile_id") or "").strip()
        if not bit_profile_id:
            bit_profile_id = os.environ.get("BIT_PROFILE_ID", "").strip()
        # 把 checkout_mode 翻成 BrowserBackendConfig；protocol 模式不需要 backend
        # （这里给 None，下游 _run_camoufox 不会被调用到）。
        backend_config: BrowserBackendConfig | None = None
        # acquired_profile_id 记录"是从池里 acquire 出来的"，跑完要 release。
        # 表单/环境变量传进来的不在池里，不需要 release。
        acquired_profile_id: str = ""
        if checkout_mode.startswith("bitbrowser_"):
            window_mode = checkout_mode[len("bitbrowser_"):]
            # 优先从 BitBrowser profile 池里 acquire 一个最少使用的 profile。
            # 池为空时回落到表单/环境变量提供的单一 ID（保持向后兼容）。
            from application.bitbrowser_profiles import (
                bitbrowser_profile_pool,
                BitBrowserProfilePoolEmpty,
            )
            try:
                resolved_profile_id = bitbrowser_profile_pool.acquire_or(
                    fallback=bit_profile_id
                )
                # 判断是不是真的从池里 acquire 的（影响 release）：池里有这个
                # ID 就视为"从池里出来的"，否则视为 fallback。
                pool_ids = {
                    item["profile_id"]
                    for item in bitbrowser_profile_pool.list_profiles()
                }
                if resolved_profile_id in pool_ids:
                    acquired_profile_id = resolved_profile_id
            except BitBrowserProfilePoolEmpty:
                # 池空 + 没 fallback → fail-fast，避免下到 BitBrowser API 才报错
                return {
                    "ok": False,
                    "error": (
                        "checkout_mode=bitbrowser_* 需要在「设置 → BitBrowser」"
                        "里添加 profile ID，或在表单里填写 BitBrowser Profile ID"
                        "（也可设置 BIT_PROFILE_ID 环境变量）"
                    ),
                }
            backend_config = BrowserBackendConfig.bitbrowser(
                profile_id=resolved_profile_id,
                window_mode=window_mode,
                api_url=os.environ.get("BIT_API_URL", "").strip() or None,
                api_token=os.environ.get("BIT_API_TOKEN", "").strip() or None,
            )
            getattr(self, "_log_fn", print)(
                f"BitBrowser profile 已选择: {resolved_profile_id} "
                f"(window_mode={window_mode}, "
                f"来源={'profile 池' if acquired_profile_id else '表单/环境变量'})"
            )
        elif checkout_mode in ("camoufox_headless", "camoufox_headed"):
            backend_config = BrowserBackendConfig.camoufox(
                headless=(checkout_mode == "camoufox_headless"),
            )
        # 解析 SMS 号码池：多行 +phone----relay_url。失败行会被静默忽略，
        # 这里只保留结构化后的非空列表，避免后续 stage / camoufox 反复字符串处理。
        sms_pool_raw = str(params.get("sms_pool") or "")
        try:
            sms_pool = payment_module.parse_sms_pool(sms_pool_raw)
        except Exception as exc:  # 防御性：解析失败也不应阻塞 checkout
            sms_pool = []
            getattr(self, "_log_fn", print)(f"SMS 号码池解析失败（忽略）: {exc}")
        if sms_pool_raw and not sms_pool:
            getattr(self, "_log_fn", print)(
                "警告：sms_pool 提供了内容但没解析出任何条目，请按 `+phone----relay_url` 格式排查"
            )
        elif sms_pool:
            getattr(self, "_log_fn", print)(f"SMS 号码池已加载 {len(sms_pool)} 条")
        checkout_proxy = None

        # Manually construct basic cookie in case old accounts don't have complete cookie string
        if not a.cookies and a.session_token:
            a.cookies = f"__Secure-next-auth.session-token={a.session_token}"

        getattr(self, "_log_fn", print)("生成 ChatGPT 测试支付链接不使用代理")
        if plan == "plus":
            if use_short_link:
                getattr(self, "_log_fn", print)(
                    "cashier_url 走短链模式（checkout_ui_mode=custom → chatgpt.com/checkout/openai_llc）"
                )
            elif use_stripe_init:
                getattr(self, "_log_fn", print)(
                    "cashier_url 走 Stripe init 协议长链（accessToken → pay.openai.com，纯协议）"
                )
            generate_kwargs = {}
            if use_stripe_init or use_short_link:
                generate_kwargs["use_stripe_init"] = use_stripe_init
                generate_kwargs["use_short_link"] = use_short_link
            url = payment_module.generate_plus_link(
                a, proxy=None, country=country, currency=currency, **generate_kwargs
            )
        else:
            url = payment_module.generate_team_link(a, proxy=None, country=country, currency=currency)
        self.raise_if_cancelled()

        cashier_url = url
        paypal_authorize_url = ""
        paypal_protocol_extract = None
        checkout_automation = None
        if url and auto_checkout:
            checkout_proxy = proxy
            if not checkout_proxy:
                proxy_region = str(params.get("proxy_region") or country or getattr(account, "region", "") or "").strip().upper()
                checkout_proxy = proxy_pool.get_next(region=proxy_region)
            if checkout_proxy:
                getattr(self, "_log_fn", print)(f"Camoufox checkout 使用代理: {_mask_proxy(checkout_proxy)}")
            else:
                getattr(self, "_log_fn", print)("Camoufox checkout 未配置代理")
            getattr(self, "_log_fn", print)("支付链接已生成，开始自动 PayPal checkout")
            getattr(self, "_log_fn", print)(f"checkout 模式: {checkout_mode}")
            # 是否启用 YesCaptcha 远端求解（前端弹窗里的开关）。
            # 关闭时 turnstile_solver 强制为 None，payment 模块的 captcha
            # 路径会退化为"代码鼠标点击 + 10s 等待跳转"，避免反复在
            # YesCaptcha 不识别的 sitekey 上烧配额。
            use_captcha_service = _bool_param(params, "use_captcha_service", True)
            if use_captcha_service:
                turnstile_solver = self._build_turnstile_solver_for_checkout()
            else:
                getattr(self, "_log_fn", print)(
                    "已禁用 YesCaptcha 求解（弹窗开关），captcha 出现时仅自动点击 + 等 10s"
                )
                turnstile_solver = None
            log_fn = getattr(self, "_log_fn", print)

            protocol_extract_failed = False
            if plan == "plus" and use_stripe_init and checkout_mode != "protocol":
                gateway_url = os.environ.get("PAYPAL_PROTOCOL_GATEWAY_URL", "").strip()
                if gateway_url:
                    paypal_protocol_extract = payment_module.extract_paypal_authorize_link_go(
                        access_token=a.access_token,
                        proxy=checkout_proxy,
                        gateway_url=gateway_url,
                        timeout=checkout_timeout,
                        log_fn=log_fn,
                    )
                else:
                    gateway_error = (
                        "未配置 PAYPAL_PROTOCOL_GATEWAY_URL，协议长链模式需要先启动 Go gateway；"
                        "当前不会回落 Python Stripe direct confirm"
                    )
                    log_fn(f"协议长链模式：{gateway_error}")
                    paypal_protocol_extract = {
                        "ok": False,
                        "status": "failed",
                        "paypal_authorize_url": "",
                        "error": gateway_error,
                        "protocol_backend": "go",
                    }
                if paypal_protocol_extract.get("ok"):
                    paypal_authorize_url = str(paypal_protocol_extract.get("paypal_authorize_url") or "")
                    if paypal_authorize_url:
                        url = paypal_authorize_url
                        log_fn("协议长链模式：已生成 PayPal 授权长链接，交给浏览器自动填写流程")
                    else:
                        protocol_extract_failed = True
                        checkout_automation = {
                            "ok": False,
                            "status": "failed",
                            "error": "协议提取成功但未返回 PayPal 授权长链接",
                            "protocol_extract": paypal_protocol_extract,
                        }
                else:
                    protocol_extract_failed = True
                    checkout_automation = paypal_protocol_extract
                    log_fn(
                        "协议长链模式提取 PayPal 授权长链接失败: "
                        + str(paypal_protocol_extract.get("error", "") or "unknown error")
                    )

            def _run_camoufox(headless_flag: bool):
                # 名字保留 _run_camoufox 兼容老日志/调用方，实际后端由
                # backend_config 决定（Camoufox / BitBrowser）。
                backend_label = (
                    f"BitBrowser({backend_config.window_mode})"
                    if backend_config and backend_config.is_bitbrowser
                    else f"Camoufox(headless={headless_flag})"
                )
                log_fn(
                    f"切换到独立线程执行 checkout backend={backend_label}"
                )
                return _run_sync_checkout_isolated(
                    payment_module.complete_paypal_checkout,
                    checkout_url=url,
                    cookies_str=a.cookies,
                    proxy=checkout_proxy,
                    email=account.email,
                    payment_method=payment_method,
                    headless=headless_flag,
                    timeout=checkout_timeout,
                    hold_seconds=checkout_hold_seconds,
                    log_fn=log_fn,
                    cancel_check=self.is_cancel_requested,
                    turnstile_solver=turnstile_solver,
                    record_har_path=record_har_path,
                    sms_pool=sms_pool,
                    backend_config=backend_config,
                    phone_swap_callback=params.get("phone_swap_callback"),
                    address_region=address_region,
                )

            def _run_protocol():
                log_fn("启动协议模式 checkout")
                return _run_sync_checkout_isolated(
                    payment_module.complete_paypal_checkout_protocol,
                    checkout_url=url,
                    cookies_str=a.cookies,
                    proxy=checkout_proxy,
                    email=account.email,
                    payment_method=payment_method,
                    timeout=checkout_timeout,
                    log_fn=log_fn,
                    cancel_check=self.is_cancel_requested,
                    turnstile_solver=turnstile_solver,
                    sms_pool=sms_pool,
                    address_region=address_region,
                )

            if checkout_mode == "protocol":
                # 协议模式失败时**直接报错**，不再自动回落 camoufox。
                # 理由：camoufox 兜底会掩盖协议链的真实失败原因，让调试变难；
                # 而且每次跑都要等 camoufox 启动 + 浏览器自动化，浪费时间。
                # 真要 fallback 的话，由前端在外层切换 checkout_mode 重新发起。
                checkout_automation = _run_protocol()
                if checkout_automation and not checkout_automation.get("ok"):
                    proto_err = str(checkout_automation.get("error", "") or "").strip()
                    log_fn(
                        "协议模式 checkout 失败（stage="
                        + str(checkout_automation.get("stage", "?"))
                        + "），不再回落 camoufox（便于排查）"
                        + (f"；原因: {proto_err}" if proto_err else "")
                    )
            else:
                try:
                    if not protocol_extract_failed:
                        checkout_automation = _run_camoufox(
                            headless_flag=(checkout_mode == "camoufox_headless")
                        )
                finally:
                    # BitBrowser 池里 acquire 出来的 profile，跑完后释放计数，
                    # 让下一次并发能挑到当前没在用的 profile。表单/环境变量
                    # 传的 ID 不在池里，acquired_profile_id 是空字符串，
                    # release 是 no-op。
                    if acquired_profile_id:
                        try:
                            from application.bitbrowser_profiles import (
                                bitbrowser_profile_pool,
                            )
                            bitbrowser_profile_pool.release(acquired_profile_id)
                            log_fn(
                                f"BitBrowser profile 池已释放: {acquired_profile_id}"
                            )
                        except Exception as exc:
                            log_fn(f"BitBrowser profile 池释放失败（忽略）: {exc}")
            self.raise_if_cancelled()
            if checkout_automation.get("ok"):
                getattr(self, "_log_fn", print)("PayPal checkout 自动流程已提交")
            else:
                checkout_error = str(checkout_automation.get("error", "") or "PayPal checkout automation failed")
                getattr(self, "_log_fn", print)(f"PayPal checkout 自动流程失败: {checkout_error}")

        checkout_ok = bool(checkout_automation and checkout_automation.get("ok"))
        action_ok = bool(url) if not auto_checkout else bool(url and checkout_ok)
        action_error = ""
        if url and auto_checkout and not checkout_ok:
            action_error = str(
                (checkout_automation or {}).get("error", "")
                or "PayPal checkout automation failed"
            )

        data = {
            "url": url,
            "checkout_url": url,
            "cashier_url": cashier_url,
            "paypal_authorize_url": paypal_authorize_url,
            "plan": plan,
            "country": country,
            "currency": currency or "",
            "payment_method": payment_method,
            "auto_checkout": auto_checkout,
            "headless": headless,
            "checkout_mode": checkout_mode,
            "proxy_used": checkout_proxy or "",
            "record_har_path": record_har_path or "",
            "message": (
                "Payment link generated, PayPal checkout automation submitted."
                if checkout_ok
                else (
                    "Payment link generated, but PayPal checkout automation failed."
                    if url and auto_checkout
                    else "Payment link generated."
                )
            ),
        }
        if paypal_protocol_extract is not None:
            data["paypal_protocol_extract"] = paypal_protocol_extract
        if checkout_automation is not None:
            data["checkout_automation"] = checkout_automation

        return {
            "ok": action_ok,
            "data": data,
            "error": action_error,
        }

    
