from types import SimpleNamespace

from core.base_mailbox import MailboxAccount, create_mailbox
from core.icloud_hme import (
    ICloudHMEClient,
    ICloudHMEMailbox,
    parse_alias_list,
    parse_icloud_accounts_json,
    parse_icloud_cookie_input,
    parse_web_mail_threads,
)
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


def test_parse_icloud_cookie_input_supports_header_and_json():
    assert parse_icloud_cookie_input('X-APPLE-WEBAUTH-USER=v=1:d=123; foo="bar"') == {
        "X-APPLE-WEBAUTH-USER": "v=1:d=123",
        "foo": '"bar"',
    }
    assert parse_icloud_cookie_input('cookie：X-APPLE-WEBAUTH-USER="v=1:d=123"; foo=bar') == {
        "X-APPLE-WEBAUTH-USER": '"v=1:d=123"',
        "foo": "bar",
    }
    assert parse_icloud_cookie_input('{"a":"1","b":"2"}') == {"a": "1", "b": "2"}


def test_icloud_client_cookie_header_quotes_cookie_values():
    client = ICloudHMEClient({"X-APPLE-WEBAUTH-USER": '"v=1:d=123"', "plain": "abc"})

    assert client._cookie_header() == 'X-APPLE-WEBAUTH-USER="v=1:d=123"; plain="abc"'


def test_icloud_cookie_header_normalizes_escaped_quoted_values():
    client = ICloudHMEClient({"X-APPLE-WEBAUTH-USER": '\\"v=1:d=123\\"', "plain": "abc"})

    assert client._cookie_header() == 'X-APPLE-WEBAUTH-USER="v=1:d=123"; plain="abc"'


def test_icloud_request_quotes_refreshed_cookie_values():
    client = ICloudHMEClient({"X-APPLE-WEBAUTH-USER": '"v=1:d=123"', "X-APPLE-WEBAUTH-TOKEN": '"old"'})
    captured_headers = []

    class FakeResponse:
        def __init__(self, status_code, text, cookies=None):
            self.status_code = status_code
            self.text = text
            self.cookies = cookies or {}

    class FakeSession:
        def request(self, _method, _url, data=None, headers=None, timeout=None):
            captured_headers.append(dict(headers or {}))
            if len(captured_headers) == 1:
                return FakeResponse(200, '{"ok":true}', {"X-APPLE-WEBAUTH-TOKEN": "new"})
            return FakeResponse(200, '{"ok":true}')

    client.session = FakeSession()

    assert client._request("POST", client.setup_url() + "/validate") == {"ok": True}
    assert client._request("GET", "https://p1-maildomainws.icloud.com/v2/hme/list") == {"ok": True}
    assert captured_headers[0]["Cookie"] == 'X-APPLE-WEBAUTH-USER="v=1:d=123"; X-APPLE-WEBAUTH-TOKEN="old"'
    assert captured_headers[1]["Cookie"] == 'X-APPLE-WEBAUTH-USER="v=1:d=123"; X-APPLE-WEBAUTH-TOKEN="new"'


def test_icloud_validate_resets_session_before_hme_requests():
    client = ICloudHMEClient({"X-APPLE-WEBAUTH-USER": '"v=1:d=123"'})

    class FakeResponse:
        def __init__(self, text):
            self.status_code = 200
            self.text = text
            self.cookies = {}

    class FakeSession:
        def __init__(self, name):
            self.name = name
            self.urls = []

        def request(self, _method, url, data=None, headers=None, timeout=None):
            self.urls.append(url)
            if "validate" in url:
                return FakeResponse('{"webservices":{"premiummailsettings":{"url":"https://p48-maildomainws.icloud.com"}},"dsInfo":{"dsid":"123","appleId":"user@example.com"}}')
            return FakeResponse('{"success":true,"result":{"hmeEmails":[]}}')

    first_session = FakeSession("first")
    second_session = FakeSession("second")

    class FakeRequests:
        def Session(self):
            return second_session

    client.session = first_session
    client._requests = FakeRequests()
    client._session_kind = "requests"

    client.validate_session()
    aliases = client.list_aliases()

    assert aliases == []
    assert any("validate" in item for item in first_session.urls)
    assert all("hme/list" not in item for item in first_session.urls)
    assert any("hme/list" in item for item in second_session.urls)


def test_icloud_validate_fallbacks_to_alternate_host_on_missing_cookie():
    client = ICloudHMEClient({"X-APPLE-WEBAUTH-USER": '"v=1:d=123"'}, host="icloud.com.cn")
    captured_urls = []

    class FakeResponse:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text
            self.cookies = {}

    class FakeSession:
        def request(self, _method, url, data=None, headers=None, timeout=None):
            captured_urls.append(url)
            if "setup.icloud.com.cn" in url:
                return FakeResponse(401, '{"reason":"Missing X-APPLE-WEBAUTH-USER cookie","error":1}')
            return FakeResponse(200, '{"webservices":{"premiummailsettings":{"url":"https://p48-maildomainws.icloud.com"},"mccgateway":{"url":"https://p48-mccgateway.icloud.com"}},"dsInfo":{"dsid":"123","appleId":"user@example.com"}}')

    client.session = FakeSession()

    info = client.validate_session()

    assert info["dsid"] == "123"
    assert client.host == "icloud.com"
    assert any("setup.icloud.com.cn" in item for item in captured_urls)
    assert any("setup.icloud.com" in item for item in captured_urls)


def test_parse_icloud_accounts_json_keeps_cookies_secret_and_returns_app_password():
    accounts = parse_icloud_accounts_json({
        "accounts": [
            {
                "id": "acc_1",
                "name": "main",
                "cookies": {"a": "1"},
                "host": "icloud.com.cn",
                "app_password": "secret",
            }
        ]
    })

    public = accounts[0].public_dict()

    assert accounts[0].host == "icloud.com.cn"
    assert public["cookies_count"] == 1
    assert public["has_app_password"] is True
    assert "cookies" not in public
    assert public["app_password"] == "secret"


def test_parse_alias_list_accepts_hme_email_shapes():
    aliases = parse_alias_list({
        "result": {
            "hmeEmails": [
                {
                    "hme": "abc@icloud.com",
                    "anonymousId": "anon-1",
                    "label": "A",
                    "forwardToEmail": "receiver@icloud.com",
                    "state": "active",
                    "createTimestamp": "2026-07-12T00:00:00Z",
                },
                {
                    "metaData": {"hme": "def@icloud.com", "label": "B"},
                    "id": "anon-2",
                    "state": "inactive",
                },
            ]
        }
    })

    assert aliases[0]["email"] == "abc@icloud.com"
    assert aliases[0]["anonymous_id"] == "anon-1"
    assert aliases[0]["forward_to_email"] == "receiver@icloud.com"
    assert aliases[0]["active"] is True
    assert aliases[1]["email"] == "def@icloud.com"
    assert aliases[1]["active"] is False


def test_icloud_web_find_by_alias_only_returns_locally_matched_messages(monkeypatch):
    client = ICloudHMEClient({"a": "1"})
    calls = []

    def fake_search(query, limit=30):
        calls.append((query, limit))
        return [
            {
                "id": "old-thread",
                "from": "ChatGPT <otp@example.test>",
                "to": "other@icloud.com",
                "subject": "OpenAI verification code",
                "body": "Your code is 111111",
            },
            {
                "id": "alias-thread",
                "from": "noreply@example.test",
                "to": "alias@icloud.com",
                "subject": "OpenAI verification code",
                "body": "Your code is 222222",
            },
        ][:limit]

    monkeypatch.setattr(client, "web_search_messages", fake_search)

    messages = client.web_find_by_alias("alias@icloud.com", 10)

    assert [item["id"] for item in messages] == ["alias-thread"]
    assert messages[0]["_alias_search"] == "1"
    assert calls == [("", 50)]


def test_parse_web_mail_threads_accepts_thread_search_response():
    messages = parse_web_mail_threads({
        "threadList": [
            {
                "threadId": "thread-1",
                "senders": ["noreply@example.test"],
                "recipients": [{"email": "alias@icloud.com"}],
                "subject": "OpenAI verification code",
                "preview": "Your code is 123456",
                "timestamp": 1780000000000,
            }
        ]
    })

    assert messages == [
        {
            "id": "thread-1",
            "from": "noreply@example.test",
            "to": "alias@icloud.com",
            "subject": "OpenAI verification code",
            "body": "Your code is 123456",
            "date": "1780000000000",
            "_source": "icloud_web",
        }
    ]


def test_icloud_hme_wait_for_code_uses_web_mail_fallback(monkeypatch):
    mailbox = ICloudHMEMailbox(
        accounts_json={
            "accounts": [
                {
                    "id": "acc_1",
                    "name": "main",
                    "cookies": {"a": "1"},
                    "host": "icloud.com",
                }
            ]
        },
        poll_interval="1",
    )
    account = MailboxAccount(
        email="alias@icloud.com",
        extra={
            "provider_resource": {
                "metadata": {
                    "account_id": "acc_1",
                    "alias_email": "alias@icloud.com",
                }
            }
        },
    )

    monkeypatch.setattr(mailbox, "_recent_imap_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        mailbox,
        "_recent_web_messages",
        lambda *_args, **_kwargs: [
            {
                "id": "thread-1",
                "from": "noreply@example.test",
                "subject": "OpenAI verification code",
                "body": "Your code is 654321",
                "_alias_search": "1",
            }
        ],
    )

    assert mailbox.get_current_ids(account) == {"thread-1"}
    assert mailbox.wait_for_code(account, keyword="OpenAI", timeout=1) == "654321"


def test_icloud_hme_web_mail_alias_result_overrides_empty_imap_shells(monkeypatch):
    mailbox = ICloudHMEMailbox(
        accounts_json={
            "accounts": [
                {
                    "id": "acc_1",
                    "name": "main",
                    "icloud_email": "main@icloud.com",
                    "app_password": "secret",
                    "cookies": {"a": "1"},
                    "host": "icloud.com",
                }
            ]
        },
        poll_interval="1",
    )
    account = MailboxAccount(
        email="alias@icloud.com",
        extra={
            "provider_resource": {
                "metadata": {
                    "account_id": "acc_1",
                    "alias_email": "alias@icloud.com",
                }
            }
        },
    )

    monkeypatch.setattr(
        mailbox,
        "_recent_imap_messages",
        lambda *_args, **_kwargs: [
            {"id": "1", "from": "", "to": "", "subject": "", "body": ""},
            {"id": "2", "from": "", "to": "", "subject": "", "body": ""},
            {"id": "3", "from": "", "to": "", "subject": "", "body": ""},
        ],
    )
    monkeypatch.setattr(
        mailbox,
        "_recent_web_messages",
        lambda *_args, **_kwargs: [
            {
                "id": "thread-code",
                "from": "noreply@example.test",
                "to": "alias@icloud.com",
                "subject": "Your temporary ChatGPT verification code",
                "body": "Your code is 804637",
                "_alias_search": "1",
            }
        ],
    )

    assert mailbox.get_current_ids(account) == {"thread-code"}
    assert mailbox.wait_for_code(account, keyword="ChatGPT", timeout=1) == "804637"


def test_icloud_hme_get_email_rejects_mismatched_forward_target(monkeypatch):
    mailbox = ICloudHMEMailbox(
        accounts_json={
            "accounts": [
                {
                    "id": "acc_1",
                    "name": "main",
                    "icloud_email": "main@icloud.com",
                    "cookies": {"X-APPLE-WEBAUTH-USER": "v=1:d=123"},
                    "host": "icloud.com",
                }
            ]
        }
    )

    class FakeClient:
        def create_alias(self, _label):
            raise AssertionError("forward target mismatch should fail before creating aliases")

        def list_aliases(self):
            return [
                {
                    "email": "old@icloud.com",
                    "anonymous_id": "anon_1",
                    "forward_to_email": "forward@example.com",
                }
            ]

    monkeypatch.setattr(mailbox, "_client", lambda _account: FakeClient())

    try:
        mailbox.get_email()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected forward target mismatch")

    assert "forward@example.com" in message
    assert "main@icloud.com" in message


def test_icloud_hme_get_email_creates_alias_and_receives_code(monkeypatch):
    mailbox = ICloudHMEMailbox(
        accounts_json={
            "accounts": [
                {
                    "id": "acc_1",
                    "name": "main",
                    "icloud_email": "main@icloud.com",
                    "cookies": {"X-APPLE-WEBAUTH-USER": "v=1:d=123"},
                    "host": "icloud.com",
                }
            ]
        },
        label_prefix="Register",
        poll_interval="1",
    )

    class FakeClient:
        aliases: list[str] = []

        def create_alias(self, label):
            self.aliases.append(label)
            return {"email": "alias@icloud.com"}

        def list_aliases(self):
            return [
                {
                    "email": "alias@icloud.com",
                    "anonymous_id": "anon_1",
                    "forward_to_email": "main@icloud.com",
                }
            ]

        def web_find_by_alias(self, alias, limit=30):
            assert alias == "alias@icloud.com"
            return [
                {
                    "id": "old-thread",
                    "from": "noreply@example.test",
                    "to": "other@icloud.com",
                    "subject": "OpenAI verification code",
                    "body": "Wrong code 111111",
                },
                {
                    "id": "new-thread",
                    "from": "noreply@example.test",
                    "to": "alias@icloud.com",
                    "subject": "OpenAI verification code",
                    "body": "Your code is 654321",
                    "_alias_search": "1",
                },
            ][:limit]

    fake_client = FakeClient()
    monkeypatch.setattr(mailbox, "_client", lambda _account: fake_client)
    monkeypatch.setattr(mailbox, "_recent_imap_messages", lambda *_args, **_kwargs: [])

    account = mailbox.get_email()
    metadata = account.extra["provider_resource"]["metadata"]

    assert account.email == "alias@icloud.com"
    assert metadata["account_id"] == "acc_1"
    assert metadata["alias_email"] == "alias@icloud.com"
    assert metadata["anonymous_id"] == "anon_1"
    assert metadata["forward_to_email"] == "main@icloud.com"
    assert fake_client.aliases[0].startswith("Register ")
    assert mailbox.wait_for_code(account, keyword="OpenAI", timeout=1, before_ids={"old-thread"}) == "654321"


def test_icloud_hme_reads_web_mail_when_alias_is_hidden(monkeypatch):
    mailbox = ICloudHMEMailbox(
        accounts_json={
            "accounts": [
                {
                    "id": "acc_1",
                    "name": "main",
                    "icloud_email": "main@icloud.com",
                    "cookies": {"X-APPLE-WEBAUTH-USER": "v=1:d=123"},
                    "host": "icloud.com",
                }
            ]
        },
        poll_interval="1",
    )
    account = MailboxAccount(
        email="alias@icloud.com",
        extra={
            "provider_resource": {
                "metadata": {
                    "account_id": "acc_1",
                    "alias_email": "alias@icloud.com",
                }
            }
        },
    )

    class FakeClient:
        def web_find_by_alias(self, alias, limit=30):
            assert alias == "alias@icloud.com"
            return []

        def web_search_messages(self, query="", limit=30):
            assert query == ""
            return [
                {
                    "id": "new-openai-thread",
                    "from": "OpenAI <noreply@example.test>",
                    "to": "",
                    "subject": "Your temporary OpenAI verification code",
                    "body": "Enter this temporary verification code to continue: 888147",
                },
                {
                    "id": "old-openai-thread",
                    "from": "OpenAI <noreply@example.test>",
                    "to": "",
                    "subject": "Your temporary OpenAI verification code",
                    "body": "Enter this temporary verification code to continue: 111111",
                },
            ][:limit]

    monkeypatch.setattr(mailbox, "_client", lambda _account: FakeClient())
    monkeypatch.setattr(mailbox, "_recent_imap_messages", lambda *_args, **_kwargs: [])

    assert mailbox.wait_for_code(
        account,
        keyword="OpenAI",
        timeout=1,
        before_ids={"old-openai-thread"},
    ) == "888147"


def test_build_platform_instance_uses_icloud_hme_mailbox(monkeypatch):
    import application.tasks as tasks_module
    import core.base_mailbox as base_mailbox

    fake_mailbox = object()
    captured = {}

    def fake_create_mailbox(provider, extra=None, proxy=None):
        captured["provider"] = provider
        captured["extra"] = dict(extra or {})
        captured["proxy"] = proxy
        return fake_mailbox

    class FakePlatform:
        def __init__(self, config, mailbox):
            self.config = config
            self.mailbox = mailbox

        def set_logger(self, _logger):
            return None

    class FakeLogger:
        def log(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(base_mailbox, "create_mailbox", fake_create_mailbox)
    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: FakePlatform)

    platform = tasks_module._build_platform_instance(
        "chatgpt",
        {
            "executor_type": "protocol",
            "captcha_solver": "auto",
            "extra": {
                "identity_provider": "mailbox",
                "mail_provider": "icloud_hme",
            },
        },
        FakeLogger(),
        resolved_proxy="http://127.0.0.1:7890",
    )

    assert platform.mailbox is fake_mailbox
    assert captured["provider"] == "icloud_hme"
    assert captured["extra"]["mail_provider"] == "icloud_hme"
    assert captured["proxy"] == "http://127.0.0.1:7890"


def test_build_platform_instance_does_not_wrap_icloud_hme_with_email_alias(monkeypatch):
    import application.tasks as tasks_module
    import core.base_mailbox as base_mailbox
    from core.email_alias_mailbox import EmailAliasMailbox

    fake_mailbox = object()

    class FakePlatform:
        def __init__(self, config, mailbox):
            self.config = config
            self.mailbox = mailbox

        def set_logger(self, _logger):
            return None

    class FakeLogger:
        def log(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(base_mailbox, "create_mailbox", lambda *_args, **_kwargs: fake_mailbox)
    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: FakePlatform)

    platform = tasks_module._build_platform_instance(
        "chatgpt",
        {
            "executor_type": "protocol",
            "extra": {
                "identity_provider": "mailbox",
                "mail_provider": "icloud_hme",
                "enable_email_alias": True,
                "email_alias_limit": 6,
            },
        },
        FakeLogger(),
    )

    assert platform.mailbox is fake_mailbox
    assert not isinstance(platform.mailbox, EmailAliasMailbox)


def test_create_mailbox_wires_icloud_hme_runtime_settings(monkeypatch):
    accounts_json = '{"accounts":[{"id":"acc_1","cookies":{"a":"1"},"host":"icloud.com"}]}'

    class FakeDefinitionsRepository:
        def get_by_key(self, provider_type, provider_key):
            assert provider_type == "mailbox"
            return SimpleNamespace(
                enabled=True,
                driver_type=provider_key,
                get_metadata=lambda: {},
            )

    class FakeSettingsRepository:
        def resolve_runtime_settings(self, provider_type, provider_key, overrides=None):
            assert provider_type == "mailbox"
            assert provider_key == "icloud_hme"
            return {
                "icloud_hme_accounts_json": accounts_json,
                "icloud_hme_label_prefix": "Register",
                "icloud_hme_poll_interval": "2",
            }

    monkeypatch.setattr(
        "infrastructure.provider_definitions_repository.ProviderDefinitionsRepository",
        FakeDefinitionsRepository,
    )
    monkeypatch.setattr(
        "infrastructure.provider_settings_repository.ProviderSettingsRepository",
        FakeSettingsRepository,
    )

    mailbox = create_mailbox("icloud_hme")

    assert isinstance(mailbox, ICloudHMEMailbox)
    assert mailbox.accounts[0].id == "acc_1"
    assert mailbox.label_prefix == "Register"
    assert mailbox.poll_interval == 2


def test_icloud_hme_provider_definition_is_after_gmail_api_code():
    repository = ProviderDefinitionsRepository()
    repository.ensure_seeded()
    definitions = repository.list_by_type("mailbox", enabled_only=False)
    keys = [item.provider_key for item in definitions]

    assert "gmail_api_code" in keys
    assert "icloud_hme" in keys
    assert keys.index("icloud_hme") > keys.index("gmail_api_code")


def test_icloud_hme_account_crud_api(client):
    create_resp = client.post(
        "/api/provider-settings/icloud-hme/accounts",
        json={
            "name": "CN account",
            "host": "icloud.com.cn",
            "cookie_header": "",
            "validate": False,
        },
    )
    assert create_resp.status_code == 200
    payload = create_resp.json()
    account_id = payload["account"]["id"]
    assert payload["account"]["name"] == "CN account"

    list_resp = client.get("/api/provider-settings/icloud-hme/accounts")
    assert list_resp.status_code == 200
    assert list_resp.json()["accounts"][0]["id"] == account_id

    delete_resp = client.delete(f"/api/provider-settings/icloud-hme/accounts/{account_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json()["ok"] is True

    list_resp = client.get("/api/provider-settings/icloud-hme/accounts")
    assert list_resp.json()["accounts"] == []


def test_icloud_hme_validate_endpoint_uses_request_cookie(client, monkeypatch):
    from application import icloud_hme as app_icloud_hme

    captured = {}

    def fake_validate(self, account):
        captured["cookies"] = dict(account.cookies)
        captured["host"] = account.host
        account.status = "active"
        account.last_error = ""
        account.alias_total = 0
        account.alias_active = 0

    monkeypatch.setattr(app_icloud_hme.ICloudHMEService, "_validate_account", fake_validate)

    create_resp = client.post(
        "/api/provider-settings/icloud-hme/accounts",
        json={
            "name": "CN account",
            "host": "icloud.com.cn",
            "cookie_header": "",
            "validate": False,
        },
    )
    assert create_resp.status_code == 200
    account_id = create_resp.json()["account"]["id"]

    validate_resp = client.post(
        f"/api/provider-settings/icloud-hme/accounts/{account_id}/validate",
        json={
            "host": "icloud.com",
            "cookie_header": 'X-APPLE-WEBAUTH-USER="v=1:d=123"; foo=bar',
        },
    )

    assert validate_resp.status_code == 200
    assert validate_resp.json()["ok"] is True
    assert captured["cookies"] == {
        "X-APPLE-WEBAUTH-USER": '"v=1:d=123"',
        "foo": "bar",
    }
    assert captured["host"] == "icloud.com"


def test_icloud_hme_upsert_reports_validation_failure(client, monkeypatch):
    from application import icloud_hme as app_icloud_hme

    def fake_validate(self, account):
        account.status = "error"
        account.last_error = "HTTP 401: Missing X-APPLE-WEBAUTH-USER cookie"

    monkeypatch.setattr(app_icloud_hme.ICloudHMEService, "_validate_account", fake_validate)

    create_resp = client.post(
        "/api/provider-settings/icloud-hme/accounts",
        json={
            "name": "Bad cookie",
            "host": "icloud.com",
            "cookie_header": "X-APPLE-WEBAUTH-USER=bad",
            "validate": True,
        },
    )

    assert create_resp.status_code == 200
    payload = create_resp.json()
    assert payload["ok"] is False
    assert payload["error"] == "HTTP 401: Missing X-APPLE-WEBAUTH-USER cookie"
    assert payload["account"]["status"] == "error"
    assert payload["account"]["last_error"] == "HTTP 401: Missing X-APPLE-WEBAUTH-USER cookie"

    list_resp = client.get("/api/provider-settings/icloud-hme/accounts")
    assert list_resp.status_code == 200
    assert list_resp.json()["accounts"][0]["last_error"] == "HTTP 401: Missing X-APPLE-WEBAUTH-USER cookie"
