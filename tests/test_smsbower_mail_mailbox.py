from __future__ import annotations

from core.base_mailbox import MAILBOX_FACTORY_REGISTRY, create_mailbox
from core.smsbower_mail_mailbox import SmsBowerMailMailbox


class _FakeResp:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def test_factory_registry_has_smsbower_mail():
    assert "smsbower_mail_api" in MAILBOX_FACTORY_REGISTRY


def test_create_mailbox_builds_smsbower_mail(monkeypatch):
    class FakeDefinitionsRepository:
        def get_by_key(self, provider_type: str, provider_key: str):
            assert provider_type == "mailbox"
            return type(
                "Def",
                (),
                {
                    "enabled": True,
                    "driver_type": "smsbower_mail_api",
                    "get_metadata": lambda self: {},
                },
            )()

    class FakeSettingsRepository:
        def resolve_runtime_settings(self, provider_type, provider_key, overrides=None):
            return {
                "smsbower_mail_api_key": "k",
                "smsbower_mail_service": "dr",
                "smsbower_mail_domain": "gmail.com",
                **(overrides or {}),
            }

    monkeypatch.setattr(
        "infrastructure.provider_definitions_repository.ProviderDefinitionsRepository",
        FakeDefinitionsRepository,
    )
    monkeypatch.setattr(
        "infrastructure.provider_settings_repository.ProviderSettingsRepository",
        FakeSettingsRepository,
    )

    mailbox = create_mailbox("smsbower_mail_api")
    assert isinstance(mailbox, SmsBowerMailMailbox)
    assert mailbox.service == "dr"
    assert mailbox.domain == "gmail.com"


def test_get_email_and_wait_for_code(monkeypatch):
    calls: list[tuple[str, dict]] = []

    mailbox = SmsBowerMailMailbox(
        api_key="test-key",
        service="dr",
        domain="gmail.com",
        alias="0",
        poll_interval=0.01,
    )

    def fake_get(url, params=None, timeout=30):
        path = url.split("smsbower.page", 1)[-1]
        params = dict(params or {})
        calls.append((path, params))
        if path.endswith("/api/mail/getActivation"):
            return _FakeResp({"status": 1, "mail": "demo@gmail.com", "mailId": 123})
        if path.endswith("/api/mail/getCode"):
            return _FakeResp({"status": 1, "code": "654321"})
        if path.endswith("/api/mail/setStatus"):
            return _FakeResp({"status": 1, "message": "Success"})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(mailbox._session, "get", fake_get)

    account = mailbox.get_email()
    assert account.email == "demo@gmail.com"
    assert account.account_id == "123"
    code = mailbox.wait_for_code(account, timeout=2)
    assert code == "654321"
    assert any(path.endswith("/api/mail/getActivation") for path, _ in calls)
    assert any(path.endswith("/api/mail/getCode") for path, _ in calls)
    assert any(path.endswith("/api/mail/setStatus") and params.get("status") == 3 for path, params in calls)


def test_peek_email_uses_price_rests(monkeypatch):
    mailbox = SmsBowerMailMailbox(api_key="k", service="dr", domain="gmail.com")

    def fake_get(url, params=None, timeout=30):
        assert "/api/mail/getPriceRests" in url
        return _FakeResp({"status": 1, "data": {"dr": {"gmail.com": {"price": 0.01, "count": 9}}}})

    monkeypatch.setattr(mailbox._session, "get", fake_get)
    text = mailbox.peek_email()
    assert "gmail.com" in text
    assert "count=9" in text
