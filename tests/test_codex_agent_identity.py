from __future__ import annotations

import base64
import json
import time

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
    assert "task_id" not in auth_json["agent_identity"]


def test_create_codex_agent_identity_leaves_task_registration_to_sub2api(monkeypatch):
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
        raise AssertionError("task registration should be left to Sub2Api")

    monkeypatch.setattr(codex_agent_identity, "register_task", _fail_register_task)

    auth_json = codex_agent_identity.create_codex_agent_identity(
        _make_access_token(),
        verify_task=True,
    )

    assert auth_json["agent_identity"]["agent_runtime_id"] == "runtime-1"
    assert "task_id" not in auth_json["agent_identity"]
