from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.provider_settings import ProviderSettingsService

router = APIRouter(prefix="/provider-settings", tags=["provider-settings"])
service = ProviderSettingsService()


class ProviderSettingUpsertRequest(BaseModel):
    id: int | None = None
    provider_type: str
    provider_key: str
    display_name: str = ""
    auth_mode: str = ""
    enabled: bool = True
    is_default: bool = False
    config: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, str] = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


@router.get("")
def list_provider_settings(provider_type: str):
    return service.list_settings(provider_type)


@router.put("")
def save_provider_setting(body: ProviderSettingUpsertRequest):
    try:
        return service.save_setting(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("")
def create_provider_setting(body: ProviderSettingUpsertRequest):
    try:
        return service.save_setting(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/{setting_id}")
def delete_provider_setting(setting_id: int):
    result = service.delete_setting(setting_id)
    if not result["ok"]:
        raise HTTPException(404, "provider setting 不存在")
    return result


class ProviderTestRequest(BaseModel):
    provider_type: str
    provider_key: str
    config: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, str] = Field(default_factory=dict)


class GmailOAuthAuthUrlRequest(BaseModel):
    credentials_json: str = ""
    auto_callback: bool = False


class GmailOAuthExchangeRequest(BaseModel):
    credentials_json: str = ""
    code: str = ""
    code_verifier: str = ""


class ICloudHMEAccountRequest(BaseModel):
    id: str = ""
    name: str = ""
    real_email: str = ""
    icloud_email: str = ""
    cookies: str = ""
    cookie_header: str = ""
    host: str = "icloud.com"
    proxy: str = ""
    app_password: str = ""
    validate_account: bool = Field(default=True, alias="validate")


class ICloudHMEAliasCreateRequest(BaseModel):
    account_id: str
    label: str = ""


class ICloudHMEAliasActionRequest(BaseModel):
    account_id: str


_GMAIL_OAUTH_SESSIONS: dict[str, dict] = {}
_GMAIL_OAUTH_LOCK = threading.Lock()
_GMAIL_OAUTH_CALLBACK_HOST = "127.0.0.1"
_GMAIL_OAUTH_CALLBACK_PORT = 53682


def _gmail_oauth_callback_html(message: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Gmail OAuth</title></head>"
        "<body style='font-family:system-ui;margin:48px;line-height:1.6'>"
        f"<h2>{message}</h2><p>可以关闭这个页面，回到项目配置弹窗继续。</p>"
        "</body></html>"
    ).encode("utf-8")


def _start_gmail_oauth_callback_listener(session_id: str) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            state = (query.get("state") or [""])[0]
            code = (query.get("code") or [""])[0]
            error = (query.get("error") or [""])[0]
            status = "error"
            message = error or "授权回调缺少 code"
            token_json = ""
            with _GMAIL_OAUTH_LOCK:
                session = _GMAIL_OAUTH_SESSIONS.get(state)
            if session and code and not error:
                try:
                    from core.gmail_oauth_mailbox import gmail_oauth_exchange_code

                    token_json = gmail_oauth_exchange_code(
                        session.get("credentials_json", ""),
                        code,
                        session.get("code_verifier", ""),
                    )
                    status = "success"
                    message = "Gmail 授权成功，Token 已生成"
                except Exception as exc:
                    message = str(exc)
            with _GMAIL_OAUTH_LOCK:
                if session:
                    session.update({
                        "status": status,
                        "message": message,
                        "token_json": token_json,
                        "updated_at": time.time(),
                    })
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_gmail_oauth_callback_html(message))

    def run_server():
        try:
            server = HTTPServer((_GMAIL_OAUTH_CALLBACK_HOST, _GMAIL_OAUTH_CALLBACK_PORT), Handler)
        except Exception as exc:
            with _GMAIL_OAUTH_LOCK:
                session = _GMAIL_OAUTH_SESSIONS.get(session_id)
                if session:
                    session.update({"status": "error", "message": f"无法监听 http://{_GMAIL_OAUTH_CALLBACK_HOST}:{_GMAIL_OAUTH_CALLBACK_PORT}: {exc}", "updated_at": time.time()})
            return
        with _GMAIL_OAUTH_LOCK:
            session = _GMAIL_OAUTH_SESSIONS.get(session_id)
            if session:
                session["listener_ready"] = True
        server.timeout = 300
        server.handle_request()
        server.server_close()

    threading.Thread(target=run_server, name=f"gmail-oauth-callback-{session_id}", daemon=True).start()


@router.post("/gmail-oauth/auth-url")
def create_gmail_oauth_auth_url(body: GmailOAuthAuthUrlRequest):
    try:
        from core.gmail_oauth_mailbox import gmail_oauth_authorization_url

        url, verifier = gmail_oauth_authorization_url(body.credentials_json)
        if not body.auto_callback:
            return {"ok": True, "url": url, "code_verifier": verifier}

        session_id = secrets.token_urlsafe(16)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        query["state"] = [session_id]
        url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))
        with _GMAIL_OAUTH_LOCK:
            _GMAIL_OAUTH_SESSIONS[session_id] = {
                "status": "pending",
                "message": "",
                "credentials_json": body.credentials_json,
                "code_verifier": verifier,
                "token_json": "",
                "created_at": time.time(),
                "updated_at": time.time(),
                "listener_ready": False,
            }
        _start_gmail_oauth_callback_listener(session_id)
        return {"ok": True, "url": url, "code_verifier": verifier, "session_id": session_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/gmail-oauth/callback-status/{session_id}")
def get_gmail_oauth_callback_status(session_id: str):
    with _GMAIL_OAUTH_LOCK:
        session = dict(_GMAIL_OAUTH_SESSIONS.get(session_id) or {})
    if not session:
        return {"ok": False, "error": "授权会话不存在或已过期"}
    return {
        "ok": True,
        "status": session.get("status", "pending"),
        "message": session.get("message", ""),
        "token_json": session.get("token_json", ""),
        "listener_ready": bool(session.get("listener_ready")),
    }


@router.post("/gmail-oauth/exchange-code")
def exchange_gmail_oauth_code(body: GmailOAuthExchangeRequest):
    try:
        from core.gmail_oauth_mailbox import gmail_oauth_exchange_code

        token_json = gmail_oauth_exchange_code(
            body.credentials_json,
            body.code,
            body.code_verifier,
        )
        return {"ok": True, "token_json": token_json}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/icloud-hme/accounts")
def list_icloud_hme_accounts():
    from application.icloud_hme import ICloudHMEService

    return ICloudHMEService().list_accounts()


@router.post("/icloud-hme/accounts")
def upsert_icloud_hme_account(body: ICloudHMEAccountRequest):
    from application.icloud_hme import ICloudHMEService

    try:
        payload = body.model_dump()
        payload["validate"] = body.validate_account
        return ICloudHMEService().upsert_account(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/icloud-hme/accounts/{account_id}")
def delete_icloud_hme_account(account_id: str):
    from application.icloud_hme import ICloudHMEService

    result = ICloudHMEService().delete_account(account_id)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error") or "iCloud 账号不存在")
    return result


@router.post("/icloud-hme/accounts/{account_id}/validate")
def validate_icloud_hme_account(account_id: str, body: ICloudHMEAccountRequest | None = None):
    from application.icloud_hme import ICloudHMEService

    try:
        payload = body.model_dump() if body else None
        return ICloudHMEService().validate_account(account_id, payload)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.get("/icloud-hme/aliases")
def list_icloud_hme_aliases(account_id: str):
    from application.icloud_hme import ICloudHMEService

    try:
        return ICloudHMEService().list_aliases(account_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/icloud-hme/aliases")
def create_icloud_hme_alias(body: ICloudHMEAliasCreateRequest):
    from application.icloud_hme import ICloudHMEService

    try:
        return ICloudHMEService().create_alias(body.account_id, body.label)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/icloud-hme/aliases/{anonymous_id}/deactivate")
def deactivate_icloud_hme_alias(anonymous_id: str, body: ICloudHMEAliasActionRequest):
    from application.icloud_hme import ICloudHMEService

    try:
        return ICloudHMEService().alias_action(body.account_id, anonymous_id, "deactivate")
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/icloud-hme/aliases/{anonymous_id}/reactivate")
def reactivate_icloud_hme_alias(anonymous_id: str, body: ICloudHMEAliasActionRequest):
    from application.icloud_hme import ICloudHMEService

    try:
        return ICloudHMEService().alias_action(body.account_id, anonymous_id, "reactivate")
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.delete("/icloud-hme/aliases/{anonymous_id}")
def delete_icloud_hme_alias(anonymous_id: str, body: ICloudHMEAliasActionRequest):
    from application.icloud_hme import ICloudHMEService

    try:
        return ICloudHMEService().alias_action(body.account_id, anonymous_id, "delete")
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/outlook-email/groups")
def list_outlook_email_groups(body: ProviderTestRequest):
    if body.provider_type != "mailbox" or body.provider_key not in {"outlook_email_api", "outlook_email"}:
        return {"ok": False, "error": f"不支持的 outlookEmail provider: {body.provider_key}"}
    extra = {**body.config, **body.auth}
    try:
        from core.outlook_email_mailbox import list_outlook_email_group_options

        options = list_outlook_email_group_options(
            api_url=extra.get("outlook_email_api_url", ""),
            api_key=extra.get("outlook_email_api_key", ""),
            admin_password=extra.get("outlook_email_admin_password", ""),
        )
        return {"ok": True, "options": options}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "options": []}


@router.post("/test")
def test_provider(body: ProviderTestRequest):
    """测试 provider 配置是否正确 — 尝试创建/获取一个邮箱地址。"""
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    definitions = ProviderDefinitionsRepository()
    definition = definitions.get_by_key(body.provider_type, body.provider_key)
    if not definition:
        return {"ok": False, "error": f"未找到 provider 定义: {body.provider_key}"}

    # Merge config + auth into a flat dict (same as runtime)
    extra = {**body.config, **body.auth}

    if body.provider_type == "mailbox":
        return _test_mailbox(definition.driver_type or body.provider_key, extra, definition)
    elif body.provider_type == "captcha":
        return {"ok": True, "message": "验证码服务暂不支持在线测试，请在注册任务中验证"}
    elif body.provider_type == "sms":
        if (definition.driver_type or body.provider_key) == "codex_sms_pool":
            return _test_codex_sms_pool(extra)
        return {"ok": True, "message": "接码服务暂不支持在线测试，请在注册任务中验证"}
    elif body.provider_type == "proxy":
        return _test_proxy(definition.driver_type or body.provider_key, extra)
    else:
        return {"ok": False, "error": f"不支持测试的 provider 类型: {body.provider_type}"}


def _test_proxy(driver_type: str, extra: dict) -> dict:
    import traceback

    try:
        from core.proxy_providers import create_proxy_provider

        provider = create_proxy_provider(driver_type, extra)
        if hasattr(provider, "test_connection"):
            result = provider.test_connection(check_exit=True)
            message = (
                f"测试成功！策略组 {result.get('selector')} 已切换到 {result.get('selected_node')}，"
                f"可用节点 {result.get('node_count')} 个"
            )
            if result.get("exit_check"):
                message += "，出口检测已通过"
            return {"ok": True, "message": message, **result}

        proxy = provider.get_proxy()
        if not proxy:
            return {"ok": False, "error": "未获取到可用代理"}
        return {"ok": True, "message": f"测试成功！获取代理: {proxy}", "proxy": proxy}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"测试失败: {str(exc)}",
            "detail": traceback.format_exc()[-500:],
        }


def _test_codex_sms_pool(extra: dict) -> dict:
    """校验 Codex 本地接码池文本，不触发真实取码。"""
    from core.base_sms import parse_codex_sms_pool_entries

    pool_text = str(
        extra.get("codex_sms_pool_text")
        or extra.get("codex_sms_pool")
        or extra.get("chatGptApiSmsPoolText")
        or ""
    )
    entries = parse_codex_sms_pool_entries(pool_text)
    if not entries:
        return {
            "ok": False,
            "error": "未识别到有效号码，格式：+手机号|取码链接，一行一个",
        }
    first = entries[0]
    return {
        "ok": True,
        "message": f"测试成功！已识别 {len(entries)} 个号码，首个号码: {first.phone_e164}",
        "count": len(entries),
        "first_phone": first.phone_e164,
    }


def _test_mailbox(driver_type: str, extra: dict, definition) -> dict:
    """尝试用给定配置创建一个邮箱，验证配置是否正确。"""
    import traceback
    from core.base_mailbox import MAILBOX_FACTORY_REGISTRY

    factory = MAILBOX_FACTORY_REGISTRY.get(driver_type)
    if not factory:
        return {"ok": False, "error": f"未找到邮箱驱动: {driver_type}"}

    try:
        if driver_type in ("generic_http_mailbox", "generic_http"):
            pipeline_config = definition.get_metadata() if definition else {}
            mailbox = factory(extra, None, pipeline_config=pipeline_config)
        else:
            mailbox = factory(extra, None)

        if hasattr(mailbox, "peek_email"):
            email = mailbox.peek_email()
            return {
                "ok": True,
                "message": f"测试成功！可用邮箱: {email}",
                "email": email,
            }

        account = mailbox.get_email()
        return {
            "ok": True,
            "message": f"测试成功！生成邮箱: {account.email}",
            "email": account.email,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"测试失败: {str(exc)}",
            "detail": traceback.format_exc()[-500:],
        }
