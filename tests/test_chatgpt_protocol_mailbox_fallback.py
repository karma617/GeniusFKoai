from __future__ import annotations



import pytest



from platforms.chatgpt.register import RegistrationResult as ProtocolRegistrationResult





class _MailboxAccount:

    email = "new@example.com"

    account_id = "mailbox-1"





class _Mailbox:

    def __init__(self):

        self.before_ids_seen = None



    def get_current_ids(self, account):

        assert account is not None

        return {"old-message"}



    def wait_for_code(self, account, keyword="", timeout=600, code_pattern=None, before_ids=None):

        assert account is not None

        self.before_ids_seen = before_ids

        return "123456"





def test_protocol_mailbox_delivery_delay_does_not_extend_short_timeout(monkeypatch):

    import time

    import platforms.chatgpt.protocol_mailbox as protocol_mailbox

    seen = {}

    class Mailbox(_Mailbox):

        def wait_for_code(self, account, keyword="", timeout=600, code_pattern=None, before_ids=None):

            seen["timeout"] = timeout

            return "123456"

    monkeypatch.setattr(time, "time", lambda: 101.0)
    monkeypatch.setattr(time, "sleep", lambda seconds: seen.setdefault("sleep", seconds))

    service = protocol_mailbox._MailboxEmailService(

        mailbox=Mailbox(),

        mailbox_account=_MailboxAccount(),

        provider="cfworker_admin_api",

        log_fn=lambda message: None,

    )

    assert service.get_verification_code(timeout=20, otp_sent_at=100.0) == "123456"
    assert seen["sleep"] == 1.0
    assert seen["timeout"] == 19


def test_protocol_mailbox_reuses_initial_message_baseline_once():
    import platforms.chatgpt.protocol_mailbox as protocol_mailbox

    class Mailbox(_Mailbox):
        def __init__(self):
            super().__init__()
            self.current_ids_calls = 0

        def get_current_ids(self, account):
            self.current_ids_calls += 1
            return {f"message-{self.current_ids_calls}"}

    mailbox = Mailbox()
    service = protocol_mailbox._MailboxEmailService(
        mailbox=mailbox,
        mailbox_account=_MailboxAccount(),
        provider="outlook_email_api",
        log_fn=lambda message: None,
    )

    service.create_email()

    assert service.refresh_before_ids() == {"message-1"}
    assert mailbox.current_ids_calls == 1
    assert service.refresh_before_ids() == {"message-2"}
    assert mailbox.current_ids_calls == 2


def test_protocol_mailbox_uses_identity_message_baseline_without_refetch():
    import platforms.chatgpt.protocol_mailbox as protocol_mailbox

    class Mailbox(_Mailbox):
        def __init__(self):
            super().__init__()
            self.current_ids_calls = 0

        def get_current_ids(self, account):
            self.current_ids_calls += 1
            return {"unexpected-refetch"}

    mailbox = Mailbox()
    service = protocol_mailbox._MailboxEmailService(
        mailbox=mailbox,
        mailbox_account=_MailboxAccount(),
        provider="outlook_email_api",
        log_fn=lambda message: None,
        initial_before_ids={"identity-message"},
    )

    service.create_email()

    assert mailbox.current_ids_calls == 0
    assert service.refresh_before_ids() == {"identity-message"}
    assert mailbox.current_ids_calls == 0


def test_protocol_mailbox_wires_mailbox_debug_logger():
    import platforms.chatgpt.protocol_mailbox as protocol_mailbox

    class Mailbox(_Mailbox):
        def __init__(self):
            super().__init__()
            self.debug_logger = None

        def set_debug_logger(self, log_fn):
            self.debug_logger = log_fn

    logs = []
    mailbox = Mailbox()
    protocol_mailbox._MailboxEmailService(
        mailbox=mailbox,
        mailbox_account=_MailboxAccount(),
        provider="gmail_api_code",
        log_fn=logs.append,
    )

    mailbox.debug_logger("diagnostic")

    assert logs == ["diagnostic"]


def test_protocol_mailbox_raises_on_otp_timeout(monkeypatch):

    import platforms.chatgpt.protocol_mailbox as protocol_mailbox

    logs = []

    mailbox = _Mailbox()

    class FakeEngine:

        def __init__(self, **kwargs):

            self.email = ""

            self.password = ""

        def run(self):

            return ProtocolRegistrationResult(

                success=False,

                email=self.email,

                password=self.password,

                error_message="???????",

            )

    monkeypatch.setattr(protocol_mailbox, "RegistrationEngine", FakeEngine)

    worker = protocol_mailbox.ChatGPTProtocolMailboxWorker(

        mailbox=mailbox,

        mailbox_account=_MailboxAccount(),

        provider="cfworker_admin_api",

        proxy_url="http://proxy.local",

        log_fn=logs.append,

    )

    with pytest.raises(RuntimeError) as excinfo:
        worker.run(email="new@example.com", password="Secret123!")
    assert "???????" in str(excinfo.value)


def test_protocol_mailbox_raises_on_oauth_start_block(monkeypatch):

    import platforms.chatgpt.protocol_mailbox as protocol_mailbox

    logs = []

    class FakeEngine:

        def __init__(self, **kwargs):

            self.email = ""

            self.password = ""

        def run(self):

            return ProtocolRegistrationResult(

                success=False,

                email=self.email,

                password=self.password,

                error_message="?? OAuth ????",

            )

    monkeypatch.setattr(protocol_mailbox, "RegistrationEngine", FakeEngine)

    worker = protocol_mailbox.ChatGPTProtocolMailboxWorker(

        mailbox=_Mailbox(),

        mailbox_account=_MailboxAccount(),

        provider="outlook_email_api",

        proxy_url="http://proxy.local",

        log_fn=logs.append,

    )

    with pytest.raises(RuntimeError) as excinfo:
        worker.run(email="new@example.com", password="Secret123!")
    assert "?? OAuth ????" in str(excinfo.value)


def test_protocol_mailbox_keeps_non_otp_errors(monkeypatch):

    import platforms.chatgpt.protocol_mailbox as protocol_mailbox



    class FakeEngine:

        def __init__(self, **kwargs):

            self.email = ""

            self.password = ""



        def run(self):

            return ProtocolRegistrationResult(

                success=False,

                email=self.email,

                password=self.password,

                error_message="IP 位置不支持",

            )



    monkeypatch.setattr(protocol_mailbox, "RegistrationEngine", FakeEngine)



    worker = protocol_mailbox.ChatGPTProtocolMailboxWorker(

        mailbox=_Mailbox(),

        mailbox_account=_MailboxAccount(),

        provider="cfworker_admin_api",

        log_fn=lambda message: None,

    )



    with pytest.raises(RuntimeError, match="IP 位置不支持"):

        worker.run(email="new@example.com", password="Secret123!")





def test_protocol_mailbox_does_not_browser_fallback_after_invalid_email_mark(monkeypatch):

    import platforms.chatgpt.protocol_mailbox as protocol_mailbox

    class FakeEngine:

        def __init__(self, **kwargs):

            self.email = ""

            self.password = ""

        def run(self):

            return ProtocolRegistrationResult(

                success=False,

                email=self.email,

                password=self.password,

                error_message="邮箱验证码三轮未收到，已标记无效邮箱",

            )

    monkeypatch.setattr(protocol_mailbox, "RegistrationEngine", FakeEngine)

    worker = protocol_mailbox.ChatGPTProtocolMailboxWorker(

        mailbox=_Mailbox(),

        mailbox_account=_MailboxAccount(),

        provider="cfworker_admin_api",

        log_fn=lambda message: None,

    )

    with pytest.raises(RuntimeError, match="无效邮箱"):

        worker.run(email="new@example.com", password="Secret123!")


def test_protocol_mailbox_mapper_preserves_protocol_metadata():

    from platforms.chatgpt.plugin import ChatGPTPlatform



    class Ctx:

        password = "Secret123!"



    result = ProtocolRegistrationResult(

        success=True,

        email="new@example.com",

        password="Secret123!",

        account_id="acct_123",

        workspace_id="ws_123",

        access_token="access-token",

        refresh_token="refresh-token",

        id_token="id-token",

        session_token="session-token",

        metadata={

            "cookies": "session=abc",

            "profile": {"email": "new@example.com"},

            "expires_at": "2026-05-20T00:00:00Z",

            "password_set_after_register": True,

            "post_register_password_error": "",

        },

    )



    mapped = ChatGPTPlatform().build_protocol_mailbox_adapter().result_mapper(Ctx(), result)



    assert mapped.extra["cookies"] == "session=abc"

    assert mapped.extra["profile"] == {"email": "new@example.com"}

    assert mapped.extra["expires_at"] == "2026-05-20T00:00:00Z"

    assert mapped.extra["account_overview"]["registration_mode_label"] == "协议模式"

    assert mapped.extra["password_set_after_register"] is True


def test_protocol_mailbox_passes_set_password_flag_to_engine(monkeypatch):

    import platforms.chatgpt.protocol_mailbox as protocol_mailbox

    created = {}

    class FakeEngine:

        def __init__(self, **kwargs):

            created["engine"] = self

            self.email = ""

            self.password = ""

        def run_chatgpt_register_latest(self):

            return ProtocolRegistrationResult(

                success=True,

                email=self.email,

                password=self.password,

                account_id="acct_123",

                access_token="access-token",

                session_token="session-token",

                metadata={"cookies": "session=abc"},

            )

    monkeypatch.setattr(protocol_mailbox, "RegistrationEngine", FakeEngine)

    worker = protocol_mailbox.ChatGPTProtocolMailboxWorker(

        mailbox=_Mailbox(),

        mailbox_account=_MailboxAccount(),

        provider="cfworker_admin_api",

        log_fn=lambda message: None,

        set_password_after_register=False,

    )

    result = worker.run(email="new@example.com", password="Secret123!")

    assert result.success is True

    assert created["engine"].set_password_after_register is False

