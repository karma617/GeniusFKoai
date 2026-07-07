from __future__ import annotations

import pytest

from core.email_alias_mailbox import EmailAliasMailbox
from core.gmail_api_code_mailbox import GmailApiCodeMailbox, parse_gmail_api_code_entries


def setup_function():
    GmailApiCodeMailbox._ACTIVE_CLAIMS.clear()
    GmailApiCodeMailbox._INVALID_EMAILS.clear()


def test_parse_gmail_api_code_entries_splits_email_and_url():
    entries = parse_gmail_api_code_entries(
        "phkong8269@gmail.com----https://gapi.mailsapi.com/api/code/fetch?token=abc&uid=def\n"
        "bad row\n"
        "other@gmail.com----http://example.test/fetch"
    )

    assert [entry.email for entry in entries] == ["phkong8269@gmail.com", "other@gmail.com"]
    assert entries[0].code_url == "https://gapi.mailsapi.com/api/code/fetch?token=abc&uid=def"


def test_parse_gmail_api_code_entries_skips_deleted_rows():
    entries = parse_gmail_api_code_entries(
        "# deleted first@gmail.com----https://example.test/first\n"
        "second@gmail.com----https://example.test/second"
    )

    assert [entry.email for entry in entries] == ["second@gmail.com"]


def test_gmail_api_code_get_email_claims_fixed_gmail():
    mailbox = GmailApiCodeMailbox(
        pool_text=(
            "first@gmail.com----https://example.test/first\n"
            "second@gmail.com----https://example.test/second"
        )
    )

    first = mailbox.get_email()
    second = mailbox.get_email()

    assert {first.email, second.email} == {"first@gmail.com", "second@gmail.com"}
    assert first.extra["provider_resource"]["provider_name"] == "gmail_api_code"
    assert first.extra["provider_resource"]["metadata"]["code_url"].startswith("https://example.test/")


def test_gmail_api_code_alias_success_releases_parent_for_next_alias():
    mailbox = GmailApiCodeMailbox(pool_text="first@gmail.com----https://example.test/first")
    wrapper = EmailAliasMailbox(mailbox, alias_limit=5, platform="chatgpt")

    first = wrapper.get_email()
    wrapper.mark_registration_success(first)
    second = wrapper.get_email()

    assert first.email != second.email
    assert first.extra["email_alias"]["parent_email"] == "first@gmail.com"
    assert second.extra["email_alias"]["parent_email"] == "first@gmail.com"


def test_gmail_api_code_invalid_parent_is_skipped_for_next_email():
    mailbox = GmailApiCodeMailbox(
        pool_text=(
            "first@gmail.com----https://example.test/first\n"
            "second@gmail.com----https://example.test/second"
        )
    )
    wrapper = EmailAliasMailbox(mailbox, alias_limit=5, platform="chatgpt")

    first = wrapper.get_email()
    tags = wrapper.mark_invalid_email(first, reason="invalid_email_no_otp")
    second = wrapper.get_email()

    assert tags == ["主号邮箱 first@gmail.com 已标记无效: Gmail API接码邮箱已标记无效"]
    assert first.extra["email_alias"]["parent_email"] == "first@gmail.com"
    assert second.extra["email_alias"]["parent_email"] == "second@gmail.com"


def test_gmail_api_code_invalid_only_parent_is_not_temporary_pool_empty():
    mailbox = GmailApiCodeMailbox(pool_text="first@gmail.com----https://example.test/first")
    account = mailbox.get_email()

    assert mailbox.mark_invalid_email(account, reason="invalid_email_no_otp") == ["Gmail API接码邮箱已标记无效"]
    with pytest.raises(RuntimeError, match="已无可用邮箱"):
        mailbox.get_email()


def test_gmail_api_code_wait_for_code_skips_before_id(monkeypatch):
    calls = {"count": 0}

    class Response:
        def __init__(self, text: str):
            self.text = text

        def raise_for_status(self):
            return None

    def fake_get(*_args, **_kwargs):
        calls["count"] += 1
        code = "111111" if calls["count"] == 1 else "222222"
        return Response(f'{{"code":"{code}"}}')

    monkeypatch.setattr("core.gmail_api_code_mailbox.requests.get", fake_get)

    mailbox = GmailApiCodeMailbox(
        pool_text="user@gmail.com----https://example.test/fetch",
        poll_interval="1",
    )
    account = mailbox.get_email()
    before_ids = mailbox.get_current_ids(account)

    assert before_ids == {"code:111111"}
    assert mailbox.wait_for_code(account, timeout=3, before_ids=before_ids) == "222222"


def test_gmail_api_code_status_602_keeps_polling(monkeypatch):
    calls = {"count": 0}

    class Response:
        status_code = 200

        def __init__(self, text: str):
            self.text = text

        def raise_for_status(self):
            return None

    def fake_get(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return Response('{"status":602,"message":"未收到验证码"}')
        return Response('{"status":200,"code":"333444"}')

    monkeypatch.setattr("core.gmail_api_code_mailbox.requests.get", fake_get)
    monkeypatch.setattr("core.gmail_api_code_mailbox.time.sleep", lambda _seconds: None)

    mailbox = GmailApiCodeMailbox(
        pool_text="user@gmail.com----https://example.test/fetch",
        poll_interval="1",
    )
    account = mailbox.get_email()

    assert mailbox.wait_for_code(account, timeout=3) == "333444"


def test_gmail_api_code_status_502_marks_email_invalid(monkeypatch):
    class Response:
        status_code = 200
        text = '{"status":502,"message":"邮箱已下架"}'

        def raise_for_status(self):
            return None

    monkeypatch.setattr("core.gmail_api_code_mailbox.requests.get", lambda *_args, **_kwargs: Response())

    mailbox = GmailApiCodeMailbox(
        pool_text=(
            "first@gmail.com----https://example.test/first\n"
            "second@gmail.com----https://example.test/second"
        )
    )
    account = mailbox.get_email()

    with pytest.raises(RuntimeError, match="不可用|下架"):
        mailbox.wait_for_code(account, timeout=3)

    assert mailbox.get_email().email == "second@gmail.com"
