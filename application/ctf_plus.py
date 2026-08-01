from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from domain.accounts import AccountUpdateCommand
from infrastructure.accounts_repository import AccountsRepository
from platforms.chatgpt.constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE, OAUTH_TOKEN_URL
from platforms.chatgpt.oauth import generate_oauth_url, submit_callback_url


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_ctf_exported(overview: dict[str, Any] | None) -> bool:
    data = dict(overview or {}).get("ctf_gpt_plus")
    return isinstance(data, dict) and data.get("exported") is True


def _parse_callback_token_payload(callback_url: str) -> dict[str, str]:
    parsed = urlparse(str(callback_url or "").strip())
    query = parse_qs(parsed.query, keep_blank_values=True)
    fragment = parse_qs(parsed.fragment, keep_blank_values=True)
    values = {**query, **fragment}
    payload: dict[str, str] = {}
    for source, target in (
        ("access_token", "access_token"),
        ("accessToken", "access_token"),
        ("refresh_token", "refresh_token"),
        ("refreshToken", "refresh_token"),
        ("id_token", "id_token"),
        ("idToken", "id_token"),
        ("account_id", "account_id"),
        ("accountId", "account_id"),
        ("chatgpt_account_id", "account_id"),
        ("email", "email"),
        ("expired", "expired"),
        ("expires_at", "expired"),
        ("last_refresh", "last_refresh"),
    ):
        raw = values.get(source, [""])
        value = str(raw[0] if raw else "").strip()
        if value:
            payload[target] = value
    return payload


def _account_totp_secret(account) -> str:
    overview = account.overview if isinstance(getattr(account, "overview", None), dict) else {}
    legacy_extra = overview.get("legacy_extra") if isinstance(overview.get("legacy_extra"), dict) else {}
    for item in list(getattr(account, "credentials", None) or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("key") or "").strip() == "totp_secret":
            value = str(item.get("value") or "").strip()
            if value:
                return value
    return str(overview.get("totp_secret") or legacy_extra.get("totp_secret") or "").strip()


@dataclass(slots=True)
class OAuthSession:
    account_id: int
    state: str
    code_verifier: str
    created_at: float


class CtfPlusAccountsService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()
        self._oauth_sessions: dict[int, OAuthSession] = {}

    def mark_exported(self, *, ids: list[int], exported: bool = True) -> dict[str, Any]:
        updated: list[int] = []
        for account_id in [int(item) for item in ids or [] if int(item or 0) > 0]:
            account = self.repository.get(account_id)
            if not account:
                continue
            self.repository.update(
                account_id,
                AccountUpdateCommand(
                    overview={
                        "ctf_gpt_plus": {
                            "exported": bool(exported),
                            "exported_at": _utcnow_iso() if exported else "",
                        }
                    }
                ),
            )
            updated.append(account_id)
        return {"ok": True, "updated_ids": updated, "exported": bool(exported)}

    def start_codex_oauth(self, *, account_id: int) -> dict[str, Any]:
        account = self.repository.get(int(account_id))
        if not account:
            raise ValueError("account not found")
        oauth = generate_oauth_url(
            redirect_uri=CODEX_REDIRECT_URI,
            scope=CODEX_SCOPE,
            client_id=CODEX_CLIENT_ID,
        )
        self._oauth_sessions[int(account_id)] = OAuthSession(
            account_id=int(account_id),
            state=oauth.state,
            code_verifier=oauth.code_verifier,
            created_at=time.time(),
        )
        return {
            "account_id": int(account_id),
            "email": account.email,
            "auth_url": oauth.auth_url,
            "state": oauth.state,
        }

    def complete_codex_oauth(self, *, account_id: int, callback_url: str) -> dict[str, Any]:
        account = self.repository.get(int(account_id))
        if not account:
            raise ValueError("account not found")
        payload = _parse_callback_token_payload(callback_url)
        if not payload.get("access_token"):
            session = self._oauth_sessions.get(int(account_id))
            if not session:
                raise ValueError("oauth session not found, start OAuth first")
            payload = json.loads(
                submit_callback_url(
                    callback_url=callback_url,
                    expected_state=session.state,
                    code_verifier=session.code_verifier,
                    redirect_uri=CODEX_REDIRECT_URI,
                    client_id=CODEX_CLIENT_ID,
                    token_url=OAUTH_TOKEN_URL,
                )
            )
        self._persist_codex_tokens(account_id=int(account_id), payload=payload)
        self._oauth_sessions.pop(int(account_id), None)
        return {"ok": True, "account_id": int(account_id), "email": account.email}

    def run_codex_oauth_browser(
        self,
        *,
        account_id: int,
        browser_mode: str = "camoufox_headed",
        bit_profile_id: str = "",
        log_fn: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        account = self.repository.get(int(account_id))
        if not account:
            raise ValueError("account not found")
        if not account.password:
            raise ValueError("account password is empty")
        log = log_fn or (lambda _message: None)
        acquired_profile_id = ""
        from application.bitbrowser_profiles import acquire_profile_for_browser_mode, release_acquired_profile
        from platforms._browser_backend import parse_checkout_mode
        from platforms.chatgpt.browser_register import ChatGPTBrowserRegister

        try:
            if str(browser_mode or "").startswith("bitbrowser_"):
                bit_profile_id, acquired_profile_id = acquire_profile_for_browser_mode(
                    browser_mode,
                    fallback=bit_profile_id,
                    log_fn=log,
                )
            backend_config = parse_checkout_mode(browser_mode, bit_profile_id=bit_profile_id)
            log(f"准备为 {account.email} 执行 Codex OAuth，浏览器模式 {browser_mode}")
            otp_callback = None
            totp_secret = _account_totp_secret(account)
            prefer_password_totp_login = bool(str(account.password or "").strip() and totp_secret)
            try:
                from core.base_platform import Account as PlatformAccount
                from core.base_platform import RegisterConfig
                from platforms.chatgpt.plugin import ChatGPTPlatform

                if prefer_password_totp_login:
                    log("Codex OAuth 检测到账号已保存密码和 2FA，本次使用密码 + TOTP，不初始化邮箱 OTP 服务")
                else:
                    # Codex OAuth 可能触发邮箱 OTP；复用账号注册时绑定的邮箱资源读取验证码。
                    platform_account = PlatformAccount(
                        platform=account.platform,
                        email=account.email,
                        password=account.password,
                        user_id=account.user_id,
                        token=account.primary_token,
                        trial_end_time=account.trial_end_time,
                        extra={
                            "account_overview": dict(account.overview or {}),
                            "provider_accounts": list(account.provider_accounts or []),
                            "provider_resources": list(account.provider_resources or []),
                        },
                    )
                    otp_callback, otp_error = ChatGPTPlatform(
                        RegisterConfig(proxy=None)
                    )._build_get_rt_mailbox_otp_callback(platform_account, log, None)
                    if otp_callback:
                        log("Codex OAuth 已接入账号邮箱 OTP callback")
                    else:
                        log(f"Codex OAuth 邮箱 OTP callback 不可用: {otp_error}")
            except Exception as exc:
                log(f"Codex OAuth 邮箱 OTP callback 初始化失败: {exc}")
            worker = ChatGPTBrowserRegister(
                headless=backend_config.is_headless,
                log_fn=log,
                backend_config=backend_config,
                otp_callback=otp_callback,
                totp_secret=totp_secret,
            )
            result = worker._retry_oauth_fresh_browser(account.email, account.password)
            if not isinstance(result, dict) or not result.get("access_token"):
                raise ValueError("Codex OAuth did not return usable tokens")
            self._persist_codex_tokens(account_id=int(account_id), payload=result)
            log(f"Codex OAuth 成功，已保存 token: {account.email}")
            return {"ok": True, "account_id": int(account_id), "email": account.email}
        finally:
            if acquired_profile_id:
                release_acquired_profile(acquired_profile_id, log_fn=log)

    def _persist_codex_tokens(self, *, account_id: int, payload: dict[str, Any]) -> None:
        account = self.repository.get(int(account_id))
        if not account:
            raise ValueError("account not found")
        credential_updates = {
            key: value
            for key, value in {
                "access_token": payload.get("access_token") or payload.get("accessToken") or "",
                "refresh_token": payload.get("refresh_token") or payload.get("refreshToken") or "",
                "id_token": payload.get("id_token") or payload.get("idToken") or "",
                "account_id": payload.get("account_id")
                or payload.get("accountId")
                or payload.get("chatgpt_account_id")
                or "",
            }.items()
            if value
        }
        if not credential_updates:
            raise ValueError("callback url did not contain usable tokens")
        self.repository.update(
            int(account_id),
            AccountUpdateCommand(
                user_id=str(credential_updates.get("account_id") or "") or None,
                credentials=credential_updates,
                primary_token=credential_updates.get("access_token"),
                overview={
                    "codex_oauth": {
                        "refreshed_at": _utcnow_iso(),
                        "email": str(payload.get("email") or account.email),
                        "expired": str(payload.get("expired") or payload.get("expires_at") or ""),
                    }
                },
            ),
        )
