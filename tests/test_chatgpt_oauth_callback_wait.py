from types import SimpleNamespace

from platforms.chatgpt import browser_register
from platforms.chatgpt import oauth as oauth_module


class _FakePage:
    url = "http://localhost:1455/auth/callback?code=ac_test&state=state_123"

    def evaluate(self, script):
        return self.url


def test_wait_for_oauth_callback_result_exchanges_callback(monkeypatch):
    calls = {}

    def fake_submit_callback_result(callback_url, oauth_start, proxy, log=None):
        calls["callback_url"] = callback_url
        calls["state"] = oauth_start.state
        calls["proxy"] = proxy
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "account_id": "acct-123",
        }

    monkeypatch.setattr(browser_register, "_submit_callback_result", fake_submit_callback_result)

    result = browser_register._wait_for_oauth_callback_result(
        _FakePage(),
        SimpleNamespace(state="state_123"),
        proxy="http://proxy.example",
        log=lambda _message: None,
        timeout_sec=1,
    )

    assert result["access_token"] == "access-token"
    assert calls == {
        "callback_url": _FakePage.url,
        "state": "state_123",
        "proxy": "http://proxy.example",
    }


def test_submit_callback_result_exchanges_callback_without_state(monkeypatch):
    token_calls = {}

    def fake_post_form(url, data, proxy_url=None):
        token_calls["url"] = url
        token_calls["data"] = data
        token_calls["proxy_url"] = proxy_url
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth_module, "_post_form", fake_post_form)
    monkeypatch.setattr(
        oauth_module,
        "_jwt_claims_no_verify",
        lambda _token: {
            "email": "user@example.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"},
        },
    )

    result = browser_register._submit_callback_result(
        "http://localhost:1455/auth/callback?code=ac_missing_state",
        SimpleNamespace(
            state="state_123",
            code_verifier="verifier-123",
            redirect_uri="http://localhost:1455/auth/callback",
            client_id="client-123",
        ),
        proxy="http://proxy.example",
        log=lambda _message: None,
    )

    assert result["access_token"] == "access-token"
    assert result["refresh_token"] == "refresh-token"
    assert result["account_id"] == "acct-123"
    assert token_calls["data"]["code"] == "ac_missing_state"
    assert token_calls["data"]["code_verifier"] == "verifier-123"
    assert token_calls["proxy_url"] == "http://proxy.example"


def test_wait_for_oauth_callback_result_stops_on_token_exchange_region_error(monkeypatch):
    logs = []

    def fake_submit_callback_result(callback_url, oauth_start, proxy, log=None):
        raise RuntimeError(
            'token exchange failed: 403: {"error":{"code":"unsupported_country_region_territory"}}'
        )

    monkeypatch.setattr(browser_register, "_submit_callback_result", fake_submit_callback_result)

    result = browser_register._wait_for_oauth_callback_result(
        _FakePage(),
        SimpleNamespace(state="state_123"),
        proxy="http://proxy.example",
        log=logs.append,
        timeout_sec=1,
    )

    assert result["callback_captured"] is True
    assert "unsupported_country_region_territory" in result["error"]
    assert "代理/IP 地区不受支持" in result["error"]
    assert any("OAuth callback 捕获" in item and "state_status=match" in item for item in logs)
    assert any("proxy=http://proxy.example" in item for item in logs)


def test_oauth_authorize_debug_summary_mentions_local_pkce_source():
    summary = browser_register._oauth_authorize_debug_summary(
        SimpleNamespace(
            auth_url=(
                "https://auth.openai.com/oauth/authorize?"
                "client_id=client-123&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback"
                "&scope=openid+email&prompt=login&state=state-123&code_challenge=challenge-123"
            ),
            client_id="client-123",
            redirect_uri="http://localhost:1455/auth/callback",
            state="state-123",
        ),
        proxy=None,
    )

    assert "本地 generate_oauth_url(Codex client + PKCE)" in summary
    assert "client_id=client-123" in summary
    assert "redirect_uri=http://localhost:1455/auth/callback" in summary
    assert "proxy=(无，使用本机出口 IP)" in summary
