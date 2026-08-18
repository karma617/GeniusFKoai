from __future__ import annotations

import base64
import json

import pytest
from sqlmodel import Session

from core.db import AccountModel, ProviderResourceModel, ProviderSettingModel, engine
from core.email_alias_mailbox import EmailAliasMailbox
from core.gmail_api_code_mailbox import (
    GmailApiCodeMailbox,
    parse_gmail_api_code_entries,
    parse_gmail_api_code_pool_rows,
)


def setup_function():
    GmailApiCodeMailbox._ACTIVE_CLAIMS.clear()
    GmailApiCodeMailbox._INVALID_EMAILS.clear()


def _save_gmail_api_code_pool(pool_text: str, *, storage: str = "config") -> None:
    with Session(engine) as session:
        setting = ProviderSettingModel(
            provider_type="mailbox",
            provider_key="gmail_api_code",
            display_name="API接码邮箱",
            enabled=True,
            is_default=True,
        )
        if storage == "auth":
            setting.set_config({})
            setting.set_auth({"gmail_api_code_pool_text": pool_text})
        else:
            setting.set_config({"gmail_api_code_pool_text": pool_text})
            setting.set_auth({})
        setting.set_metadata({})
        session.add(setting)
        session.commit()


def test_parse_gmail_api_code_entries_splits_email_and_url():
    entries = parse_gmail_api_code_entries(
        "phkong8269@gmail.com----https://gapi.mailsapi.com/api/code/fetch?token=abc&uid=def\n"
        "bad row\n"
        "other@gmail.com----http://example.test/fetch\n"
        "user@icloud.com----http://example.test/icloud"
    )

    assert [entry.email for entry in entries] == ["phkong8269@gmail.com", "other@gmail.com", "user@icloud.com"]
    assert entries[0].code_url == "https://gapi.mailsapi.com/api/code/fetch?token=abc&uid=def"


def test_parse_gmail_api_code_entries_skips_deleted_rows():
    entries = parse_gmail_api_code_entries(
        "# deleted first@gmail.com----https://example.test/first\n"
        "second@gmail.com----https://example.test/second"
    )

    assert [entry.email for entry in entries] == ["second@gmail.com"]


def test_parse_gmail_api_code_pool_rows_keeps_status_markers_for_display():
    rows = parse_gmail_api_code_pool_rows(
        "# registered_exhausted exhausted@icloud.com----https://example.test/exhausted\n"
        "# registered used@gmail.com----https://example.test/used\n"
        "# invalid dead@gmail.com----https://example.test/dead\n"
        "# deleted old@gmail.com----https://example.test/old\n"
        "active@gmail.com----https://example.test/active"
    )

    assert [(row.email, row.status) for row in rows] == [
        ("exhausted@icloud.com", "registered_exhausted"),
        ("used@gmail.com", "registered"),
        ("dead@gmail.com", "invalid"),
        ("old@gmail.com", "deleted"),
        ("active@gmail.com", "active"),
    ]
    assert [entry.email for entry in parse_gmail_api_code_entries(
        "\n".join(f"{'# ' + row.status + ' ' if row.status != 'active' else ''}{row.email}----{row.code_url}" for row in rows)
    )] == ["active@gmail.com"]


def test_gmail_api_code_entries_skip_registered_exhausted_rows():
    entries = parse_gmail_api_code_entries(
        "# registered_exhausted used@icloud.com----https://example.test/used\n"
        "available@icloud.com----https://example.test/available"
    )

    assert [entry.email for entry in entries] == ["available@icloud.com"]


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


def test_gmail_api_code_release_account_releases_claim_without_changing_status():
    mailbox = GmailApiCodeMailbox(
        pool_text="first@gmail.com----https://example.test/first"
    )

    account = mailbox.get_email()
    assert "first@gmail.com" in GmailApiCodeMailbox._ACTIVE_CLAIMS

    assert mailbox.release_account(account) is True
    assert "first@gmail.com" not in GmailApiCodeMailbox._ACTIVE_CLAIMS
    assert mailbox.get_email().email == "first@gmail.com"


def test_gmail_api_code_alias_wrapper_uses_gmail_without_child_alias():
    mailbox = GmailApiCodeMailbox(pool_text="first@gmail.com----https://example.test/first")
    wrapper = EmailAliasMailbox(mailbox, alias_limit=5, platform="chatgpt")

    first = wrapper.get_email()

    assert first.email == "first@gmail.com"
    assert "email_alias" not in first.extra


def test_gmail_api_code_alias_wrapper_allows_one_icloud_child_alias():
    _save_gmail_api_code_pool("first@icloud.com----https://example.test/first")
    mailbox = GmailApiCodeMailbox(pool_text="first@icloud.com----https://example.test/first")
    wrapper = EmailAliasMailbox(mailbox, alias_limit=5, platform="chatgpt")

    first = wrapper.get_email()
    tags = wrapper.mark_registration_success(first)
    second = wrapper.get_email()

    assert first.email == "first@icloud.com"
    assert tags == []
    assert second.email.startswith("first+")
    assert second.email.endswith("@icloud.com")
    assert second.extra["email_alias"]["parent_email"] == "first@icloud.com"
    assert second.extra["email_alias"]["limit"] == 1
    assert wrapper.mark_registration_success(second) == ["API接码邮箱已注册"]
    with pytest.raises(RuntimeError, match="Email alias quota exhausted"):
        wrapper.get_email()


def test_gmail_api_code_alias_wrapper_uses_child_for_registered_icloud_parent():
    _save_gmail_api_code_pool("first@icloud.com----https://example.test/first")
    mailbox = GmailApiCodeMailbox(pool_text="first@icloud.com----https://example.test/first")
    parent = mailbox.get_email()
    assert mailbox.mark_registration_success(parent) == ["API接码邮箱已注册"]

    next_mailbox = GmailApiCodeMailbox(
        pool_text="# registered first@icloud.com----https://example.test/first"
    )
    wrapper = EmailAliasMailbox(next_mailbox, alias_limit=5, platform="chatgpt")

    child = wrapper.get_email()

    assert child.email.startswith("first+")
    assert child.email.endswith("@icloud.com")
    assert child.extra["email_alias"]["parent_email"] == "first@icloud.com"
    wrapper.release_account(child)


def test_gmail_api_code_alias_exhausted_parent_is_persisted_and_skipped():
    _save_gmail_api_code_pool(
        "first@icloud.com----https://example.test/first\n"
        "second@icloud.com----https://example.test/second"
    )
    mailbox = GmailApiCodeMailbox(
        pool_text=(
            "first@icloud.com----https://example.test/first\n"
            "second@icloud.com----https://example.test/second"
        )
    )
    account = mailbox.get_email()

    assert mailbox.mark_alias_exhausted(account, reason="registration_disallowed") == ["API接码邮箱已注册"]

    with Session(engine) as session:
        setting = session.get(ProviderSettingModel, 1)
        pool_text = setting.get_config()["gmail_api_code_pool_text"]
    assert "# registered_exhausted first@icloud.com----https://example.test/first" in pool_text

    next_mailbox = GmailApiCodeMailbox(pool_text=pool_text)
    wrapper = EmailAliasMailbox(next_mailbox, alias_limit=5, platform="chatgpt")
    next_account = wrapper.get_email()
    assert next_account.email == "second@icloud.com"


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

    assert tags == ["API接码邮箱已标记无效"]
    assert "email_alias" not in first.extra
    assert second.email == "second@gmail.com"


def test_gmail_api_code_invalid_only_parent_is_not_temporary_pool_empty():
    mailbox = GmailApiCodeMailbox(pool_text="first@gmail.com----https://example.test/first")
    account = mailbox.get_email()

    assert mailbox.mark_invalid_email(account, reason="invalid_email_no_otp") == ["API接码邮箱已标记无效"]
    with pytest.raises(RuntimeError, match="已无可用邮箱"):
        mailbox.get_email()


def test_gmail_api_code_mark_success_updates_pool_text_registered():
    _save_gmail_api_code_pool(
        "first@gmail.com----https://example.test/first\n"
        "second@gmail.com----https://example.test/second"
    )
    mailbox = GmailApiCodeMailbox(pool_text="first@gmail.com----https://example.test/first")
    account = mailbox.get_email()

    assert mailbox.mark_registration_success(account) == ["API接码邮箱已注册"]

    with Session(engine) as session:
        setting = session.get(ProviderSettingModel, 1)
        pool_text = setting.get_config()["gmail_api_code_pool_text"]
    assert "# registered first@gmail.com----https://example.test/first" in pool_text
    assert [entry.email for entry in parse_gmail_api_code_entries(pool_text)] == ["second@gmail.com"]


def test_gmail_api_code_mark_invalid_updates_pool_text_unusable():
    _save_gmail_api_code_pool(
        "first@gmail.com----https://example.test/first\n"
        "second@gmail.com----https://example.test/second"
    )
    mailbox = GmailApiCodeMailbox(pool_text="first@gmail.com----https://example.test/first")
    account = mailbox.get_email()

    assert mailbox.mark_invalid_email(account, reason="gmail_api_code_502") == ["API接码邮箱已标记无效"]

    with Session(engine) as session:
        setting = session.get(ProviderSettingModel, 1)
        pool_text = setting.get_config()["gmail_api_code_pool_text"]
    assert "# invalid first@gmail.com----https://example.test/first" in pool_text
    assert [entry.email for entry in parse_gmail_api_code_entries(pool_text)] == ["second@gmail.com"]


def test_gmail_api_code_mark_invalid_updates_auth_pool_text_unusable():
    _save_gmail_api_code_pool(
        "first@gmail.com----https://example.test/first\n"
        "second@gmail.com----https://example.test/second",
        storage="auth",
    )
    mailbox = GmailApiCodeMailbox(pool_text="first@gmail.com----https://example.test/first")
    account = mailbox.get_email()

    assert mailbox.mark_invalid_email(account, reason="invalid_email_no_otp") == ["API接码邮箱已标记无效"]

    with Session(engine) as session:
        setting = session.get(ProviderSettingModel, 1)
        config = setting.get_config()
        pool_text = setting.get_auth()["gmail_api_code_pool_text"]
    assert "gmail_api_code_pool_text" not in config
    assert "# invalid first@gmail.com----https://example.test/first" in pool_text
    assert [entry.email for entry in parse_gmail_api_code_entries(pool_text)] == ["second@gmail.com"]


def test_gmail_api_code_mark_success_updates_auth_pool_text_registered():
    _save_gmail_api_code_pool(
        "first@gmail.com----https://example.test/first\n"
        "second@gmail.com----https://example.test/second",
        storage="auth",
    )
    mailbox = GmailApiCodeMailbox(pool_text="first@gmail.com----https://example.test/first")
    account = mailbox.get_email()

    assert mailbox.mark_registration_success(account) == ["API接码邮箱已注册"]

    with Session(engine) as session:
        setting = session.get(ProviderSettingModel, 1)
        config = setting.get_config()
        pool_text = setting.get_auth()["gmail_api_code_pool_text"]
    assert "gmail_api_code_pool_text" not in config
    assert "# registered first@gmail.com----https://example.test/first" in pool_text
    assert [entry.email for entry in parse_gmail_api_code_entries(pool_text)] == ["second@gmail.com"]


def test_gmail_api_code_parent_status_matches_alias_provider_resource():
    with Session(engine) as session:
        account = AccountModel(
            platform="chatgpt",
            email="first+alias@gmail.com",
            password="Secret123!",
            user_id="first+alias@gmail.com",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        resource = ProviderResourceModel(
            account_id=account.id or 0,
            provider_type="mailbox",
            provider_name="gmail_api_code",
            resource_type="mailbox",
            resource_identifier="first@gmail.com",
            handle="first+alias@gmail.com",
            display_name="first+alias@gmail.com",
        )
        resource.set_metadata(
            {
                "email": "first@gmail.com",
                "alias_email": "first+alias@gmail.com",
                "alias_parent_email": "first@gmail.com",
                "registration_status": "invalid",
            }
        )
        session.add(resource)
        session.commit()

    mailbox = GmailApiCodeMailbox(
        pool_text=(
            "first@gmail.com----https://example.test/first\n"
            "second@gmail.com----https://example.test/second"
        )
    )

    assert mailbox.get_email().email == "second@gmail.com"


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


def test_gmail_api_code_wait_for_code_allows_same_code_on_new_message_id(monkeypatch):
    calls = {"count": 0}

    class Response:
        def __init__(self, *, url: str, text: str, content_type: str = "text/html; charset=utf-8"):
            self.status_code = 200
            self.url = url
            self.text = text
            self.headers = {"content-type": content_type}

        def raise_for_status(self):
            return None

    def _list_page(message_id: str) -> Response:
        return Response(
            url=f"https://example.test/messages/{message_id}/user@gmail.com",
            text=(
                f'<a class="item active" href="#mail-{message_id}" data-id="{message_id}">'
                '<div class="subject">Your temporary ChatGPT verification code</div>'
                '<div class="time">2026-08-03 10:53:16</div>'
                "</a>"
                '<script>var detailBase="/message/"; var detailSuffix="/user@gmail.com";</script>'
                '<article class="card mail" id="mail-view">'
                '<div class="placeholder">请选择一封邮件</div>'
                "</article>"
            ),
        )

    def _detail_response(url: str, message_id: str) -> Response:
        selected_mail = """
        <html><body>
          <p>Enter this temporary verification code to continue:</p>
          <p>899728</p>
        </body></html>
        """
        data_uri = "data:text/html;charset=utf-8;base64," + base64.b64encode(selected_mail.encode()).decode()
        return Response(
            url=url,
            text=json.dumps({"body": data_uri, "subject": "Your temporary ChatGPT verification code", "message_id": message_id}),
        )

    def fake_get(url, *_args, **_kwargs):
        calls["count"] += 1
        if url.endswith("/fetch"):
            return _list_page("1434454" if calls["count"] == 1 else "1435003")
        if "/message/1434454/" in url:
            return _detail_response(url, "1434454")
        if "/message/1435003/" in url:
            return _detail_response(url, "1435003")
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("core.gmail_api_code_mailbox.requests.get", fake_get)

    mailbox = GmailApiCodeMailbox(
        pool_text="user@gmail.com----https://example.test/fetch",
        poll_interval="1",
    )
    account = mailbox.get_email()
    before_ids = mailbox.get_current_ids(account)

    assert any(item.startswith("mail:1434454|code:899728") for item in before_ids)
    assert mailbox.wait_for_code(account, timeout=1, before_ids=before_ids) == "899728"


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


def test_gmail_api_code_extracts_code_from_json_data_html_body(monkeypatch):
    html_body = """
    <html><body>
      <div>2026-07-29 12:38:09</div>
      <p>Enter this temporary verification code to continue:</p>
      <p>108448</p>
    </body></html>
    """
    data_uri = "data:text/html;charset=utf-8;base64," + base64.b64encode(html_body.encode()).decode()

    class Response:
        status_code = 200
        text = json.dumps({"body": data_uri, "subject": "Your temporary ChatGPT verification code"})

        def raise_for_status(self):
            return None

    monkeypatch.setattr("core.gmail_api_code_mailbox.requests.get", lambda *_args, **_kwargs: Response())

    mailbox = GmailApiCodeMailbox(
        pool_text="user@gmail.com----https://example.test/fetch",
        poll_interval="1",
    )
    account = mailbox.get_email()

    assert mailbox.wait_for_code(account, timeout=1) == "108448"


def test_gmail_api_code_extracts_selected_code_from_html_page_data_body(monkeypatch):
    selected_mail = """
    <html><body>
      <p>Enter this temporary verification code to continue:</p>
      <p>654321</p>
    </body></html>
    """
    data_uri = "data:text/html;charset=utf-8;base64," + base64.b64encode(selected_mail.encode()).decode()

    class Response:
        status_code = 200
        text = (
            "<html><body>"
            "<aside>2026-07-29 12:38:09 Your temporary ChatGPT verificati...</aside>"
            "<aside>2026-07-29 12:38:05 Your temporary ChatGPT verificati...</aside>"
            f'<iframe src="{data_uri}"></iframe>'
            "</body></html>"
        )

        def raise_for_status(self):
            return None

    monkeypatch.setattr("core.gmail_api_code_mailbox.requests.get", lambda *_args, **_kwargs: Response())

    mailbox = GmailApiCodeMailbox(
        pool_text="user@gmail.com----https://example.test/fetch",
        poll_interval="1",
    )
    account = mailbox.get_email()

    assert mailbox.wait_for_code(account, timeout=1) == "654321"


def test_gmail_api_code_decodes_mail_view_iframe_src_before_extracting_id():
    selected_mail = """
    <!doctype html><html><body>
      <table class="main"><tbody><tr><td>
        <p>Enter this temporary verification code to continue:</p>
        <p style="font-family: Menlo">149477</p>
      </td></tr></tbody></table>
    </body></html>
    """
    data_uri = "data:text/html;charset=utf-8;base64," + base64.b64encode(selected_mail.encode()).decode()
    html = f"""
    <a class="item active" href="#mail-221070" data-id="221070">
      <div class="subject">Your temporary ChatGPT verification code</div>
      <div class="time">2026-07-29 14:25:02</div>
    </a>
    <article class="card mail" id="mail-view">
      <iframe class="mail-frame" src="{data_uri}"></iframe>
    </article>
    """

    assert GmailApiCodeMailbox._extract_code(html) == "149477"


def test_gmail_api_code_debug_log_reports_api_body_and_decoded_code(monkeypatch):
    selected_mail = """
    <html><body>
      <table class="main"><tbody><tr><td>
        <p>Enter this temporary verification code to continue:</p>
        <p>149477</p>
      </td></tr></tbody></table>
    </body></html>
    """
    data_uri = "data:text/html;charset=utf-8;base64," + base64.b64encode(selected_mail.encode()).decode()

    class Response:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        text = (
            '<a class="item active" href="#mail-221070" data-id="221070"></a>'
            f'<article class="card mail" id="mail-view"><iframe src="{data_uri}"></iframe></article>'
        )

        def raise_for_status(self):
            return None

    monkeypatch.setattr("core.gmail_api_code_mailbox.requests.get", lambda *_args, **_kwargs: Response())

    logs: list[str] = []
    mailbox = GmailApiCodeMailbox(
        pool_text="user@gmail.com----https://example.test/message/221070/token/user@gmail.com",
        poll_interval="1",
    )
    mailbox.set_debug_logger(logs.append)
    account = mailbox.get_email()

    assert mailbox.wait_for_code(account, timeout=1) == "149477"
    joined = "\n".join(logs)
    assert "url=https://example.test/message/221070/token/user@gmail.com" in joined
    assert "data_uri=1" in joined
    assert "mail_view=yes" in joined
    assert "extracted=149477" in joined
    assert "Enter this temporary verification code to continue: 149477" in joined


def test_gmail_api_code_messages_page_loads_active_detail_json(monkeypatch):
    html_body = """
    <html><body>
      <table class="main"><tbody><tr><td>
        <p>Enter this temporary verification code to continue:</p>
        <p>247818</p>
      </td></tr></tbody></table>
    </body></html>
    """
    data_uri = "data:text/html;charset=utf-8;base64," + base64.b64encode(html_body.encode()).decode()
    list_url = "http://yangyang.website/messages/token/user@gmail.com"
    detail_url = "http://yangyang.website/message/225422/token/user@gmail.com"
    calls: list[str] = []

    class Response:
        def __init__(self, *, url: str, text: str, content_type: str = "text/html; charset=utf-8"):
            self.status_code = 200
            self.url = url
            self.text = text
            self.headers = {"content-type": content_type}

        def raise_for_status(self):
            return None

    def fake_get(url, *_args, **_kwargs):
        calls.append(url)
        if url == list_url:
            return Response(
                url=url,
                text=(
                    '<a class="item active" href="#mail-225422" data-id="225422">'
                    '<div class="subject">Your temporary ChatGPT verification code</div>'
                    '<div class="time">2026-07-29 15:09:49</div></a>'
                    "<script>var detailBase='/message/';"
                    "var detailSuffix='/token/user@gmail.com';</script>"
                    '<article class="card mail" id="mail-view"><div class="placeholder">请选择一封邮件</div></article>'
                ),
            )
        assert url == detail_url
        return Response(
            url=url,
            content_type="application/json; charset=utf-8",
            text=json.dumps({"body": data_uri, "html": True, "subject": "Your temporary ChatGPT verification code"}),
        )

    monkeypatch.setattr("core.gmail_api_code_mailbox.requests.get", fake_get)

    logs: list[str] = []
    mailbox = GmailApiCodeMailbox(pool_text=f"user@gmail.com----{list_url}", poll_interval="1")
    mailbox.set_debug_logger(logs.append)
    account = mailbox.get_email()

    assert mailbox.wait_for_code(account, timeout=1) == "247818"
    assert calls == [list_url, detail_url]
    joined = "\n".join(logs)
    assert f"detail_url={detail_url}" in joined
    assert "detail_http=200" in joined
    assert "extracted=247818" in joined


def test_gmail_api_code_prefers_main_table_code_over_message_url_id():
    html = (
        "http://yangyang.website/message/220593/token/user@icloud.com "
        '<table class="main"><tbody><tr><td>'
        "<p>Enter this temporary verification code to continue:</p>"
        '<p style="font-family: Menlo">'
        "<!--[if mso]><span><![endif]-->314863<!--[if mso]></span><![endif]-->"
        "</p>"
        '<a href="http://url3243.email.openai.com/ls/click?upn=220616">Help center</a>'
        "</td></tr></tbody></table>"
    )

    assert GmailApiCodeMailbox._extract_code(html, code_pattern=r"(?<!\d)(\d{6})(?!\d)") == "314863"


def test_gmail_api_code_ignores_active_item_href_mail_id():
    html = """
    <html><body>
      <a class="item active" href="#mail-221070" data-id="221070">
        <div class="subject">Your temporary ChatGPT verification code <span style="color:#dc2626">(垃圾邮件)</span></div>
        <div class="time">2026-07-29 13:11:48</div>
        <div class="from">noreply_at_tm_openai_com_wftw8ed36w9ve5_f0km8037@icloud.com</div>
      </a>
      <table class="main"><tbody><tr><td>
        <p>Enter this temporary verification code to continue:</p>
        <p>314863</p>
      </td></tr></tbody></table>
    </body></html>
    """

    assert GmailApiCodeMailbox._extract_code(html) == "314863"


def test_gmail_api_code_prefers_login_temporary_code_over_tracking_id_with_pattern():
    selected_mail = """
    <!doctype html><html><body>
      <div>Your temporary ChatGPT login code</div>
      <p>Log in to ChatGPT with the button below.</p>
      <a href="https://url3243.email.openai.com/ls/click?upn=202123">Log in to ChatGPT</a>
      <p>You can also enter this temporary code: 466072</p>
      <p>Didn't request a verification code? You can ignore this email.</p>
    </body></html>
    """
    data_uri = "data:text/html;charset=utf-8;base64," + base64.b64encode(selected_mail.encode()).decode()
    html = f"""
    <a class="item active" href="#mail-825390" data-id="825390">
      <div class="subject">Your temporary ChatGPT login code</div>
      <div class="time">2026-08-01 18:27:46</div>
    </a>
    <article class="card mail" id="mail-view">
      <iframe class="mail-frame" src="{data_uri}"></iframe>
    </article>
    """

    assert GmailApiCodeMailbox._extract_code(html, code_pattern=r"(?<!#)(?<!\d)(\d{6})(?!\d)") == "466072"
