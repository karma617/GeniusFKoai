# -*- coding: utf-8 -*-
"""rebind_account_email 协议测试（全 mock，不访问真实网络）。

覆盖：成功、change_email/begin 失败、change_email/verify 失败、新邮箱取码超时、
重新登录失败、mail_config 缺参、解析器异常兜底（不向外层抛出）、新旧邮箱相同。
"""
from __future__ import annotations

import types

import pytest

import platforms.chatgpt.email_rebind as email_rebind
from core.base_mailbox import MailboxAccount
from platforms.chatgpt.register import RegistrationResult

OLD_EMAIL = "old@example.com"
NEW_EMAIL = "fresh12345@cloud.example.com"
MAIL_CONFIG = {
    "api_url": "https://cloud-mail.example.com",
    "api_token": "token-xyz",
    "domains": ["cloud.example.com"],
}


class FakeEmailService:
    service_type = types.SimpleNamespace(value="fake_mailbox")

    def create_email(self, config=None):
        return {"email": OLD_EMAIL, "service_id": OLD_EMAIL, "token": OLD_EMAIL}


class FakeMailbox:
    instances = []
    next_wait_behavior = "code"  # code | timeout | error | empty
    next_get_email_error = None
    next_get_email_errors: list = []  # 按顺序抛出（域名预检重试场景用完即止）
    next_new_email = NEW_EMAIL

    def __init__(self, api_url="", admin_token="", domain="", fingerprint="", proxy=None):
        self.api_url = api_url
        self.admin_token = admin_token
        self.domain = domain
        self.proxy = proxy
        self.wait_calls = []
        self._api_mode = "auto"
        self.get_email_calls = 0
        FakeMailbox.instances.append(self)

    @property
    def wait_behavior(self):
        return FakeMailbox.next_wait_behavior

    @property
    def new_email(self):
        return FakeMailbox.next_new_email

    def get_email(self):
        self.get_email_calls += 1
        if FakeMailbox.next_get_email_errors:
            raise FakeMailbox.next_get_email_errors.pop(0)
        if FakeMailbox.next_get_email_error is not None:
            raise FakeMailbox.next_get_email_error
        email = FakeMailbox.next_new_email
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "cloud_mail",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "api_url": self.api_url,
                        "domain": self.domain,
                        "api_mode": "cloud_mail",
                    },
                },
            },
        )

    def get_current_ids(self, account):
        return {"mail-1"}

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None,
                      code_pattern=None, otp_sent_at=None):
        self.wait_calls.append({
            "account": account,
            "keyword": keyword,
            "timeout": timeout,
            "before_ids": set(before_ids or set()),
        })
        behavior = FakeMailbox.next_wait_behavior
        if behavior == "timeout":
            raise TimeoutError("等待验证码超时")
        if behavior == "error":
            raise RuntimeError("mailbox boom")
        if behavior == "empty":
            return ""
        return "654321"


class FakeRegistrationEngine:
    instances = []
    next_result = None
    next_error = None

    def __init__(self, email_service=None, proxy_url=None, callback_logger=None, **kwargs):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger
        self.email = ""
        self.password = ""
        self.totp_secret = ""
        self.prefer_password_totp_login = False
        self.k12_join_enabled = None
        self.set_password_after_register = None
        self.raise_error = FakeRegistrationEngine.next_error
        self.result = FakeRegistrationEngine.next_result
        FakeRegistrationEngine.instances.append(self)

    def run_chatgpt_refresh_session_latest(self, result=None, **kwargs):
        if self.raise_error is not None:
            raise self.raise_error
        return self.result


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.url = "https://chatgpt.com/backend-api/accounts/change_email"


class FakeSession:
    instances = []

    def __init__(self):
        self.calls = []
        self.begin_response = FakeResponse(200, "{}")
        self.verify_response = FakeResponse(200, "{}")
        self.closed = False
        FakeSession.instances.append(self)

    def post(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        if url.endswith("/change_email/begin"):
            return self.begin_response
        if url.endswith("/change_email/verify"):
            return self.verify_response
        raise AssertionError("unexpected url: " + url)

    def close(self):
        self.closed = True


class FakeResolver:
    """记录 resolver 收到的 account_obj，供断言内存增强副本。"""

    calls = []

    @classmethod
    def reset(cls):
        cls.calls.clear()

    @classmethod
    def install(cls, monkeypatch, *, resolver_result=None, resolver_raise=None):
        def fake_resolver(account_obj, log, proxy):
            cls.calls.append(account_obj)
            if resolver_raise is not None:
                raise resolver_raise
            if resolver_result is not None:
                return resolver_result
            return FakeEmailService(), ""

        monkeypatch.setattr(email_rebind, "_resolve_account_mailbox_email_service", fake_resolver)


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeMailbox.instances.clear()
    FakeMailbox.next_wait_behavior = "code"
    FakeMailbox.next_get_email_error = None
    FakeMailbox.next_get_email_errors = []
    FakeMailbox.next_new_email = NEW_EMAIL
    FakeRegistrationEngine.instances.clear()
    FakeRegistrationEngine.next_result = None
    FakeRegistrationEngine.next_error = None
    FakeSession.instances.clear()
    FakeResolver.reset()
    yield


def _install_fakes(monkeypatch, *, resolver_result=None, resolver_raise=None):
    """挂接全部 mock；resolver_result 为 None 时返回可用 email_service。"""
    session = FakeSession()
    FakeResolver.install(
        monkeypatch,
        resolver_result=resolver_result,
        resolver_raise=resolver_raise,
    )
    monkeypatch.setattr(email_rebind, "RegistrationEngine", FakeRegistrationEngine)
    monkeypatch.setattr(email_rebind, "CFWorkerMailbox", FakeMailbox)
    monkeypatch.setattr(
        email_rebind,
        "curl_requests",
        types.SimpleNamespace(Session=lambda impersonate=None: session),
    )
    return session


def _success_engine_result():
    return RegistrationResult(
        success=True,
        email=OLD_EMAIL,
        access_token="at-new",
        refresh_token="rt-new",
        id_token="id-new",
        metadata={"session": {}},
    )


def test_rebind_success_returns_tokens_and_mailbox_resource(monkeypatch):
    session = _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = _success_engine_result()

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL, "extra": {"provider_resources": []}},
        dict(MAIL_CONFIG),
        proxy="",
        log_fn=lambda message: None,
    )

    assert result["ok"] is True
    assert result["old_email"] == OLD_EMAIL
    assert result["new_email"] == NEW_EMAIL
    assert result["access_token"] == "at-new"
    assert result["refresh_token"] == "rt-new"
    assert result["id_token"] == "id-new"
    assert result["mailbox_resource"]["handle"] == NEW_EMAIL
    assert result["mailbox_resource"]["provider_name"] == "cloud_mail"
    assert result.get("error", "") == ""

    # 新邮箱构造参数来自独立 mail_config
    mailbox = FakeMailbox.instances[-1]
    assert mailbox.api_url == MAIL_CONFIG["api_url"]
    assert mailbox.admin_token == MAIL_CONFIG["api_token"]
    assert mailbox.domain == "cloud.example.com"

    # begin -> 取码 -> verify 顺序与载荷
    assert [call["url"] for call in session.calls] == [
        "https://chatgpt.com/backend-api/accounts/change_email/begin",
        "https://chatgpt.com/backend-api/accounts/change_email/verify",
    ]
    begin_kwargs = session.calls[0]["kwargs"]
    assert begin_kwargs["json"] == {"email": NEW_EMAIL}
    assert begin_kwargs["headers"]["authorization"] == "Bearer at-new"
    verify_kwargs = session.calls[1]["kwargs"]
    assert verify_kwargs["json"] == {"email": NEW_EMAIL, "code": "654321"}
    assert session.closed is True

    # 收码使用新建邮箱账号，过滤旧邮件
    assert len(mailbox.wait_calls) == 1
    assert mailbox.wait_calls[0]["account"].email == NEW_EMAIL
    assert mailbox.wait_calls[0]["before_ids"] == {"mail-1"}
    assert mailbox.wait_calls[0]["timeout"] >= 300

    # 登录引擎按旧账号 OTP 链路构造
    engine = FakeRegistrationEngine.instances[0]
    assert engine.email == OLD_EMAIL
    assert engine.proxy_url == ""
    assert engine.k12_join_enabled is False
    assert engine.set_password_after_register is False


def test_rebind_begin_failure_returns_structured_error(monkeypatch):
    session = _install_fakes(monkeypatch)
    session.begin_response = FakeResponse(403, "forbidden by cloudflare")
    FakeRegistrationEngine.next_result = _success_engine_result()

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert result["old_email"] == OLD_EMAIL
    assert result["new_email"] == NEW_EMAIL
    assert "change_email/begin 403" in result["error"]
    assert "forbidden" in result["error"]
    # begin 失败后不再取码、不再 verify
    assert len(session.calls) == 1
    assert FakeMailbox.instances[-1].wait_calls == []
    assert session.closed is True


def test_rebind_verify_failure_returns_structured_error(monkeypatch):
    session = _install_fakes(monkeypatch)
    session.verify_response = FakeResponse(400, '{"error":{"code":"invalid_otp"}}')
    FakeRegistrationEngine.next_result = _success_engine_result()

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert result["new_email"] == NEW_EMAIL
    assert "change_email/verify 400" in result["error"]
    assert "invalid_otp" in result["error"]
    assert len(session.calls) == 2
    assert len(FakeMailbox.instances[-1].wait_calls) == 1


def test_rebind_new_code_timeout_returns_structured_error(monkeypatch):
    session = _install_fakes(monkeypatch)
    FakeMailbox.next_wait_behavior = "timeout"
    FakeRegistrationEngine.next_result = _success_engine_result()

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert result["new_email"] == NEW_EMAIL
    assert result["error"] == "新邮箱验证码超时未收到"
    # begin 已发出但 verify 未执行
    assert [call["url"] for call in session.calls] == [
        "https://chatgpt.com/backend-api/accounts/change_email/begin"
    ]


def test_rebind_new_code_poll_error_returns_structured_error(monkeypatch):
    _install_fakes(monkeypatch)
    FakeMailbox.next_wait_behavior = "error"
    FakeRegistrationEngine.next_result = _success_engine_result()

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert "取新邮箱验证码失败" in result["error"]
    assert "mailbox boom" in result["error"]


def test_rebind_login_failure_returns_structured_error(monkeypatch):
    session = _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = RegistrationResult(
        success=False, email=OLD_EMAIL, error_message="重新登录未进入邮箱验证码或 callback 步骤"
    )

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert result["new_email"] == ""
    assert "重新登录失败" in result["error"]
    assert "未进入邮箱验证码" in result["error"]
    # 登录失败不创建新邮箱、不发协议请求
    assert FakeMailbox.instances == []
    assert session.calls == []


def test_rebind_missing_mail_config_returns_structured_error(monkeypatch):
    session = _install_fakes(monkeypatch)

    missing_domain = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL},
        {"api_url": MAIL_CONFIG["api_url"], "api_token": MAIL_CONFIG["api_token"]},
        log_fn=lambda message: None,
    )
    assert missing_domain["ok"] is False
    assert "domains" in missing_domain["error"]

    missing_token = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL},
        {"api_url": MAIL_CONFIG["api_url"], "domains": ["cloud.example.com"]},
        log_fn=lambda message: None,
    )
    assert missing_token["ok"] is False
    assert "api_token" in missing_token["error"]

    missing_email = email_rebind.rebind_account_email(
        {}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )
    assert missing_email["ok"] is False
    assert "账号缺少邮箱" in missing_email["error"]

    # 校验失败不触发登录/邮箱/HTTP
    assert FakeRegistrationEngine.instances == []
    assert FakeMailbox.instances == []
    assert session.calls == []


def test_rebind_mailbox_resolver_error_returns_structured_error(monkeypatch):
    _install_fakes(monkeypatch, resolver_result=(None, "账号没有绑定邮箱 provider 资源"))

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert "旧邮箱收码服务不可用" in result["error"]
    assert "provider" in result["error"]
    assert FakeRegistrationEngine.instances == []
    assert FakeMailbox.instances == []


def test_rebind_unexpected_exception_is_isolated(monkeypatch):
    _install_fakes(monkeypatch, resolver_raise=RuntimeError("resolver exploded"))

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert result["old_email"] == OLD_EMAIL
    assert "换绑异常" in result["error"]
    assert "resolver exploded" in result["error"]


def test_rebind_rejects_same_new_email(monkeypatch):
    session = _install_fakes(monkeypatch)
    FakeMailbox.next_new_email = OLD_EMAIL
    FakeRegistrationEngine.next_result = _success_engine_result()

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert result["error"] == "新旧邮箱相同"
    assert session.calls == []


def test_rebind_passes_proxy_to_engine_mailbox_and_http(monkeypatch):
    session = _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = _success_engine_result()
    proxy = "http://127.0.0.1:7890"

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), proxy=proxy, log_fn=lambda message: None
    )

    assert result["ok"] is True
    assert FakeRegistrationEngine.instances[0].proxy_url == proxy
    mailbox = FakeMailbox.instances[-1]
    assert mailbox.proxy == proxy
    for call in session.calls:
        assert call["kwargs"]["proxies"] == {"http": proxy, "https": proxy}


def test_select_cloud_mail_domain_variants():
    assert email_rebind._select_cloud_mail_domain({"domains": ["a.com", "b.com"]}) == "a.com"
    assert email_rebind._select_cloud_mail_domain({"domains": " a.com , b.com "}) == "a.com"
    assert email_rebind._select_cloud_mail_domain({"domain": "c.com", "domains": ["a.com"]}) == "c.com"
    assert email_rebind._select_cloud_mail_domain({"domains": ["@a.com"]}) == "a.com"
    assert email_rebind._select_cloud_mail_domain({}) == ""


def _allocation_entries(domain, count, prefix="s"):
    return [
        {"subdomain": prefix + str(index), "account_email": "", "created_at": ""}
        for index in range(count)
    ]


def test_select_cloud_mail_domain_quota_aware_variants():
    allocations = {"a.com": _allocation_entries("a.com", 10), "b.com": []}
    # 满额主域名跳过，选下一个有余量的
    assert (
        email_rebind._select_cloud_mail_domain(
            {"domains": ["a.com", "b.com"]},
            subdomain_allocations=allocations,
            subdomain_limit=10,
        )
        == "b.com"
    )
    # 显式域名仍有余量时优先
    assert (
        email_rebind._select_cloud_mail_domain(
            {"domain": "c.com", "domains": ["a.com"]},
            subdomain_allocations={"c.com": _allocation_entries("c.com", 3)},
            subdomain_limit=10,
        )
        == "c.com"
    )
    # 全部满额 -> 空串
    full = {"a.com": _allocation_entries("a.com", 10), "b.com": _allocation_entries("b.com", 10)}
    assert (
        email_rebind._select_cloud_mail_domain(
            {"domains": ["a.com", "b.com"]},
            subdomain_allocations=full,
            subdomain_limit=10,
        )
        == ""
    )
    # 不传配额数据 -> 维持旧行为（不看占用）
    assert email_rebind._select_cloud_mail_domain({"domains": ["a.com", "b.com"]}) == "a.com"


def _stored_cloud_mail_resource(**metadata_overrides):
    """模拟首次换绑成功后落库的 mailbox_resource（安全：不含 api_token）。"""
    metadata = {
        "email": OLD_EMAIL,
        "api_url": MAIL_CONFIG["api_url"],
        "domain": "cloud.example.com",
        "api_mode": "cloud_mail",
    }
    metadata.update(metadata_overrides)
    return {
        "provider_type": "mailbox",
        "provider_name": "cloud_mail",
        "resource_type": "mailbox",
        "resource_identifier": OLD_EMAIL,
        "handle": OLD_EMAIL,
        "display_name": OLD_EMAIL,
        "metadata": metadata,
    }


def test_rebind_reinjects_token_into_compatible_cloud_mail_resource(monkeypatch):
    """第二次换绑：相容 cloud_mail 资源在内存副本中恢复 api_token，原 dict 与返回值不带 secret。"""
    _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = _success_engine_result()

    stored_resource = _stored_cloud_mail_resource(api_url=MAIL_CONFIG["api_url"] + "/")
    account = {"email": OLD_EMAIL, "extra": {"provider_resources": [stored_resource]}}

    result = email_rebind.rebind_account_email(
        account, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is True

    # resolver 收到的内存副本已注入独立 mail_config 的 token
    assert len(FakeResolver.calls) == 1
    enhanced_resource = FakeResolver.calls[0].extra["provider_resources"][0]
    assert enhanced_resource["metadata"]["api_token"] == MAIL_CONFIG["api_token"]
    assert enhanced_resource["metadata"]["api_url"] == MAIL_CONFIG["api_url"]
    assert enhanced_resource["metadata"]["domain"] == "cloud.example.com"
    # 注入只发生在副本上：调用方原始 dict 不被修改
    assert "api_token" not in stored_resource["metadata"]
    assert enhanced_resource["metadata"] is not stored_resource["metadata"]

    # secret 不进入返回 mailbox_resource
    assert "api_token" not in result["mailbox_resource"].get("metadata", {})
    assert "api_token" not in result["mailbox_resource"]


def test_rebind_leaves_incompatible_and_normal_resources_untouched(monkeypatch):
    """api_url 或 domain 不相容的 cloud_mail 资源、普通邮箱资源均不注入 token。"""
    _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = _success_engine_result()

    other_api_url = _stored_cloud_mail_resource(api_url="https://other-cloud.example.com")
    other_domain = _stored_cloud_mail_resource(domain="other.example.com")
    normal_mailbox = {
        "provider_type": "mailbox",
        "provider_name": "moemail_api",
        "resource_type": "mailbox",
        "resource_identifier": OLD_EMAIL,
        "handle": OLD_EMAIL,
        "metadata": {
            "email": OLD_EMAIL,
            "api_url": MAIL_CONFIG["api_url"],
            "domain": "cloud.example.com",
        },
    }
    account = {
        "email": OLD_EMAIL,
        "extra": {"provider_resources": [other_api_url, other_domain, normal_mailbox]},
    }

    result = email_rebind.rebind_account_email(
        account, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is True
    enhanced = FakeResolver.calls[0].extra["provider_resources"]
    for resource in enhanced:
        assert "api_token" not in resource["metadata"]
    assert enhanced[0]["metadata"]["api_url"] == "https://other-cloud.example.com"
    assert enhanced[1]["metadata"]["domain"] == "other.example.com"
    # 原始 dict 同样未被修改
    for resource in account["extra"]["provider_resources"]:
        assert "api_token" not in resource["metadata"]

SUB_EMAIL = "abc123xyz@sub7.cloud.example.com"


def test_rebind_uses_subdomain_domain_for_new_mailbox(monkeypatch):
    """任务层把 mail_config.domain 覆写为「子域名.主域名」后，新邮箱按该域名创建。"""
    session = _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = _success_engine_result()
    FakeMailbox.next_new_email = SUB_EMAIL
    config = dict(MAIL_CONFIG)
    config["domain"] = "sub7.cloud.example.com"

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL, "extra": {"provider_resources": []}},
        config,
        log_fn=lambda message: None,
    )

    assert result["ok"] is True
    assert result["new_email"] == SUB_EMAIL
    mailbox = FakeMailbox.instances[-1]
    assert mailbox.domain == "sub7.cloud.example.com"
    assert session.calls[0]["kwargs"]["json"] == {"email": SUB_EMAIL}
    assert session.calls[1]["kwargs"]["json"] == {"email": SUB_EMAIL, "code": "654321"}


def test_rebind_rejects_new_email_domain_mismatch(monkeypatch):
    """创建地址域名与目标不符 -> 结构化失败，不进入协议请求。"""
    session = _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = _success_engine_result()
    FakeMailbox.next_new_email = "abc123@other.example.com"

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert "与目标 cloud.example.com 不符" in result["error"]
    assert session.calls == []


def test_rebind_rejects_new_email_invalid_local_part(monkeypatch):
    """创建地址本地部分不合法 -> 结构化失败。"""
    _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = _success_engine_result()
    FakeMailbox.next_new_email = "prefix-abc123@cloud.example.com"

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=lambda message: None
    )

    assert result["ok"] is False
    assert "本地部分不合法" in result["error"]


def test_rebind_retries_add_user_when_domain_precheck_rejects_subdomain(monkeypatch):
    """worker domainList 预检未包含子域名域名时，跳过预检按完整地址重试一次。"""
    _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = _success_engine_result()
    FakeMailbox.next_get_email_errors = [
        RuntimeError(
            "Cloud Mail 未启用邮箱域名 sub7.cloud.example.com，当前可用域名: cloud.example.com"
        )
    ]
    FakeMailbox.next_new_email = SUB_EMAIL
    config = dict(MAIL_CONFIG)
    config["domain"] = "sub7.cloud.example.com"

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL, "extra": {"provider_resources": []}},
        config,
        log_fn=lambda message: None,
    )

    assert result["ok"] is True
    assert result["new_email"] == SUB_EMAIL
    mailbox = FakeMailbox.instances[-1]
    assert mailbox.get_email_calls == 2
    assert mailbox._api_mode == "cloud_mail"


def test_rebind_success_logs_key_protocol_nodes(monkeypatch):
    """关键节点逐行日志：OTP 登录、新邮箱地址、begin/verify 请求与响应、终态。"""
    _install_fakes(monkeypatch)
    FakeRegistrationEngine.next_result = _success_engine_result()
    logs: list[str] = []

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL, "extra": {"provider_resources": []}},
        dict(MAIL_CONFIG),
        log_fn=logs.append,
    )

    assert result["ok"] is True
    text = "\n".join(logs)
    assert "旧邮箱 OTP 新鲜登录" in text
    assert "重新登录成功" in text
    assert "新邮箱已生成: " + NEW_EMAIL in text
    assert "change_email/begin 请求 email=" + NEW_EMAIL in text
    assert "change_email/begin 响应 status=200" in text
    assert "change_email/verify 请求 email=" + NEW_EMAIL in text
    assert "code=654321" in text
    assert "change_email/verify 响应 status=200" in text
    assert "换绑成功" in text


def test_rebind_failure_logs_terminal_line(monkeypatch):
    """协议失败时输出「换绑失败」终态日志（供前端日志弹窗）。"""
    session = _install_fakes(monkeypatch)
    session.begin_response = FakeResponse(403, "forbidden by cloudflare")
    FakeRegistrationEngine.next_result = _success_engine_result()
    logs: list[str] = []

    result = email_rebind.rebind_account_email(
        {"email": OLD_EMAIL}, dict(MAIL_CONFIG), log_fn=logs.append
    )

    assert result["ok"] is False
    assert any(
        "换绑失败" in message and "change_email/begin 403" in message for message in logs
    )
    assert any("change_email/begin 响应 status=403" in message for message in logs)
