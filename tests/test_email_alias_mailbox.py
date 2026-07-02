from __future__ import annotations

import pytest

from application import tasks as tasks_module
from core.base_mailbox import BaseMailbox, MailboxAccount
from core.base_platform import Account
from core.db import save_account
from core.email_alias_mailbox import EmailAliasMailbox, get_email_alias_usage


class FakeMailbox(BaseMailbox):
    def __init__(self, email: str = "main@example.com"):
        self.account = MailboxAccount(
            email=email,
            account_id="mailbox-1",
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "fake_mailbox",
                    "resource_type": "mailbox",
                    "resource_identifier": "mailbox-1",
                    "handle": email,
                    "display_name": email,
                    "metadata": {"email": email},
                },
            },
        )
        self.current_id_emails: list[str] = []
        self.released: list[str] = []
        self.marked_success: list[str] = []

    def get_email(self) -> MailboxAccount:
        return self.account

    def get_current_ids(self, account: MailboxAccount) -> set:
        self.current_id_emails.append(account.email)
        return {"message-1"}

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        return "123456"

    def _release_local_account_reservation(self, account: MailboxAccount) -> None:
        self.released.append(account.email)

    def mark_registration_success(self, account: MailboxAccount) -> list[str]:
        self.marked_success.append(account.email)
        self._release_local_account_reservation(account)
        return ["registered"]


class SequentialMailbox(FakeMailbox):
    def __init__(self, emails: list[str]):
        super().__init__(emails[0])
        self.accounts = [
            MailboxAccount(
                email=email,
                account_id=f"mailbox-{index + 1}",
                extra={
                    "provider_resource": {
                        "provider_type": "mailbox",
                        "provider_name": "fake_mailbox",
                        "resource_type": "mailbox",
                        "resource_identifier": f"mailbox-{index + 1}",
                        "handle": email,
                        "display_name": email,
                        "metadata": {"email": email},
                    },
                },
            )
            for index, email in enumerate(emails)
        ]
        self.index = 0

    def get_email(self) -> MailboxAccount:
        if self.index >= len(self.accounts):
            raise RuntimeError("outlookEmail 当前可用邮箱都已被本机其他任务占用")
        account = self.accounts[self.index]
        self.index += 1
        return account


def _mailbox_resource(email: str, *, parent_email: str = "", is_alias: bool = False) -> dict:
    metadata = {"email": email}
    if is_alias:
        metadata.update(
            {
                "is_email_alias": True,
                "email_alias_enabled": True,
                "alias_email": email,
                "alias_parent_email": parent_email,
                "alias_parent_account_id": "mailbox-1",
            }
        )
    return {
        "provider_type": "mailbox",
        "provider_name": "fake_mailbox",
        "resource_type": "mailbox",
        "resource_identifier": "mailbox-1",
        "handle": email,
        "display_name": email,
        "metadata": metadata,
    }


def _save_registered(email: str, *, parent_email: str = "", is_alias: bool = False, platform: str = "chatgpt") -> None:
    save_account(
        Account(
            platform=platform,
            email=email,
            password="Secret123!",
            user_id=email,
            extra={
                "provider_resources": [
                    _mailbox_resource(email, parent_email=parent_email, is_alias=is_alias)
                ],
            },
        )
    )


def test_email_alias_mailbox_generates_alias_and_reads_parent_inbox():
    mailbox = FakeMailbox()
    wrapper = EmailAliasMailbox(mailbox, alias_limit=4, platform="chatgpt")

    account = wrapper.get_email()

    assert account.email.startswith("main+")
    assert account.email.endswith("@example.com")
    assert account.account_id == "mailbox-1"
    assert account.extra["email_alias"]["parent_email"] == "main@example.com"
    assert account.extra["provider_resource"]["metadata"]["alias_parent_email"] == "main@example.com"
    assert wrapper.get_current_ids(account) == {"message-1"}
    assert mailbox.current_id_emails == ["main@example.com"]


def test_email_alias_usage_counts_saved_main_and_alias_accounts():
    _save_registered("main@example.com")
    _save_registered("main+one@example.com", parent_email="main@example.com", is_alias=True)
    _save_registered(
        "main+other-platform@example.com",
        parent_email="main@example.com",
        is_alias=True,
        platform="windsurf",
    )

    usage = get_email_alias_usage("main@example.com", platform="chatgpt")

    assert usage.main_success_count == 1
    assert usage.alias_success_count == 1
    assert usage.total_success_count == 2


def test_email_alias_mailbox_rejects_parent_after_alias_limit():
    _save_registered("main+one@example.com", parent_email="main@example.com", is_alias=True)
    _save_registered("main+two@example.com", parent_email="main@example.com", is_alias=True)
    mailbox = FakeMailbox()
    wrapper = EmailAliasMailbox(mailbox, alias_limit=2, platform="chatgpt")

    with pytest.raises(RuntimeError, match="Email alias quota exhausted"):
        wrapper.get_email()

    assert mailbox.released == ["main@example.com"]
    assert mailbox.marked_success == ["main@example.com"]


def test_email_alias_quota_exhausted_switches_to_next_parent():
    _save_registered("main+one@example.com", parent_email="main@example.com", is_alias=True)
    _save_registered("main+two@example.com", parent_email="main@example.com", is_alias=True)
    mailbox = SequentialMailbox(["main@example.com", "fresh@example.com"])
    wrapper = EmailAliasMailbox(mailbox, alias_limit=2, platform="chatgpt")

    account = wrapper.get_email()

    assert account.email.startswith("fresh+")
    assert account.extra["email_alias"]["parent_email"] == "fresh@example.com"
    assert mailbox.marked_success == ["main@example.com"]
    assert mailbox.released == ["main@example.com"]


def test_email_alias_quota_exhausted_releases_parent_when_pool_reports_occupied():
    _save_registered("main+one@example.com", parent_email="main@example.com", is_alias=True)
    _save_registered("main+two@example.com", parent_email="main@example.com", is_alias=True)

    class OccupiedAfterFirstMailbox(FakeMailbox):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def get_email(self) -> MailboxAccount:
            self.calls += 1
            if self.calls == 1:
                return self.account
            raise RuntimeError("outlookEmail 当前可用邮箱都已被本机其他任务占用")

    mailbox = OccupiedAfterFirstMailbox()
    wrapper = EmailAliasMailbox(mailbox, alias_limit=2, platform="chatgpt")

    with pytest.raises(RuntimeError, match="Email alias quota exhausted"):
        wrapper.get_email()

    assert mailbox.released
    assert mailbox.released[0] == "main@example.com"
    assert mailbox.marked_success == ["main@example.com"]


def test_email_alias_success_releases_parent_until_total_limit():
    mailbox = FakeMailbox()
    wrapper = EmailAliasMailbox(mailbox, alias_limit=2, platform="chatgpt")
    account = wrapper.get_email()
    _save_registered(account.email, parent_email="main@example.com", is_alias=True)

    assert wrapper.mark_registration_success(account) == []
    assert mailbox.marked_success == []
    assert mailbox.released == ["main@example.com"]


def test_email_alias_success_marks_parent_when_total_limit_reached():
    _save_registered("main@example.com")
    mailbox = FakeMailbox()
    wrapper = EmailAliasMailbox(mailbox, alias_limit=1, platform="chatgpt")
    account = wrapper.get_email()
    _save_registered(account.email, parent_email="main@example.com", is_alias=True)

    assert wrapper.mark_registration_success(account) == ["registered"]
    assert mailbox.marked_success == ["main@example.com"]


def test_build_platform_instance_wraps_mailbox_when_email_alias_enabled(monkeypatch):
    class FakePlatform:
        def __init__(self, *, config, mailbox=None):
            self.config = config
            self.mailbox = mailbox

        def set_logger(self, log_fn):
            self.log_fn = log_fn

    class FakeLogger:
        def log(self, *_args, **_kwargs):
            return None

    fake_mailbox = FakeMailbox()
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **_kwargs: fake_mailbox)
    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: FakePlatform)

    platform = tasks_module._build_platform_instance(
        "chatgpt",
        {
            "executor_type": "protocol",
            "extra": {
                "identity_provider": "mailbox",
                "mail_provider": "fake_mailbox",
                "enable_email_alias": True,
                "email_alias_limit": 3,
            },
        },
        FakeLogger(),
    )

    assert isinstance(platform.mailbox, EmailAliasMailbox)
    assert platform.mailbox.alias_limit == 3
