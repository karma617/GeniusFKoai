from __future__ import annotations

from core.base_identity import create_identity_provider, normalize_identity_provider


class _FakeMailboxAccount:
    email = "phone-first@example.com"
    account_id = "mailbox-1"
    extra = {}


class _FakeMailbox:
    def __init__(self):
        self.account = _FakeMailboxAccount()

    def get_email(self):
        return self.account

    def get_current_ids(self, account):
        assert account is self.account
        return {"message-1"}


class _FailingBaselineMailbox(_FakeMailbox):
    def get_current_ids(self, account):
        raise RuntimeError("outlookEmail GET /api/external/messages 请求失败: HTTP 502")


def test_sms_oauth_identity_provider_aliases_and_metadata():
    for alias in ("sms_oauth", "phone_sms_oauth", "phone_first_oauth"):
        assert normalize_identity_provider(alias) == "sms_oauth"

    provider = create_identity_provider("sms_oauth", mailbox=_FakeMailbox())
    material = provider.resolve()

    assert material.identity_provider == "sms_oauth"
    assert material.email == "phone-first@example.com"
    assert material.mailbox_account.account_id == "mailbox-1"
    assert material.before_ids == {"message-1"}
    assert material.metadata["signup_method"] == "phone"
    assert material.metadata["plus_account_access_strategy"] == "sms_oauth"
    assert material.metadata["phone_signup_relogin_after_bind_email"] is True


def test_mailbox_identity_provider_keeps_email_when_baseline_fails():
    provider = create_identity_provider("mailbox", mailbox=_FailingBaselineMailbox())

    material = provider.resolve()

    assert material.email == "phone-first@example.com"
    assert material.mailbox_account.account_id == "mailbox-1"
    assert material.before_ids == set()
    assert "HTTP 502" in material.metadata["mailbox_baseline_error"]
