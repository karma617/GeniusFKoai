from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from core.base_mailbox import MailboxAccount
from core.base_platform import RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.protocol_authflow import ChatGPTAuthFlowProtocolWorker


class _DummyAuthResult:
    def __init__(self, email: str, password: str, access_token: str):
        self._data = {
            "email": email,
            "password": password,
            "session_token": "session-token",
            "access_token": access_token,
            "device_id": "device-id",
            "csrf_token": "csrf-token",
            "id_token": "id-token",
            "refresh_token": "refresh-token",
            "cookie_header": "__Secure-next-auth.session-token=session-token",
        }

    def to_dict(self) -> dict:
        return dict(self._data)


class _DummyAuthFlow:
    def __init__(self, config):
        self.config = config
        self.result = SimpleNamespace(email="", password="")

    def run_register(self, provider):
        email = provider.create_mailbox()
        code = provider.wait_for_otp(email, timeout=30, issued_after=0)
        assert code == "123456"
        return _DummyAuthResult(email, self.result.password, _jwt("acc-test"))


class _FakeMailbox:
    def __init__(self):
        self.wait_kwargs = None

    def get_current_ids(self, account):
        assert account.email == "user@example.com"
        return {"before-1"}

    def wait_for_code(self, account, **kwargs):
        self.wait_kwargs = kwargs
        return "123456"


def _jwt(account_id: str) -> str:
    payload = {
        "sub": "sub-test",
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{raw}.sig"


def test_authflow_worker_uses_current_mailbox_adapter(monkeypatch):
    from platforms.chatgpt.authflow_experimental import auth_flow as imported_auth_flow

    monkeypatch.setattr(imported_auth_flow, "AuthFlow", _DummyAuthFlow)
    mailbox = _FakeMailbox()
    worker = ChatGPTAuthFlowProtocolWorker(
        mailbox=mailbox,
        mailbox_account=MailboxAccount(email="user@example.com", account_id="mail-1"),
        proxy_url="http://127.0.0.1:8080",
        log_fn=lambda message: None,
    )

    result = worker.run(email="user@example.com", password="Secret123!")

    assert result.success is True
    assert result.email == "user@example.com"
    assert result.password == "Secret123!"
    assert result.account_id == "acc-test"
    assert result.session_token == "session-token"
    assert result.metadata["authflow_experimental"] is True
    assert mailbox.wait_kwargs["before_ids"] == {"before-1"}


def test_chatgpt_protocol_variant_builds_experimental_worker():
    mailbox = _FakeMailbox()
    platform = ChatGPTPlatform(
        RegisterConfig(
            extra={
                "mail_provider": "local_ms_pool",
            }
        ),
        mailbox=mailbox,
    )
    adapter = platform.build_protocol_mailbox_adapter()
    logs: list[str] = []
    ctx = SimpleNamespace(
        identity=SimpleNamespace(
            identity_provider="mailbox",
            mailbox_account=MailboxAccount(email="user@example.com", account_id="mail-1"),
        ),
        extra={"chatgpt_protocol_variant": "authflow_experimental"},
        proxy=None,
        log=logs.append,
    )

    worker = adapter.worker_builder(ctx, SimpleNamespace())

    assert isinstance(worker, ChatGPTAuthFlowProtocolWorker)
    assert any("实验 AuthFlow" in item for item in logs)
