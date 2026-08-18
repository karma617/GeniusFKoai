from __future__ import annotations

from types import SimpleNamespace

from application import tasks


def test_smsbower_mail_otp_timeout_error_detected():
    assert tasks._is_smsbower_mail_otp_timeout_error(
        "等待 SMSBower 验证码超时 (600s) last=Code has not been received yet"
    )


def test_registration_disallowed_marks_browser_mailbox_parent_exhausted():
    calls = []
    logs = []

    class Mailbox:
        def mark_parent_exhausted(self, account, reason=""):
            calls.append((account.email, reason))
            return ["已注册且子邮箱耗尽"]

    mailbox_account = SimpleNamespace(email="sample@icloud.com", extra={})
    platform = SimpleNamespace(_last_identity=SimpleNamespace(mailbox_account=mailbox_account))
    logger = SimpleNamespace(log=lambda message, **_kwargs: logs.append(message))

    assert tasks._is_registration_disallowed_error(
        "400 registration_disallowed: We can't create your account due to our Terms of Use"
    )
    assert tasks._is_registration_disallowed_error(
        "Sorry, we cannot create your account due to our Terms of Use"
    )
    assert not tasks._is_registration_disallowed_error("curl: (28) Connection timed out")
    assert tasks._mark_registration_email_exhausted(
        platform,
        Mailbox(),
        logger,
        "registration_disallowed",
    )
    assert calls == [("sample@icloud.com", "registration_disallowed")]
    assert any("已标记邮箱已注册且子邮箱耗尽 sample@icloud.com" in item for item in logs)


def test_release_smsbower_mailbox_after_otp_timeout_marks_invalid():
    calls = []
    logs = []

    class Mailbox:
        def mark_invalid_email(self, account, reason=""):
            calls.append((account.email, reason))
            return ["smsbower_cancelled"]

    mailbox_account = SimpleNamespace(
        email="sample@gmail.com",
        extra={"mailbox_provider_key": "smsbower_mail_api"},
    )
    platform = SimpleNamespace(_last_identity=SimpleNamespace(mailbox_account=mailbox_account))
    logger = SimpleNamespace(log=lambda message, **_kwargs: logs.append(message))

    released = tasks._release_smsbower_mailbox_after_otp_timeout(
        platform,
        Mailbox(),
        logger,
        "等待 SMSBower 验证码超时",
    )

    assert released is True
    assert calls == [("sample@gmail.com", "等待 SMSBower 验证码超时")]
    assert any("已释放当前邮箱 sample@gmail.com" in item for item in logs)
