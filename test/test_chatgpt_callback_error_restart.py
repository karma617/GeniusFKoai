from __future__ import annotations

from types import SimpleNamespace

from platforms.chatgpt import browser_register


def test_callback_error_url_requests_oauth_restart():
    url = (
        "http://localhost:1455/auth/callback"
        "?error=login_required"
        "&error_description=The+Authorization+Server+requires+End-User+authentication."
    )

    reason = browser_register._extract_callback_error_from_url(url)

    assert "login_required" in reason
    assert "End-User authentication" in reason


def test_submit_callback_error_result_does_not_exchange_token():
    logs: list[str] = []
    oauth_start = SimpleNamespace(
        state="state-1",
        code_verifier="verifier",
        redirect_uri="http://localhost:1455/auth/callback",
        client_id="client",
    )
    url = "http://localhost:1455/auth/callback?error=login_required&state=state-1"

    result = browser_register._submit_callback_result_or_error(
        url,
        oauth_start,
        proxy=None,
        log=logs.append,
    )

    assert result["oauth_restart_required"] is True
    assert result["callback_url"] == url
    assert result["error"] == "login_required"
    assert any("重走授权登录" in item for item in logs)


def test_wait_for_callback_error_returns_restart_marker():
    class FakePage:
        url = "http://localhost:1455/auth/callback?error=login_required"

        def evaluate(self, script):
            return self.url

    result = browser_register._wait_for_oauth_callback_result(
        FakePage(),
        SimpleNamespace(state="state-1", code_verifier="verifier", redirect_uri="", client_id=""),
        proxy=None,
        log=lambda _msg: None,
        timeout_sec=1,
    )

    assert result is not None
    assert result["oauth_restart_required"] is True
    assert result["error"] == "login_required"
