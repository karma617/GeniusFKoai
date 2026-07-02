from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import pytest

from platforms.chatgpt import sub2api_upload


def _make_access_token() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = {
        "exp": int(time.time()) + 3600,
        "email": "k12@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "account-1",
            "chatgpt_user_id": "user-1",
        },
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def test_normal_account_without_refresh_token_still_rejected():
    account = SimpleNamespace(
        email="normal@example.com",
        credentials={"access_token": _make_access_token(), "plan_type": "free"},
    )

    with pytest.raises(ValueError, match="rt"):
        sub2api_upload._build_direct_account_payload(
            account,
            group_ids=[1],
            proxy_id=None,
            priority=1,
        )


def test_k12_account_without_refresh_token_builds_direct_payload():
    account = SimpleNamespace(
        email="k12@example.com",
        credentials={"access_token": _make_access_token(), "plan_type": "k12"},
    )

    payload = sub2api_upload._build_direct_account_payload(
        account,
        group_ids=[1],
        proxy_id=None,
        priority=1,
    )

    assert payload["credentials"]["plan_type"] == "k12"
    assert "refresh_token" not in payload["credentials"]


def test_k12_account_without_refresh_token_builds_import_payload():
    account = SimpleNamespace(
        email="k12@example.com",
        credentials={"access_token": _make_access_token(), "plan_type": "k12"},
    )

    payload = sub2api_upload._build_import_payload(
        account,
        group_ids=[1],
        proxy_id=None,
        priority=1,
    )

    assert payload["content"] == account.credentials["access_token"]


def test_request_json_retries_transient_request_exception(monkeypatch):
    calls = {"count": 0}

    def fake_request(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("TLS connect error")
        return SimpleNamespace(status_code=200, text='{"code":0,"data":{"ok":true}}')

    monkeypatch.setattr(sub2api_upload.cffi_requests, "request", fake_request)

    result = sub2api_upload._request_json(
        "https://sub2api.test",
        "/api/v1/admin/accounts",
        retries=3,
        retry_delay=0,
    )

    assert result == {"ok": True}
    assert calls["count"] == 3


def test_request_json_does_not_retry_business_400(monkeypatch):
    calls = {"count": 0}

    def fake_request(*args, **kwargs):
        calls["count"] += 1
        return SimpleNamespace(status_code=400, text='{"message":"bad request"}')

    monkeypatch.setattr(sub2api_upload.cffi_requests, "request", fake_request)

    with pytest.raises(sub2api_upload.Sub2ApiRequestError, match="bad request"):
        sub2api_upload._request_json(
            "https://sub2api.test",
            "/api/v1/admin/accounts",
            retries=3,
            retry_delay=0,
        )

    assert calls["count"] == 1
