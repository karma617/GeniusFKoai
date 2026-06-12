import base64
import json
from urllib.parse import parse_qs, urlparse

from platforms.chatgpt.register import (
    PLATFORM_OAUTH_CLIENT_ID,
    PLATFORM_OAUTH_REDIRECT_URI,
    RegistrationEngine,
    _cookies_to_header,
    _extract_chatgpt_account_id,
    _extract_oauth_callback_params_from_url,
)


def _jwt(payload: dict) -> str:
    """构造无签名测试 JWT；只测 payload 解析，不做真实验签。"""
    header = {"alg": "none", "typ": "JWT"}

    def enc(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc(header)}.{enc(payload)}."


def test_chatgptfree_session_extracts_account_id_from_access_token():
    token = _jwt(
        {
            "sub": "user-fallback",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-chatgpt-free",
            },
        }
    )

    assert _extract_chatgpt_account_id(token) == "acct-chatgpt-free"


def test_chatgptfree_session_falls_back_to_sub_claim():
    token = _jwt({"sub": "user-fallback"})

    assert _extract_chatgpt_account_id(token) == "user-fallback"


def test_chatgptfree_cookie_header_preserves_session_cookie():
    header = _cookies_to_header(
        {
            "__Secure-next-auth.session-token": "session-123",
            "_account": "acct-cookie",
        }
    )

    assert "__Secure-next-auth.session-token=session-123" in header
    assert "_account=acct-cookie" in header


def test_chatgptfree_platform_oauth_url_matches_reference_client():
    engine = RegistrationEngine(email_service=object(), callback_logger=lambda _msg: None)

    start = engine._build_platform_oauth_start("free@example.com", "device-123")
    params = parse_qs(urlparse(start.auth_url).query)

    assert params["client_id"] == [PLATFORM_OAUTH_CLIENT_ID]
    assert params["redirect_uri"] == [PLATFORM_OAUTH_REDIRECT_URI]
    assert params["audience"] == ["https://api.openai.com/v1"]
    assert params["login_hint"] == ["free@example.com"]
    assert params["device_id"] == ["device-123"]
    assert start.client_id == PLATFORM_OAUTH_CLIENT_ID


def test_chatgptfree_platform_callback_param_parser():
    parsed = _extract_oauth_callback_params_from_url(
        "https://platform.openai.com/auth/callback?code=abc&state=xyz&scope=openid"
    )

    assert parsed == {"code": "abc", "state": "xyz", "scope": "openid"}
