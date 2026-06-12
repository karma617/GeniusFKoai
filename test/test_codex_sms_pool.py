from __future__ import annotations

from core.base_sms import (
    CodexSmsPoolProvider,
    create_sms_provider,
    extract_codex_sms_verification_code,
    parse_codex_sms_pool_entries,
)


class FakeResponse:
    def __init__(self, text: str, ok: bool = True, status_code: int = 200):
        self.text = text
        self.ok = ok
        self.status_code = status_code


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.urls.append(url)
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse("{}")


def test_parse_codex_sms_pool_entries_supports_pipe_and_gujumpgate_separator():
    text = "\n".join(
        [
            "+12367724448|http://cdk.jijie.chat/sms-pickup?phone=12367724448&t=1",
            "447700900123----http://example.test/sms?phone=447700900123",
        ]
    )

    entries = parse_codex_sms_pool_entries(text)

    assert [entry.phone_e164 for entry in entries] == ["+12367724448", "+447700900123"]
    assert entries[0].verification_url == "http://cdk.jijie.chat/sms-pickup?phone=12367724448"
    assert "----" in entries[1].key


def test_extract_codex_sms_verification_code_from_json_or_text():
    assert extract_codex_sms_verification_code({"message": "Your verification code is 654321."}) == "654321"
    assert extract_codex_sms_verification_code({"data": {"otp": "123 456"}}) == "123456"
    assert extract_codex_sms_verification_code("验证码：778899，请勿泄露") == "778899"
    assert extract_codex_sms_verification_code({"phone": "12367724448", "status": "ok"}) == ""


def test_codex_sms_pool_provider_gets_number_and_polls_code(tmp_path):
    state_file = tmp_path / "codex_sms_state.json"
    session = FakeSession([FakeResponse('{"message":"OpenAI code: 246810"}')])
    provider = CodexSmsPoolProvider(
        "+12367724448|http://cdk.jijie.chat/sms-pickup?phone=12367724448",
        poll_interval=1,
        request_timeout=1,
        state_file=state_file,
        session=session,  # type: ignore[arg-type]
    )

    activation = provider.get_number(service="chatgpt")
    code = provider.get_code(activation.activation_id, timeout=1)

    assert activation.phone_number == "+12367724448"
    assert code == "246810"
    assert session.urls == ["http://cdk.jijie.chat/sms-pickup?phone=12367724448"]


def test_codex_sms_pool_rotates_after_phone_send_failure(tmp_path):
    state_file = tmp_path / "codex_sms_state.json"
    provider = CodexSmsPoolProvider(
        "\n".join(
            [
                "+12367724448|http://cdk.jijie.chat/sms-pickup?phone=12367724448",
                "+12367724449|http://cdk.jijie.chat/sms-pickup?phone=12367724449",
            ]
        ),
        poll_interval=1,
        request_timeout=1,
        state_file=state_file,
        session=FakeSession([]),  # type: ignore[arg-type]
    )

    first = provider.get_number(service="chatgpt")
    provider.mark_send_failed(first.activation_id, "virtual phone unsupported")
    second = provider.get_number(service="chatgpt")

    assert first.phone_number == "+12367724448"
    assert second.phone_number == "+12367724449"


def test_create_sms_provider_accepts_codex_pool():
    provider = create_sms_provider(
        "codex_sms_pool",
        {"codex_sms_pool_text": "+12367724448|http://cdk.jijie.chat/sms-pickup?phone=12367724448"},
    )

    assert isinstance(provider, CodexSmsPoolProvider)
