"""账号生命周期管理 — 定时检测、自动续期、过期预警。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlmodel import Session, select

from core.account_graph import load_account_graphs, patch_account_graph
from core.base_platform import AccountStatus, RegisterConfig
from core.db import AccountModel, AccountOverviewModel, engine
from core.platform_accounts import build_platform_account
from core.registry import get

logger = logging.getLogger(__name__)
LIFECYCLE_ACCOUNT_CHECK_ENABLED_KEY = "lifecycle_account_check_enabled"
LIFECYCLE_TOKEN_REFRESH_ENABLED_KEY = "lifecycle_token_refresh_enabled"
LIFECYCLE_TRIAL_WARNING_ENABLED_KEY = "lifecycle_trial_warning_enabled"
LIFECYCLE_EXTERNAL_SYNC_ENABLED_KEY = "lifecycle_external_sync_enabled"
LIFECYCLE_SERVICE_DEFAULTS = {
    LIFECYCLE_ACCOUNT_CHECK_ENABLED_KEY: True,
    LIFECYCLE_TOKEN_REFRESH_ENABLED_KEY: True,
    LIFECYCLE_TRIAL_WARNING_ENABLED_KEY: True,
    LIFECYCLE_EXTERNAL_SYNC_ENABLED_KEY: False,
}


def _bool_config(value: str | None, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "启用", "开启"}


def is_lifecycle_manager_enabled() -> bool:
    """Return whether any periodic lifecycle service should auto-start."""
    return any(get_lifecycle_service_flags().values())


def get_lifecycle_service_flags() -> dict[str, bool]:
    """Return per-service lifecycle switches with safe defaults."""
    try:
        from core.config_store import config_store

        return {
            key: _bool_config(config_store.get(key, "true" if default else "false"), default)
            for key, default in LIFECYCLE_SERVICE_DEFAULTS.items()
        }
    except Exception:
        return dict(LIFECYCLE_SERVICE_DEFAULTS)


def _external_upload_targets_config() -> dict[str, str | bool]:
    """读取外部面板配置；同步/上传必须按已配置目标精确执行。"""
    try:
        from core.config_store import config_store

        cpa_api_url = str(config_store.get("cpa_api_url", "") or "").strip()
        cpa_api_key = str(config_store.get("cpa_api_key", "") or "").strip()
        sub2api_url = str(config_store.get("sub2api_url", "") or "").strip()
    except Exception:
        cpa_api_url, cpa_api_key, sub2api_url = "", "", ""
    return {
        "cpa_api_url": cpa_api_url,
        "cpa_api_key": cpa_api_key,
        "sub2api_url": sub2api_url,
        "cpa_enabled": bool(cpa_api_url and cpa_api_key),
        "sub2api_enabled": bool(sub2api_url),
    }


def _external_upload_target_label() -> str:
    """返回后台同步目标名称；未配置任何外部目标时返回空字符串。"""
    config = _external_upload_targets_config()
    targets: list[str] = []
    if config.get("cpa_enabled"):
        targets.append("CPA")
    if config.get("sub2api_enabled"):
        targets.append("SUB2API")
    return "+".join(targets)


def _is_k12_account_graph(graph: dict[str, Any]) -> bool:
    """K12 账号由专用流程上传，本后台普通 SUB2API 同步不再处理。"""
    overview = graph.get("overview") if isinstance(graph, dict) else {}
    overview = overview if isinstance(overview, dict) else {}
    legacy_extra = overview.get("legacy_extra") if isinstance(overview.get("legacy_extra"), dict) else {}
    if overview.get("k12_session") or legacy_extra.get("k12_session"):
        return True
    if str(overview.get("k12_workspace_id") or legacy_extra.get("k12_workspace_id") or "").strip():
        return True
    for item in graph.get("credentials") or []:
        if not isinstance(item, dict) or item.get("scope") != "platform":
            continue
        if str(item.get("key") or "").strip().lower() in {"plan_type", "plantype"} and str(item.get("value") or "").strip().lower() == "k12":
            return True
    return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _utcnow_ts() -> int:
    return int(_utcnow().timestamp())


# ---------------------------------------------------------------------------
# Account validity check
# ---------------------------------------------------------------------------

def check_accounts_validity(
    *,
    platform: str = "",
    limit: int = 100,
    log_fn=None,
) -> dict[str, int]:
    """Check validity of active accounts. Returns {valid, invalid, error, skipped}."""
    log = log_fn or logger.info

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        accounts = session.exec(q.limit(limit)).all()
        graphs = load_account_graphs(session, [int(a.id) for a in accounts if a.id])

    # Only check accounts that are in an active lifecycle state
    active_statuses = {"registered", "trial", "subscribed"}
    targets = [
        a for a in accounts
        if graphs.get(int(a.id or 0), {}).get("lifecycle_status") in active_statuses
    ]

    results = {"valid": 0, "invalid": 0, "error": 0, "skipped": len(accounts) - len(targets)}
    for acc in targets:
        try:
            platform_cls = get(acc.platform)
            plugin = platform_cls(config=RegisterConfig())
            with Session(engine) as session:
                current = session.get(AccountModel, acc.id)
                if not current:
                    continue
                account_obj = build_platform_account(session, current)

            valid = plugin.check_valid(account_obj)
            with Session(engine) as session:
                model = session.get(AccountModel, acc.id)
                if model:
                    model.updated_at = _utcnow()
                    summary_updates = {"checked_at": _utcnow_iso(), "valid": valid}
                    if hasattr(plugin, "get_last_check_overview"):
                        summary_updates.update(plugin.get_last_check_overview() or {})
                    credential_updates = {}
                    if hasattr(plugin, "get_last_check_credentials"):
                        credential_updates.update(plugin.get_last_check_credentials() or {})
                    patch_account_graph(
                        session, model,
                        summary_updates=summary_updates,
                        credential_updates=credential_updates or None,
                    )
                    session.add(model)
                    session.commit()
            if valid:
                results["valid"] += 1
            else:
                results["invalid"] += 1
                log(f"  {acc.email} ({acc.platform}): 失效")
        except Exception as exc:
            results["error"] += 1
            log(f"  {acc.email} ({acc.platform}): 检测异常 {exc}")

    log(f"检测完成: 有效 {results['valid']}, 失效 {results['invalid']}, "
        f"异常 {results['error']}, 跳过 {results['skipped']}")
    return results


# ---------------------------------------------------------------------------
# Token auto-refresh (ChatGPT-specific for now, extensible)
# ---------------------------------------------------------------------------

def refresh_expiring_tokens(
    *,
    platform: str = "",
    hours_before_expiry: int = 24,
    limit: int = 50,
    log_fn=None,
) -> dict[str, int]:
    """Refresh tokens that are about to expire within `hours_before_expiry` hours."""
    log = log_fn or logger.info
    results = {"refreshed": 0, "failed": 0, "skipped": 0}

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        accounts = session.exec(q.limit(limit)).all()
        graphs = load_account_graphs(session, [int(a.id) for a in accounts if a.id])

    active_statuses = {"registered", "trial", "subscribed"}
    for acc in accounts:
        graph = graphs.get(int(acc.id or 0), {})
        if graph.get("lifecycle_status") not in active_statuses:
            results["skipped"] += 1
            continue

        # Currently only ChatGPT has token refresh support
        if acc.platform != "chatgpt":
            results["skipped"] += 1
            continue

        try:
            from domain.actions import ActionExecutionCommand
            from infrastructure.accounts_repository import AccountsRepository
            from infrastructure.platform_runtime import PlatformRuntime

            result = PlatformRuntime().execute_action(
                ActionExecutionCommand(
                    platform="chatgpt",
                    account_id=int(acc.id or 0),
                    action_id="refresh_token",
                    params={},
                ),
                log_fn=log,
            )

            if result.ok:
                results["refreshed"] += 1
                log(f"  ✓ {acc.email}: session/at 重新登录成功")
            else:
                data = result.data if isinstance(result.data, dict) else {}
                should_delete = (
                    bool(data.get("delete_local_account"))
                    or str(data.get("error_type") or "") == "account_banned"
                )
                if should_delete:
                    deleted = AccountsRepository().delete(int(acc.id or 0))
                    note = "已删除本地账号" if deleted else "本地账号不存在或已删除"
                    log(f"  ✗ {acc.email}: 账号已封禁/注销，{note}: {result.error}")
                else:
                    log(f"  ✗ {acc.email}: {result.error}")
                results["failed"] += 1
        except Exception as exc:
            results["failed"] += 1
            log(f"  ✗ {acc.email}: 刷新异常 {exc}")

    log(f"刷新完成: 成功 {results['refreshed']}, 失败 {results['failed']}, "
        f"跳过 {results['skipped']}")
    return results


# ---------------------------------------------------------------------------
# Trial expiry warning
# ---------------------------------------------------------------------------

def flag_expiring_trials(
    *,
    hours_warning: int = 48,
    log_fn=None,
) -> dict[str, int]:
    """Flag trial accounts that will expire within `hours_warning` hours."""
    log = log_fn or logger.info
    now_ts = _utcnow_ts()
    warning_ts = now_ts + hours_warning * 3600
    results = {"warned": 0, "expired": 0, "skipped": 0}

    with Session(engine) as session:
        overviews = session.exec(
            select(AccountOverviewModel)
            .where(AccountOverviewModel.lifecycle_status == "trial")
        ).all()

    for overview in overviews:
        summary = overview.get_summary()
        trial_end = int(summary.get("trial_end_time") or 0)
        if not trial_end:
            results["skipped"] += 1
            continue

        if trial_end < now_ts:
            # Already expired
            with Session(engine) as session:
                model = session.get(AccountModel, overview.account_id)
                if model:
                    model.updated_at = _utcnow()
                    patch_account_graph(
                        session, model,
                        lifecycle_status=AccountStatus.EXPIRED.value,
                        summary_updates={"expiry_warning": "expired"},
                    )
                    session.add(model)
                    session.commit()
            results["expired"] += 1
        elif trial_end < warning_ts:
            # Expiring soon
            hours_left = max(0, (trial_end - now_ts) // 3600)
            with Session(engine) as session:
                model = session.get(AccountModel, overview.account_id)
                if model:
                    model.updated_at = _utcnow()
                    patch_account_graph(
                        session, model,
                        summary_updates={
                            "expiry_warning": f"expiring_in_{hours_left}h",
                            "expiry_warning_hours": hours_left,
                        },
                    )
                    session.add(model)
                    session.commit()
            results["warned"] += 1
        else:
            results["skipped"] += 1

    log(f"过期预警: 已过期 {results['expired']}, 即将过期 {results['warned']}, "
        f"跳过 {results['skipped']}")
    return results


# ---------------------------------------------------------------------------
# ChatGPT token refresh + CPA sync + liveness check
# ---------------------------------------------------------------------------

def refresh_and_sync_cpa(
    *,
    platform: str = "chatgpt",
    limit: int = 200,
    log_fn=None,
) -> dict[str, int]:
    """
    刷新 ChatGPT 账号 token，检查存活状态，重新上传到 CPA。
    - 用 session_token 刷新 access_token
    - 用 /backend-api/me 检查存活
    - 存活账号重新生成 CPA JSON 并上传
    - 封禁账号标记为 disabled
    """
    log = log_fn or logger.info
    results = {"refreshed": 0, "uploaded": 0, "sub2api_uploaded": 0, "dead": 0, "skipped": 0, "error": 0}

    from curl_cffi import requests as cffi_requests
    import json
    import base64

    def _decode_jwt(token: str) -> dict:
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return {}
            payload = parts[1]
            pad = 4 - len(payload) % 4
            if pad != 4:
                payload += "=" * pad
            return json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            return {}

    # 读取外部上传目标配置：从哪个面板配置取地址，就只上传到哪个面板。
    target_config = _external_upload_targets_config()
    cpa_api_url = str(target_config.get("cpa_api_url") or "")
    cpa_api_key = str(target_config.get("cpa_api_key") or "")
    sub2api_url = str(target_config.get("sub2api_url") or "")
    cpa_enabled = bool(target_config.get("cpa_enabled"))
    sub2api_enabled = bool(target_config.get("sub2api_enabled"))
    if not cpa_enabled and not sub2api_enabled:
        return results

    # 获取所有活跃 chatgpt 账号
    with Session(engine) as session:
        q = select(AccountModel).where(AccountModel.platform == platform)
        q = q.order_by(AccountModel.created_at.desc()).limit(limit)
        accounts = session.exec(q).all()
        graphs = load_account_graphs(session, [int(a.id) for a in accounts if a.id])

    active_statuses = {"registered", "trial", "subscribed"}

    for acc in accounts:
        graph = graphs.get(int(acc.id or 0), {})
        if graph.get("lifecycle_status") not in active_statuses:
            results["skipped"] += 1
            continue

        session_token = credentials.get("session_token", "")
        oauth_refresh_token = credentials.get("refresh_token") or credentials.get("registration_refresh_token", "")
        if not (session_token or oauth_refresh_token):
            results["skipped"] += 1
            continue

        try:
            # WEB 协议优先使用 NextAuth session；ANDROID 协议使用 OAuth RT。
            proxy = credentials.get("proxy", None)
            data = {}
            new_session = session_token
            credential_updates = {}
            if session_token:
                s = cffi_requests.Session(impersonate="chrome120", proxy=proxy)
                s.cookies.set("__Secure-next-auth.session-token", session_token,
                              domain=".chatgpt.com", path="/")
                resp = s.get("https://chatgpt.com/api/auth/session",
                             headers={"accept": "application/json"}, timeout=30)
                if resp.status_code != 200:
                    raise RuntimeError(f"session 刷新失败 HTTP {resp.status_code}")
                data = resp.json()
                access_token = data.get("accessToken", "")
                new_session = s.cookies.get("__Secure-next-auth.session-token") or session_token
                if new_session != session_token:
                    credential_updates["session_token"] = new_session
            else:
                from platforms.chatgpt.token_refresh import TokenRefreshManager
                class _A:
                    pass
                refresh_account = _A()
                refresh_account.email = acc.email
                refresh_account.access_token = credentials.get("access_token", "")
                refresh_account.refresh_token = oauth_refresh_token
                refresh_account.session_token = ""
                refresh_account.cookies = credentials.get("cookies", "")
                refresh_account.client_id = credentials.get("client_id", "")
                refreshed = TokenRefreshManager(proxy_url=proxy).refresh_account(refresh_account)
                if not refreshed.success:
                    raise RuntimeError(refreshed.error_message or "OAuth RT 刷新失败")
                access_token = refreshed.access_token
                if refreshed.refresh_token:
                    credential_updates["refresh_token"] = refreshed.refresh_token

            if not access_token:
                raise RuntimeError("刷新未返回 accessToken")

            results["refreshed"] += 1
            credential_updates["access_token"] = access_token
            if session_token:
                # id_token = access_token (NextAuth 没有独立 id_token)
                credential_updates["id_token"] = access_token

            with Session(engine) as sess:
                model = sess.get(AccountModel, acc.id)
                if model:
                    model.updated_at = _utcnow()
                    patch_account_graph(
                        sess, model,
                        credential_updates=credential_updates,
                        summary_updates={"last_refresh_at": _utcnow_iso(), "refresh_success": True},
                    )
                    sess.add(model)
                    sess.commit()

            # 2. 检查存活
            check_resp = cffi_requests.get(
                "https://chatgpt.com/backend-api/me",
                headers={"authorization": f"Bearer {access_token}", "accept": "application/json"},
                proxy=proxy, timeout=15, impersonate="chrome120",
            )

            if check_resp.status_code != 200:
                err_detail = ""
                try:
                    err_detail = str(check_resp.json().get("detail", ""))[:80]
                except Exception:
                    err_detail = check_resp.text[:80]
                if _is_k12_account_graph(graph):
                    message = f"K12 /backend-api/me HTTP {check_resp.status_code}: {err_detail}"
                    log(f"  ↷ {acc.email}: {message}，保留当前状态")
                    results["error"] += 1
                    with Session(engine) as sess:
                        model = sess.get(AccountModel, acc.id)
                        if model:
                            patch_account_graph(
                                sess,
                                model,
                                summary_updates={
                                    "checked_at": _utcnow_iso(),
                                    "check_error": message,
                                    "k12_liveness_check_error": message,
                                    "k12_liveness_checked_at": _utcnow_iso(),
                                },
                            )
                            sess.add(model)
                            sess.commit()
                    continue
                hard_dead_markers = (
                    "account_deactivated", "deleted", "disabled", "suspended", "banned",
                )
                hard_dead = any(marker in err_detail.lower() for marker in hard_dead_markers)
                if hard_dead:
                    log(f"  ✗ {acc.email}: 账号已注销/封禁 ({check_resp.status_code}: {err_detail})")
                    results["dead"] += 1
                    with Session(engine) as sess:
                        model = sess.get(AccountModel, acc.id)
                        if model:
                            patch_account_graph(
                                sess, model,
                                lifecycle_status=AccountStatus.INVALID.value,
                                summary_updates={"deactivated_at": _utcnow_iso(), "deactivated_reason": err_detail},
                            )
                            sess.add(model)
                            sess.commit()
                else:
                    log(f"  ↷ {acc.email}: 测活暂时失败，保留当前状态 ({check_resp.status_code}: {err_detail})")
                    results["error"] += 1
                    with Session(engine) as sess:
                        model = sess.get(AccountModel, acc.id)
                        if model:
                            patch_account_graph(
                                sess,
                                model,
                                summary_updates={
                                    "checked_at": _utcnow_iso(),
                                    "check_error": f"/backend-api/me HTTP {check_resp.status_code}: {err_detail}",
                                },
                            )
                            sess.add(model)
                            sess.commit()
                continue

            # 3. 上传到 CPA
            if cpa_enabled:
                from datetime import timedelta
                tz8 = timezone(timedelta(hours=8))
                jwt_payload = _decode_jwt(access_token)
                auth_info = jwt_payload.get("https://api.openai.com/auth", {})
                account_id = auth_info.get("chatgpt_account_id", "")
                exp = jwt_payload.get("exp", 0)
                iat = jwt_payload.get("iat", 0)
                expired_str = datetime.fromtimestamp(exp, tz=tz8).strftime("%Y-%m-%dT%H:%M:%S+08:00") if exp else ""
                last_refresh = datetime.fromtimestamp(iat, tz=tz8).strftime("%Y-%m-%dT%H:%M:%S+08:00") if iat else _utcnow_iso()

                token_data = {
                    "access_token": access_token,
                    "account_id": account_id,
                    "disabled": False,
                    "email": acc.email,
                    "expired": expired_str,
                    "id_token": access_token,
                    "last_refresh": last_refresh,
                    "refresh_token": credentials.get("refresh_token", ""),
                    "type": "codex",
                }

                from urllib.parse import quote
                upload_url = f"{cpa_api_url.rstrip('/')}/v0/management/auth-files?name={quote(acc.email + '.json')}"
                upload_resp = cffi_requests.post(
                    upload_url,
                    headers={"Authorization": f"Bearer {cpa_api_key}", "Content-Type": "application/json"},
                    data=json.dumps(token_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    verify=False, timeout=30, impersonate="chrome110",
                )
                if upload_resp.status_code in (200, 201, 207):
                    results["uploaded"] += 1
                    log(f"  ✓ {acc.email}: 刷新+上传成功")
                else:
                    log(f"  ✗ {acc.email}: 上传失败 HTTP {upload_resp.status_code}")
            elif not sub2api_enabled:
                log(f"  ✓ {acc.email}: 刷新成功 (CPA 未配置)")

            # 中文说明：刷新拿到新 access_token 后，同步更新 SUB2API，避免远端仍用旧 token。
            if sub2api_enabled and sub2api_url:
                if _is_k12_account_graph(graph):
                    results["skipped"] += 1
                    log(f"  ↷ {acc.email}: K12 账号跳过后台 SUB2API 同步")
                    time.sleep(0.5)
                    continue
                try:
                    from platforms.chatgpt.sub2api_upload import upload_to_sub2api

                    jwt_payload = _decode_jwt(access_token)
                    auth_info = jwt_payload.get("https://api.openai.com/auth", {})
                    target = SimpleNamespace(
                        email=acc.email,
                        access_token=access_token,
                        refresh_token=credentials.get("refresh_token", ""),
                        id_token=access_token,
                        session_token=new_session,
                        account_id=auth_info.get("chatgpt_account_id", ""),
                        user_id=auth_info.get("chatgpt_account_id", ""),
                        workspace_id=auth_info.get("organization_id", ""),
                        expires_at=data.get("expires", ""),
                        session=data,
                        extra={"session": data, "expires_at": data.get("expires", "")},
                    )
                    ok, msg = upload_to_sub2api(target)
                    if ok:
                        results["sub2api_uploaded"] += 1
                        log(f"  ✓ {acc.email}: SUB2API 同步成功 - {msg}")
                    else:
                        log(f"  ✗ {acc.email}: SUB2API 同步失败 - {msg}")
                except Exception as exc:
                    log(f"  ✗ {acc.email}: SUB2API 同步异常 {exc}")

            time.sleep(0.5)

        except Exception as exc:
            results["error"] += 1
            log(f"  ✗ {acc.email}: 异常 {exc}")

    if cpa_enabled and sub2api_enabled:
        log(f"[CPA+SUB2API Sync] 刷新 {results['refreshed']}, CPA {results['uploaded']}, "
            f"SUB2API {results['sub2api_uploaded']}, "
            f"封禁 {results['dead']}, 跳过 {results['skipped']}, 错误 {results['error']}")
    elif cpa_enabled:
        log(f"[CPA Sync] 刷新 {results['refreshed']}, 上传 {results['uploaded']}, "
            f"封禁 {results['dead']}, 跳过 {results['skipped']}, 错误 {results['error']}")
    elif sub2api_enabled:
        log(f"[SUB2API Sync] 刷新 {results['refreshed']}, 上传 {results['sub2api_uploaded']}, "
            f"封禁 {results['dead']}, 跳过 {results['skipped']}, 错误 {results['error']}")
    return results


# ---------------------------------------------------------------------------
# Lifecycle manager (combines all periodic tasks)
# ---------------------------------------------------------------------------

class LifecycleManager:
    """Runs periodic lifecycle tasks in a background thread."""

    def __init__(
        self,
        *,
        check_interval_hours: float = 6,
        refresh_interval_hours: float = 12,
        cpa_sync_interval_hours: float = 6,
        warning_hours: int = 48,
    ):
        self.check_interval = check_interval_hours * 3600
        self.refresh_interval = refresh_interval_hours * 3600
        self.cpa_sync_interval = cpa_sync_interval_hours * 3600
        self.warning_hours = warning_hours
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_check = 0.0
        self._last_refresh = 0.0
        self._last_cpa_sync = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lifecycle-manager")
        self._thread.start()
        print("[LifecycleManager] 已启动")

    def stop(self):
        self._running = False

    def _loop(self):
        # Wait a bit before first run to let the app fully initialize
        time.sleep(30)
        while self._running:
            now = time.time()
            try:
                service_flags = get_lifecycle_service_flags()
                # Trial expiry warnings — run every cycle
                if service_flags.get(LIFECYCLE_TRIAL_WARNING_ENABLED_KEY, True):
                    flag_expiring_trials(hours_warning=self.warning_hours)

                # Validity check
                if (
                    service_flags.get(LIFECYCLE_ACCOUNT_CHECK_ENABLED_KEY, True)
                    and now - self._last_check >= self.check_interval
                ):
                    print("[LifecycleManager] 开始账号有效性检测...")
                    check_accounts_validity()
                    self._last_check = now

                # Token refresh
                if (
                    service_flags.get(LIFECYCLE_TOKEN_REFRESH_ENABLED_KEY, True)
                    and now - self._last_refresh >= self.refresh_interval
                ):
                    print("[LifecycleManager] 开始 token 自动续期...")
                    refresh_expiring_tokens()
                    self._last_refresh = now

                # CPA sync (刷新 token + 存活检查 + 上传)
                if (
                    service_flags.get(LIFECYCLE_EXTERNAL_SYNC_ENABLED_KEY, False)
                    and now - self._last_cpa_sync >= self.cpa_sync_interval
                ):
                    target_label = _external_upload_target_label()
                    if target_label:
                        print(f"[LifecycleManager] 开始 {target_label} 同步 (刷新+检查+上传)...")
                        refresh_and_sync_cpa()
                    self._last_cpa_sync = now

            except Exception as exc:
                print(f"[LifecycleManager] 错误: {exc}")

            # Sleep in small increments so stop() is responsive
            for _ in range(60):
                if not self._running:
                    break
                time.sleep(1)


lifecycle_manager = LifecycleManager()
