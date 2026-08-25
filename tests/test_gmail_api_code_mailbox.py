from core.base_mailbox import MailboxAccount
from core.gmail_api_code_mailbox import GmailApiCodeMailbox
from platforms.chatgpt.constants import OTP_CODE_PATTERN


NO_MAIL_HTML = """
<!doctype html>
<html lang="zh-CN">
<style>body { color: #172033; }</style>
<body><h1>邮箱邮件</h1><p>最近 1 天没有邮件</p></body>
</html>
"""

JAPANESE_OTP_HTML = """
<!doctype html>
<html lang="ja">
<style>body { color: #202123; }</style>
<body>
  <table class="main"><tr><td>
    <p>ChatGPT 用の一時ログインコード</p>
    <p>この一時検証コードを入力して続行してください: 542243</p>
  </td></tr></table>
  <p>时间：2026-08-25 15:56:53</p>
</body>
</html>
"""


def test_custom_pattern_ignores_six_digit_css_color_on_empty_mailbox_page():
    assert GmailApiCodeMailbox._extract_code(NO_MAIL_HTML, OTP_CODE_PATTERN) == ""


def test_custom_pattern_extracts_visible_japanese_otp_instead_of_css_color():
    assert GmailApiCodeMailbox._extract_code(JAPANESE_OTP_HTML, OTP_CODE_PATTERN) == "542243"


def test_message_identity_is_consistent_with_and_without_custom_pattern():
    mailbox = object.__new__(GmailApiCodeMailbox)
    mailbox._last_fetch_debug = {}

    default_id = mailbox._current_id(JAPANESE_OTP_HTML)
    custom_id = mailbox._current_id(JAPANESE_OTP_HTML, OTP_CODE_PATTERN)

    assert default_id == custom_id == "code:542243"


def test_wait_for_code_ignores_empty_page_then_returns_visible_otp():
    mailbox = GmailApiCodeMailbox(
        pool_text="person@example.com----https://mail.example.test/messages",
        poll_interval="1",
    )
    account = MailboxAccount(email="person@example.com")
    responses = iter((NO_MAIL_HTML, JAPANESE_OTP_HTML))
    mailbox._fetch_text = lambda _entry: next(responses)
    mailbox.poll_interval = 0
    baseline_id = mailbox._current_id(NO_MAIL_HTML)

    code = mailbox.wait_for_code(
        account,
        timeout=1,
        before_ids={baseline_id},
        code_pattern=OTP_CODE_PATTERN,
    )

    assert baseline_id.startswith("body:")
    assert code == "542243"
