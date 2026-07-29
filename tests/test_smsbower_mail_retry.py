from __future__ import annotations

from types import SimpleNamespace

from application import tasks


def test_smsbower_mail_otp_timeout_error_detected():
    assert tasks._is_smsbower_mail_otp_timeout_error(
        "等待 SMSBower 验证码超时 (600s) last=Code has not been received yet"
    )


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
