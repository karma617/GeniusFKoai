from types import SimpleNamespace

from core import base_mailbox
from core.base_mailbox import BaseMailbox, FallbackMailbox, MailboxAccount, create_mailbox


class FakeMailbox(BaseMailbox):
    def __init__(self, name: str):
        self.name = name

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=f"{self.name}@example.test")

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        return "123456"

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()


class FakeDefinitionsRepository:
    def get_by_key(self, provider_type: str, provider_key: str):
        if provider_type != "mailbox":
            return None
        return SimpleNamespace(
            enabled=True,
            driver_type=provider_key,
            get_metadata=lambda: {},
        )


class FakeSettingsRepository:
    def list_enabled(self, provider_type: str):
        return [
            SimpleNamespace(provider_key="gmail_api_code"),
            SimpleNamespace(provider_key="outlook_email_api"),
        ]

    def resolve_runtime_settings(self, provider_type: str, provider_key: str, overrides: dict | None = None) -> dict:
        return dict(overrides or {})


def test_create_mailbox_does_not_fallback_to_every_enabled_provider(monkeypatch):
    monkeypatch.setattr(
        "infrastructure.provider_definitions_repository.ProviderDefinitionsRepository",
        FakeDefinitionsRepository,
    )
    monkeypatch.setattr(
        "infrastructure.provider_settings_repository.ProviderSettingsRepository",
        FakeSettingsRepository,
    )
    monkeypatch.setitem(
        base_mailbox.MAILBOX_FACTORY_REGISTRY,
        "gmail_api_code",
        lambda extra, proxy: FakeMailbox("gmail"),
    )
    monkeypatch.setitem(
        base_mailbox.MAILBOX_FACTORY_REGISTRY,
        "outlook_email_api",
        lambda extra, proxy: FakeMailbox("outlook"),
    )

    mailbox = create_mailbox("gmail_api_code")

    assert isinstance(mailbox, FakeMailbox)
    assert mailbox.name == "gmail"


def test_create_mailbox_uses_explicit_fallbacks(monkeypatch):
    monkeypatch.setattr(
        "infrastructure.provider_definitions_repository.ProviderDefinitionsRepository",
        FakeDefinitionsRepository,
    )
    monkeypatch.setattr(
        "infrastructure.provider_settings_repository.ProviderSettingsRepository",
        FakeSettingsRepository,
    )
    monkeypatch.setitem(
        base_mailbox.MAILBOX_FACTORY_REGISTRY,
        "gmail_api_code",
        lambda extra, proxy: FakeMailbox("gmail"),
    )
    monkeypatch.setitem(
        base_mailbox.MAILBOX_FACTORY_REGISTRY,
        "outlook_email_api",
        lambda extra, proxy: FakeMailbox("outlook"),
    )

    mailbox = create_mailbox("gmail_api_code", extra={"mail_provider_fallbacks": ["outlook_email_api"]})

    assert isinstance(mailbox, FallbackMailbox)
    assert [key for key, _provider in mailbox.providers] == ["gmail_api_code", "outlook_email_api"]
