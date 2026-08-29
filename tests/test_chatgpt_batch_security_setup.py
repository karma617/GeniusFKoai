import json
from types import SimpleNamespace

from application.tasks import _set_chatgpt_account_password_protocol
from platforms.chatgpt.mfa import _cookie_header as _mfa_cookie_header
from platforms.chatgpt.register import RegistrationEngine, SentinelPayload, _iter_cookie_records


class _DummyEmailService:
    pass


class _Response:
    def __init__(self, status_code, *, url="", payload=None, headers=None, text=""):
        self.status_code = status_code
        self.url = url
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class _Session:
    def __init__(self, cookies=None):
        self.cookies = dict(cookies or {})
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kwargs):
        raise AssertionError(f"unexpected GET {url}")


def test_protocol_device_identity_adopts_server_cookie():
    engine = RegistrationEngine(_DummyEmailService(), callback_logger=lambda _message: None)
    assert engine._init_latest_chatgpt_session() is True
    original_device_id = engine.protocol_fingerprint.device_id
    engine.session.cookies.set("oai-did", "stale-auth-device-id", domain="auth.openai.com", path="/")
    engine.session.cookies.set("oai-did", "server-device-id", domain="chatgpt.com", path="/")

    device_id = engine._ensure_protocol_device_identity("test_server_cookie")

    assert original_device_id != "server-device-id"
    assert device_id == "server-device-id"
    assert engine._device_id == "server-device-id"
    assert engine.protocol_fingerprint.device_id == "server-device-id"
    assert {
        str(cookie.get("value") or "")
        for cookie in (_iter_cookie_records(engine.session.cookies) or [])
        if str(cookie.get("name") or "") == "oai-did"
    } == {"server-device-id"}


def test_protocol_extended_warmups_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OPENAI_PROTOCOL_EXTENDED_WARMUP", raising=False)
    engine = RegistrationEngine(_DummyEmailService(), callback_logger=lambda _message: None)
    engine.session = _Session()
    engine._device_id = "device-id"

    engine._latest_chatgpt_warmup_chatgpt_anon_session("device-id")

    assert engine._latest_chatgpt_warmup_authenticated_session("access-token") is True
    assert engine.session.posts == []


def test_batch_security_setup_relogs_before_password_add(monkeypatch):
    stages = []

    class _FreshSession(_Session):
        def get(self, url, **kwargs):
            assert url == "https://chatgpt.com/api/auth/session"
            stages.append("session_refresh")
            self.cookies["__Secure-next-auth.session-token"] = "after-password-session"
            return _Response(200, url=url, payload={"accessToken": "after-password-at"})

    class _FakeRegistrationEngine:
        def __init__(self, **kwargs):
            self.session = _FreshSession()
            self.email = ""
            self.email_info = {}
            self.password = ""
            self.set_password_after_register = True
            self.prefer_password_totp_login = True
            self._device_id = "fresh-device"
            self._chatgpt_oai_session_id = "fresh-oai-session"
            self._post_register_password_error = ""
            self._log = kwargs["callback_logger"]

        def run_chatgpt_refresh_session_latest(self, *, session_source):
            assert session_source == "batch_security_setup_relogin"
            assert self.password == ""
            assert self.set_password_after_register is False
            assert self.prefer_password_totp_login is False
            stages.append("relogin")
            self.session.cookies.update(
                {
                    "__Secure-next-auth.session-token": "fresh-session",
                    "oai-did": "fresh-device",
                }
            )
            return SimpleNamespace(
                success=True,
                error_message="",
                access_token="fresh-at",
                session_token="fresh-session",
                metadata={
                    "chatgpt_user_agent": "fresh-ua",
                    "chatgpt_accept_language": "ja-JP,ja;q=0.5",
                    "chatgpt_oai_client_version": "fresh-client",
                    "chatgpt_oai_client_build_number": "123",
                    "chatgpt_oai_device_id": "fresh-device",
                    "chatgpt_oai_session_id": "fresh-oai-session",
                },
            )

        def _clear_completed_auth_step_cookies(self):
            stages.append("clear_transient_auth")
            self.session.cookies.pop("login_session", None)
            return 1

        def _seed_oai_did_cookie(self, device_id):
            assert device_id == "fresh-device"
            stages.append("seed_device")
            self.session.cookies["oai-did"] = device_id
            return device_id

        def _latest_chatgpt_add_password_after_register(self, access_token):
            assert access_token == "fresh-at"
            assert self.password == "Password123!"
            assert self.set_password_after_register is True
            stages.append("add_password")
            return True

        def _latest_chatgpt_browser_headers(self, **kwargs):
            return {"accept": kwargs["accept"]}

        @staticmethod
        def _response_json_dict(response):
            return response.json()

        @staticmethod
        def _diag_shape(value):
            return "yes" if value else "no"

        def _diag_cookie_names_text(self):
            return ",".join(sorted(self.session.cookies))

        def _latest_chatgpt_warmup_authenticated_session(self, access_token):
            assert access_token == "after-password-at"
            stages.append("warmup")
            return True

        @staticmethod
        def _latest_chatgpt_user_agent():
            return "fresh-ua"

        @staticmethod
        def _latest_chatgpt_accept_language():
            return "ja-JP,ja;q=0.5"

        @staticmethod
        def _latest_chatgpt_client_version():
            return "fresh-client"

        @staticmethod
        def _latest_chatgpt_client_build_number():
            return "123"

    class _FakePlatform:
        def __init__(self, config):
            self.config = config

        @staticmethod
        def _build_refresh_session_mailbox_email_service(account, log_fn, proxy):
            return _DummyEmailService(), ""

    import platforms.chatgpt.plugin as plugin_module
    import platforms.chatgpt.register as register_module

    monkeypatch.setattr(plugin_module, "ChatGPTPlatform", _FakePlatform)
    monkeypatch.setattr(register_module, "RegistrationEngine", _FakeRegistrationEngine)

    account = SimpleNamespace(
        email="user@example.com",
        extra={
            "cookie_header": "oai-client-auth-session=stale-auth-session; cf_clearance=stale-clearance",
            "session_token": "stale-session",
            "chatgpt_oai_device_id": "stale-device",
        },
    )
    logs = []
    logger = SimpleNamespace(log=lambda message, *args, **kwargs: logs.append(message))

    ok, updates, error = _set_chatgpt_account_password_protocol(
        account,
        password="Password123!",
        proxy=None,
        logger=logger,
    )

    assert ok is True
    assert error == ""
    assert stages == [
        "relogin",
        "clear_transient_auth",
        "seed_device",
        "add_password",
        "session_refresh",
        "warmup",
    ]
    assert updates["access_token"] == "after-password-at"
    assert updates["session_token"] == "after-password-session"
    assert updates["chatgpt_oai_device_id"] == "fresh-device"
    assert "stale-auth-session" not in updates["cookie_header"]
    assert "stale-clearance" not in updates["cookie_header"]
    assert any("注册同源链路重新登录" in message for message in logs)


def test_auth_context_reset_keeps_chatgpt_session_cookie():
    engine = RegistrationEngine(_DummyEmailService(), callback_logger=lambda _message: None)
    assert engine._init_latest_chatgpt_session() is True
    engine.session.cookies.set(
        "__Secure-next-auth.session-token",
        "chatgpt-session",
        domain=".chatgpt.com",
        path="/",
        secure=True,
    )
    engine.session.cookies.set(
        "oai-client-auth-session",
        "stale-auth-session",
        domain=".auth.openai.com",
        path="/",
        secure=True,
    )
    engine.session.cookies.set(
        "login_session",
        "stale-login-session",
        domain=".auth.openai.com",
        path="/",
        secure=True,
    )

    removed = engine._clear_auth_openai_cookies()

    remaining = {
        (str(cookie.get("name") or ""), str(cookie.get("domain") or ""))
        for cookie in (_iter_cookie_records(engine.session.cookies) or [])
    }
    assert removed == 2
    assert ("__Secure-next-auth.session-token", ".chatgpt.com") in remaining
    assert not any("auth.openai.com" in domain for _name, domain in remaining)


def test_completed_auth_step_reset_preserves_unified_session_cookies():
    engine = RegistrationEngine(_DummyEmailService(), callback_logger=lambda _message: None)
    assert engine._init_latest_chatgpt_session() is True
    cookie_values = {
        "login_session": "stale-login",
        "auth_provider": "stale-provider",
        "auth-session-minimized": "stale-minimized",
        "auth-session-minimized-client-checksum": "stale-checksum",
        "oai-client-auth-session": "continuity-auth-session",
        "hydra_redirect": "continuity-hydra",
        "unified_session_manifest": "continuity-manifest",
        "usc_test": "continuity-usc",
    }
    for name, value in cookie_values.items():
        engine.session.cookies.set(
            name,
            value,
            domain="auth.openai.com",
            path="/",
            secure=True,
        )

    removed = engine._clear_completed_auth_step_cookies()

    remaining = {
        str(cookie.get("name") or "")
        for cookie in (_iter_cookie_records(engine.session.cookies) or [])
    }
    assert removed == 4
    assert not {
        "login_session",
        "auth_provider",
        "auth-session-minimized",
        "auth-session-minimized-client-checksum",
    } & remaining
    assert {
        "oai-client-auth-session",
        "hydra_redirect",
        "unified_session_manifest",
        "usc_test",
    } <= remaining

def test_mfa_cookie_header_drops_stale_auth_and_cloudflare_state():
    header = _mfa_cookie_header(
        "; ".join(
            [
                "__Secure-next-auth.session-token=old-token",
                "__Secure-oai-is=oai-is",
                "_account=account-id",
                "oai-did=device-id",
                "oai-client-auth-session=stale-auth-session",
                "cf_clearance=stale-clearance",
            ]
        ),
        "new-token",
    )

    assert header == (
        "__Secure-next-auth.session-token=new-token; "
        "__Secure-oai-is=oai-is; _account=account-id; oai-did=device-id"
    )
    assert "oai-client-auth-session" not in header
    assert "cf_clearance" not in header


class _Reauth431Session(_Session):
    def __init__(self):
        super().__init__({"__Secure-next-auth.session-token": "session-token"})

    def post(self, url, **kwargs):
        assert url.endswith(
            "/api/auth/signin/openai?connection=password&login_hint=user%40example.com"
            "&reauth=password&post_login_add_password=true&max_age=0&ext-oai-did=device-id"
        )
        return _Response(
            200,
            url=url,
            payload={"url": "https://auth.openai.com/api/accounts/authorize?state=test"},
        )

    def get(self, url, **kwargs):
        if url.endswith("/api/auth/providers"):
            return _Response(200, url=url, payload={})
        if url.endswith("/api/auth/csrf"):
            return _Response(200, url=url, payload={"csrfToken": "csrf-token"})
        if "/api/accounts/authorize" in url:
            return _Response(302, url=url, headers={"Location": "/email-verification"})
        if url == "https://auth.openai.com/email-verification":
            return _Response(431, url=url, text="Request Header Fields Too Large")
        raise AssertionError(f"unexpected GET {url}")


def test_add_password_stops_before_otp_when_reauth_page_returns_431():
    engine = RegistrationEngine(_DummyEmailService(), callback_logger=lambda _message: None)
    engine.session = _Reauth431Session()
    engine.email = "user@example.com"
    engine.password = "Password123!"
    engine._device_id = "device-id"
    engine.set_password_after_register = True
    engine._refresh_mailbox_before_ids = lambda: None
    engine._get_verification_code = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("431 页面后不应继续等待验证码")
    )

    assert engine._latest_chatgpt_add_password_after_register("") is False
    assert engine._post_register_password_error.startswith("reauth_page_http_431")


class _OtpRetrySession(_Session):
    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if len(self.posts) == 1:
            return _Response(
                400,
                url=url,
                payload={
                    "error": {
                        "code": "invalid_auth_step",
                        "type": "invalid_request_error",
                        "message": "invalid auth step",
                    }
                },
                text='{"error":{"code":"invalid_auth_step"}}',
            )
        return _Response(
            200,
            url=url,
            payload={
                "continue_url": "https://auth.openai.com/reset-password/new-password",
                "page": {"type": "reset_password_new_password"},
            },
        )


def test_otp_retry_replaces_sentinel_so_token():
    engine = RegistrationEngine(_DummyEmailService(), callback_logger=lambda _message: None)
    engine.session = _OtpRetrySession({"oai-did": "device-id"})
    engine._device_id = "device-id"
    sentinels = iter(
        [
            SentinelPayload(p="p1", t="t1", c="c1", flow="email_otp_validate", so_token="old-so"),
            SentinelPayload(p="p2", t="t2", c="c2", flow="email_otp_validate", so_token="new-so"),
        ]
    )
    engine._check_sentinel = lambda *args, **kwargs: next(sentinels)
    engine._latest_chatgpt_headless_auth_json = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("disabled"))

    payload = engine._latest_chatgpt_validate_email_otp("123456")

    assert payload["page"]["type"] == "reset_password_new_password"
    assert len(engine.session.posts) == 2
    first_headers = engine.session.posts[0][1]["headers"]
    retry_headers = engine.session.posts[1][1]["headers"]
    assert first_headers["openai-sentinel-so-token"] == "old-so"
    assert retry_headers["openai-sentinel-so-token"] == "new-so"


def test_sentinel_so_header_normalizes_raw_json_and_object_values():
    expected = {
        "so": "observer-value",
        "c": "current-challenge",
        "id": "device-id",
        "flow": "oauth_create_account",
    }
    raw_header = RegistrationEngine._sentinel_so_header(
        "observer-value",
        challenge="current-challenge",
        device_id="device-id",
        flow="oauth_create_account",
    )
    json_header = RegistrationEngine._sentinel_so_header(
        '{"so":"observer-value","c":"stale-challenge"}',
        challenge="current-challenge",
        device_id="device-id",
        flow="oauth_create_account",
    )
    object_header = RegistrationEngine._sentinel_so_header(
        {"so": "observer-value", "c": "stale-challenge"},
        challenge="current-challenge",
        device_id="device-id",
        flow="oauth_create_account",
    )

    assert json.loads(raw_header) == expected
    assert json.loads(json_header) == expected
    assert json.loads(object_header) == expected


def test_quickjs_observer_object_becomes_valid_so_header(monkeypatch):
    import platforms.chatgpt.authflow_experimental.sentinel_quickjs as sentinel_quickjs

    monkeypatch.setattr(
        sentinel_quickjs,
        "get_sentinel_tokens_via_quickjs",
        lambda *args, **kwargs: {
            "token": json.dumps(
                {
                    "p": "proof",
                    "t": "turnstile",
                    "c": "current-challenge",
                    "id": "device-id",
                    "flow": "oauth_create_account",
                }
            ),
            "so_token": {"so": "observer-value", "c": "sdk-challenge"},
        },
    )
    engine = RegistrationEngine(_DummyEmailService(), callback_logger=lambda _message: None)

    payload = engine._quickjs_sentinel_payload(
        object(),
        "device-id",
        flow="oauth_create_account",
        user_agent="Mozilla/5.0",
        label="test",
    )

    assert payload is not None
    assert json.loads(payload.so_token) == {
        "so": "observer-value",
        "c": "current-challenge",
        "id": "device-id",
        "flow": "oauth_create_account",
    }


class _AboutYouSession(_Session):
    def __init__(self, response):
        super().__init__({"oai-did": "device-id"})
        self.response = response
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.response


def test_about_you_continue_url_is_visited_before_create_account():
    response = _Response(200, url="https://auth.openai.com/about-you", text="<html></html>")
    engine = RegistrationEngine(_DummyEmailService(), callback_logger=lambda _message: None)
    engine.session = _AboutYouSession(response)

    assert engine._latest_chatgpt_open_about_you("/about-you") is True
    assert engine._last_about_you_error == ""
    assert engine._email_otp_continue_url == "https://auth.openai.com/about-you"
    assert len(engine.session.gets) == 1
    url, kwargs = engine.session.gets[0]
    assert url == "https://auth.openai.com/about-you"
    assert kwargs["allow_redirects"] is True
    assert kwargs["timeout"] == 30
    assert kwargs["headers"]["referer"] == "https://auth.openai.com/email-verification"
    assert kwargs["headers"]["sec-fetch-dest"] == "document"
    assert kwargs["headers"]["sec-fetch-mode"] == "navigate"


def test_about_you_http_failure_stops_registration_step():
    response = _Response(403, url="https://auth.openai.com/about-you", text="forbidden")
    engine = RegistrationEngine(_DummyEmailService(), callback_logger=lambda _message: None)
    engine.session = _AboutYouSession(response)

    assert engine._latest_chatgpt_open_about_you("/about-you") is False
    assert engine._last_about_you_error == "http_403:forbidden"


class _SetupFakeModel:
    def __init__(self, *, platform="chatgpt", password=""):
        self.id = 1
        self.platform = platform
        self.password = password


class _SetupFakeAccount:
    def __init__(self, *, email, extra, password="", region=""):
        self.email = email
        self.extra = dict(extra or {})
        self.password = password
        self.region = region


class _SetupFakeSession:
    current_model = None

    def __init__(self, engine):
        self.model = _SetupFakeSession.current_model

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, model_cls, account_id):
        return self.model


class _SetupFakeLogger:
    def __init__(self):
        self.logs = []

    def log(self, message, *args, **kwargs):
        self.logs.append(message)
        return True

    def is_cancel_requested(self):
        return False


def _run_security_setup(monkeypatch, *, model, account):
    import application.tasks as tasks_module
    import platforms.chatgpt.plugin as plugin_module

    calls = {"persist": [], "set_password": [], "auto_enable": []}
    _SetupFakeSession.current_model = model

    def fake_build_platform_account(session, model):
        return account

    def fake_persist(account_id, *, password, extra):
        calls["persist"].append((account_id, password, dict(extra or {})))
        return None

    def fake_set_password_protocol(account, *, password, proxy, logger):
        calls["set_password"].append(password)
        account.extra = dict(account.extra or {})
        account.extra["password_set_after_register"] = True
        return True, {"password_set_after_register": "true"}, ""

    def fake_auto_enable(account, logger, *, proxy=None, enable=None, require_password_set=False):
        calls["auto_enable"].append(require_password_set)
        account.extra = dict(account.extra or {})
        account.extra["mfa_enabled"] = "true"
        account.extra["totp_secret"] = "fake-totp"

    def fake_generate_password():
        return "GeneratedPass1!"

    def fake_proxy_log_value(proxy):
        return proxy

    def fake_resolve_action_proxy(configured, *, region="", log_fn=None, action_label=""):
        return "proxy:default"

    monkeypatch.setattr(tasks_module, "Session", _SetupFakeSession)
    monkeypatch.setattr(tasks_module, "build_platform_account", fake_build_platform_account)
    monkeypatch.setattr(tasks_module, "_persist_chatgpt_security_setup", fake_persist)
    monkeypatch.setattr(tasks_module, "_set_chatgpt_account_password_protocol", fake_set_password_protocol)
    monkeypatch.setattr(tasks_module, "_auto_enable_chatgpt_2fa_after_register", fake_auto_enable)
    monkeypatch.setattr(plugin_module, "_generate_chatgpt_registration_password", fake_generate_password)
    monkeypatch.setattr(plugin_module, "_proxy_log_value", fake_proxy_log_value)
    monkeypatch.setattr(plugin_module, "_resolve_action_proxy", fake_resolve_action_proxy)

    logger = _SetupFakeLogger()
    result = tasks_module._setup_chatgpt_password_and_2fa_for_account(
        1,
        configured_proxy=None,
        logger=logger,
    )
    return result, calls, logger


def test_security_setup_2fa_bound_missing_password_runs_only_password(monkeypatch):
    # 已有2FA但无密码：只跑密码协议，2FA 链路不再调用。
    model = _SetupFakeModel(password="")
    account = _SetupFakeAccount(email="user@example.com", extra={"mfa_enabled": "true", "session_token": "stale"})
    result, calls, logger = _run_security_setup(monkeypatch, model=model, account=account)

    assert result["ok"] is True
    assert result.get("skipped_2fa") is True
    assert result.get("password_action") == "set"
    assert result.get("two_fa_action") == "skipped"
    assert calls["set_password"] != []
    assert calls["auto_enable"] == []
    assert any(str(log).startswith("检测:") for log in logger.logs)


def test_security_setup_stored_password_missing_2fa_keeps_password(monkeypatch):
    # 已有密码（仅 stored_password，无标记）+ 缺2FA：不跑密码协议，2FA 以 require_password_set=False 调用。
    model = _SetupFakeModel(password="existing-pass")
    account = _SetupFakeAccount(email="user@example.com", extra={})
    result, calls, logger = _run_security_setup(monkeypatch, model=model, account=account)

    assert result["ok"] is True
    assert result.get("password_action") == "kept"
    assert result.get("two_fa_action") == "set"
    assert calls["set_password"] == []
    assert calls["auto_enable"] == [False]
    assert account.password == "existing-pass"


def test_security_setup_both_present_skips_everything(monkeypatch):
    # 两者都有：全部跳过，密码协议与2FA链路都不调用。
    model = _SetupFakeModel(password="existing-pass")
    account = _SetupFakeAccount(
        email="user@example.com",
        extra={"mfa_enabled": "true", "password_set_after_register": "true"},
    )
    result, calls, logger = _run_security_setup(monkeypatch, model=model, account=account)

    assert result["ok"] is True
    assert result.get("skipped") is True
    assert result.get("password_action") == "kept"
    assert result.get("two_fa_action") == "skipped"
    assert calls["set_password"] == []
    assert calls["auto_enable"] == []
    assert calls["persist"] == []


def test_security_setup_both_missing_password_then_2fa(monkeypatch):
    # 两者都缺：先设置密码，再绑定2FA，require_password_set=True。
    model = _SetupFakeModel(password="")
    account = _SetupFakeAccount(email="user@example.com", extra={"session_token": "stale"})
    result, calls, logger = _run_security_setup(monkeypatch, model=model, account=account)

    assert result["ok"] is True
    assert result.get("password_action") == "set"
    assert result.get("two_fa_action") == "set"
    assert calls["set_password"] != []
    assert calls["auto_enable"] == [True]
    assert calls["persist"] != []
    # 密码阶段先于2FA阶段执行
    assert calls["set_password"][0] == "GeneratedPass1!"

