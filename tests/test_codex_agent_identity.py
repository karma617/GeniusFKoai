from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import pytest

from platforms.chatgpt import codex_agent_identity


def _make_access_token() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = {
        "exp": int(time.time()) + 3600,
        "email": "agent@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "account-1",
            "chatgpt_user_id": "user-1",
            "chatgpt_plan_type": "free",
        },
        "https://api.openai.com/profile": {
            "email": "agent@example.com",
        },
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def test_get_session_from_access_token_reads_chatgpt_claims():
    session = codex_agent_identity.get_session_from_access_token(_make_access_token())

    assert session["accountId"] == "account-1"
    assert session["userId"] == "user-1"
    assert session["email"] == "agent@example.com"
    assert session["planType"] == "free"


def test_generate_auth_json_uses_agent_identity_mode():
    auth_json = codex_agent_identity.generate_auth_json(
        agent_runtime_id="runtime-1",
        private_key_pkcs8_b64="private-key",
        account_id="account-1",
        chatgpt_user_id="user-1",
        email="agent@example.com",
        plan_type="free",
        task_id="task-1",
    )

    assert auth_json["auth_mode"] == "agentIdentity"
    assert auth_json["agent_identity"]["agent_runtime_id"] == "runtime-1"
    assert auth_json["agent_identity"]["task_id"] == "task-1"


def test_register_agent_uses_official_codex_payload(monkeypatch):
    captured = {}

    def _post(*args, **kwargs):
        captured["url"] = args[0]
        captured["json"] = kwargs["json"]
        return SimpleNamespace(
            status_code=200,
            text='{"agent_runtime_id":"runtime-1"}',
            headers={"content-type": "application/json"},
            json=lambda: {"agent_runtime_id": "runtime-1"},
        )

    monkeypatch.setattr(codex_agent_identity.requests, "post", _post)

    assert codex_agent_identity.register_agent(_make_access_token(), "public-key") == "runtime-1"
    assert captured["url"].endswith("/v1/agent/register")
    assert captured["json"]["agent_public_key"] == "public-key"
    assert captured["json"]["capabilities"] == ["responsesapi"]
    assert captured["json"]["ttl"] is None


def test_create_codex_agent_identity_registers_direct_task_id(monkeypatch):
    monkeypatch.setattr(
        codex_agent_identity,
        "generate_ed25519_keypair",
        lambda: ("private-key", "public-key"),
    )
    monkeypatch.setattr(
        codex_agent_identity,
        "register_agent",
        lambda access_token, public_key_ssh, timeout=15: "runtime-1",
    )
    monkeypatch.setattr(
        codex_agent_identity,
        "register_task",
        lambda access_token, agent_runtime_id, private_key_pkcs8_b64, timeout=15: "task-1",
    )

    auth_json = codex_agent_identity.create_codex_agent_identity(
        _make_access_token(),
        verify_task=True,
    )

    assert auth_json["agent_identity"]["agent_runtime_id"] == "runtime-1"
    assert auth_json["agent_identity"]["task_id"] == "task-1"


def test_create_codex_agent_identity_skips_task_when_disabled(monkeypatch):
    monkeypatch.setattr(
        codex_agent_identity,
        "generate_ed25519_keypair",
        lambda: ("private-key", "public-key"),
    )
    monkeypatch.setattr(
        codex_agent_identity,
        "register_agent",
        lambda access_token, public_key_ssh, timeout=15: "runtime-1",
    )

    def _fail_register_task(*args, **kwargs):
        raise AssertionError("task registration should be skipped")

    monkeypatch.setattr(codex_agent_identity, "register_task", _fail_register_task)

    auth_json = codex_agent_identity.create_codex_agent_identity(
        _make_access_token(),
        verify_task=False,
    )

    assert auth_json["agent_identity"]["agent_runtime_id"] == "runtime-1"
    assert "task_id" not in auth_json["agent_identity"]


def test_generate_auth_json_from_sub2api_exported_credentials():
    auth_json = codex_agent_identity.generate_auth_json_from_agent_identity_credentials({
        "credentials": {
            "agent_private_key": "private-key",
            "agent_runtime_id": "agent-1",
            "auth_mode": "agentIdentity",
            "chatgpt_account_id": "account-1",
            "chatgpt_account_is_fedramp": False,
            "chatgpt_user_id": "user-1",
            "email": "LessleyJurisch93@hotmail.com",
            "plan_type": "free",
            "task_id": "task-1",
        }
    })

    assert auth_json == {
        "auth_mode": "agentIdentity",
        "agent_identity": {
            "agent_runtime_id": "agent-1",
            "agent_private_key": "private-key",
            "account_id": "account-1",
            "chatgpt_user_id": "user-1",
            "email": "LessleyJurisch93@hotmail.com",
            "plan_type": "free",
            "chatgpt_account_is_fedramp": False,
            "task_id": "task-1",
        },
    }


def test_generate_auth_json_from_agent_identity_jwt():
    agent_identity_jwt = _make_jwt({
        "iss": "https://chatgpt.com/codex-backend/agent-identity",
        "aud": "codex-app-server",
        "iat": 1,
        "exp": 9999999999,
        "agent_runtime_id": "agent-jwt-1",
        "agent_private_key": "private-key",
        "account_id": "account-1",
        "chatgpt_user_id": "user-1",
        "email": "free@example.com",
        "plan_type": "free",
        "chatgpt_account_is_fedramp": False,
    })

    auth_json = codex_agent_identity.generate_auth_json_from_agent_identity_jwt(agent_identity_jwt)

    assert auth_json == {
        "auth_mode": "agentIdentity",
        "agent_identity": {
            "agent_runtime_id": "agent-jwt-1",
            "agent_private_key": "private-key",
            "account_id": "account-1",
            "chatgpt_user_id": "user-1",
            "email": "free@example.com",
            "plan_type": "free",
            "chatgpt_account_is_fedramp": False,
        },
    }


def test_generate_auth_json_from_agent_identity_jwt_rejects_wrong_issuer():
    agent_identity_jwt = _make_jwt({
        "iss": "https://chatgpt.com/not-agent-identity",
        "aud": "codex-app-server",
    })

    with pytest.raises(ValueError, match="issuer mismatch"):
        codex_agent_identity.generate_auth_json_from_agent_identity_jwt(agent_identity_jwt)


def test_register_agent_reports_non_json_response(monkeypatch):
    response = SimpleNamespace(
        status_code=200,
        text="<html>challenge</html>",
        headers={"content-type": "text/html"},
        json=lambda: (_ for _ in ()).throw(ValueError("unexpected character: line 1 column 1 (char 0)")),
    )
    monkeypatch.setattr(codex_agent_identity.requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError) as exc:
        codex_agent_identity.register_agent(_make_access_token(), "public-key")

    message = str(exc.value)
    assert "Agent registration 返回非 JSON" in message
    assert "HTTP 200" in message
    assert "text/html" in message
    assert "challenge" in message


def test_register_agent_reports_registry_not_enabled(monkeypatch):
    response = SimpleNamespace(
        status_code=403,
        text='{"error":{"code":"agent_registry_not_enabled"}}',
        headers={"content-type": "application/json"},
        json=lambda: {"error": {"code": "agent_registry_not_enabled"}},
    )
    monkeypatch.setattr(codex_agent_identity.requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(codex_agent_identity.AgentRegistryNotEnabledError):
        codex_agent_identity.register_agent(_make_access_token(), "public-key")


def test_create_codex_agent_identity_recovers_old_runtime_and_registers_new_task(monkeypatch):
    monkeypatch.setattr(
        codex_agent_identity,
        "generate_ed25519_keypair",
        lambda: ("new-private", "new-public"),
    )

    def _registry_not_enabled(*_args, **_kwargs):
        raise codex_agent_identity.AgentRegistryNotEnabledError("agent_registry_not_enabled")

    monkeypatch.setattr(codex_agent_identity, "register_agent", _registry_not_enabled)
    monkeypatch.setattr(
        codex_agent_identity,
        "recover_agent_runtime_from_sub2api",
        lambda **_kwargs: codex_agent_identity.RecoveredAgentRuntime(
            agent_runtime_id="old-runtime",
            private_key_pkcs8_b64="old-private",
        ),
    )
    captured = {}

    def _register_task(access_token, agent_runtime_id, private_key_pkcs8_b64, timeout=15):
        captured["agent_runtime_id"] = agent_runtime_id
        captured["private_key"] = private_key_pkcs8_b64
        return "new-task"

    monkeypatch.setattr(codex_agent_identity, "register_task", _register_task)

    auth_json = codex_agent_identity.create_codex_agent_identity(
        _make_access_token(),
        verify_task=False,
        sub2api_url="https://sub2api.example.test",
        sub2api_email="admin@example.test",
        sub2api_password="secret",
    )

    assert captured == {"agent_runtime_id": "old-runtime", "private_key": "old-private"}
    assert auth_json["agent_identity"]["agent_runtime_id"] == "old-runtime"
    assert auth_json["agent_identity"]["agent_private_key"] == "old-private"
    assert auth_json["agent_identity"]["task_id"] == "new-task"
    assert auth_json["agent_identity"]["account_id"] == "account-1"
    assert auth_json["agent_identity"]["chatgpt_user_id"] == "user-1"


def test_recover_agent_runtime_from_sub2api_prefers_exact_match(monkeypatch):
    from platforms.chatgpt import sub2api_upload

    private_key, _public = codex_agent_identity.generate_ed25519_keypair()
    calls = []

    def _login(api_url, email, password, timeout=30):
        assert api_url == "https://sub2api.example.test"
        assert email == "admin@example.test"
        assert password == "secret"
        return "https://sub2api.example.test", "admin-token"

    def _request(origin, path, *, token="", timeout=30, **_kwargs):
        calls.append(path)
        assert origin == "https://sub2api.example.test"
        assert token == "admin-token"
        if path.startswith("/api/v1/admin/accounts?"):
            return {
                "items": [
                    {
                        "id": 41,
                        "status": "active",
                        "credentials": {
                            "auth_mode": "agentIdentity",
                            "agent_runtime_id": "shared-runtime",
                            "chatgpt_account_id": "shared-account",
                            "chatgpt_user_id": "shared-user",
                            "task_id": "old-task",
                        },
                        "credentials_status": {"has_agent_private_key": True},
                    },
                    {
                        "id": 42,
                        "status": "active",
                        "credentials": {
                            "auth_mode": "agentIdentity",
                            "agent_runtime_id": "exact-runtime",
                            "chatgpt_account_id": "account-1",
                            "chatgpt_user_id": "user-1",
                            "task_id": "old-task",
                        },
                        "credentials_status": {"has_agent_private_key": True},
                    },
                ],
                "pages": 1,
            }
        if "ids=42" in path:
            return {
                "accounts": [
                    {
                        "platform": "openai",
                        "type": "oauth",
                        "credentials": {
                            "auth_mode": "agentIdentity",
                            "agent_runtime_id": "exact-runtime",
                            "agent_private_key": private_key,
                            "chatgpt_account_id": "account-1",
                            "chatgpt_user_id": "user-1",
                        },
                    }
                ]
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(sub2api_upload, "login_sub2api", _login)
    monkeypatch.setattr(sub2api_upload, "_request_json", _request)

    recovered = codex_agent_identity.recover_agent_runtime_from_sub2api(
        chatgpt_account_id="account-1",
        chatgpt_user_id="user-1",
        api_url="https://sub2api.example.test",
        email="admin@example.test",
        password="secret",
    )

    assert recovered.agent_runtime_id == "exact-runtime"
    assert recovered.private_key_pkcs8_b64 == private_key
    assert any("ids=42" in call for call in calls)
    assert not any("ids=41" in call for call in calls)
