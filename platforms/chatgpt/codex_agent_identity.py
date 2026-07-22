"""Codex Agent Identity auth.json 生成工具。"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from curl_cffi import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

AUTHAPI_BASE = "https://auth.openai.com/api/accounts"
IMPERSONATE = "chrome"
AGENT_VERSION = "0.138.0-alpha.6"
AGENT_HARNESS_ID = "codex-cli"
RUNNING_LOCATION = "local"


def generate_ed25519_keypair() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    pkcs8_der = private_key.private_bytes(
        encoding=Encoding.DER,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    private_key_b64 = base64.b64encode(pkcs8_der).decode()

    pub_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    ssh_header = b"ssh-ed25519"
    blob = bytearray()
    blob.extend(len(ssh_header).to_bytes(4, "big"))
    blob.extend(ssh_header)
    blob.extend(len(pub_bytes).to_bytes(4, "big"))
    blob.extend(pub_bytes)
    public_key_ssh = f"ssh-ed25519 {base64.b64encode(bytes(blob)).decode()}"
    return private_key_b64, public_key_ssh


def decode_jwt_claims(jwt_token: str) -> dict[str, Any]:
    parts = str(jwt_token or "").split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    payload_b64 = parts[1]
    payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


def get_session_from_access_token(access_token: str) -> dict[str, Any]:
    claims = decode_jwt_claims(access_token)
    auth_info = claims.get("https://api.openai.com/auth", {})
    profile = claims.get("https://api.openai.com/profile", {})
    if not isinstance(auth_info, dict):
        auth_info = {}
    if not isinstance(profile, dict):
        profile = {}
    return {
        "accessToken": access_token,
        "accountId": auth_info.get("chatgpt_account_id", ""),
        "userId": auth_info.get("chatgpt_user_id", "") or auth_info.get("user_id", ""),
        "email": profile.get("email") or claims.get("email", ""),
        "planType": auth_info.get("chatgpt_plan_type", "free"),
    }


def register_agent(access_token: str, public_key_ssh: str, *, timeout: int = 15) -> str:
    response = requests.post(
        f"{AUTHAPI_BASE}/v1/agent/register",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "abom": {
                "agent_version": AGENT_VERSION,
                "agent_harness_id": AGENT_HARNESS_ID,
                "running_location": RUNNING_LOCATION,
            },
            "agent_public_key": public_key_ssh,
        },
        impersonate=IMPERSONATE,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Agent registration failed: {response.status_code} {response.text}")
    data = response.json()
    agent_runtime_id = data.get("agent_runtime_id")
    if not agent_runtime_id:
        raise RuntimeError(f"No agent_runtime_id in response: {data}")
    return str(agent_runtime_id)


def register_task(
    access_token: str,
    agent_runtime_id: str,
    private_key_pkcs8_b64: str,
    *,
    timeout: int = 15,
) -> str:
    pkcs8_der = base64.b64decode(private_key_pkcs8_b64)
    pem = b"-----BEGIN PRIVATE KEY-----\n" + base64.encodebytes(pkcs8_der) + b"-----END PRIVATE KEY-----\n"
    private_key = load_pem_private_key(pem, password=None)

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = f"{agent_runtime_id}:{timestamp}"
    signature_b64 = base64.b64encode(private_key.sign(payload.encode())).decode()

    response = requests.post(
        f"{AUTHAPI_BASE}/v1/agent/{agent_runtime_id}/task/register",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        json={"timestamp": timestamp, "signature": signature_b64},
        impersonate=IMPERSONATE,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Task registration failed: {response.status_code} {response.text}")
    data = response.json()
    return str(data.get("encrypted_task_id") or "")


def generate_auth_json(
    *,
    agent_runtime_id: str,
    private_key_pkcs8_b64: str,
    account_id: str,
    chatgpt_user_id: str,
    email: str,
    plan_type: str = "free",
    task_id: str = "",
    chatgpt_account_is_fedramp: bool = False,
) -> dict[str, Any]:
    agent_identity = {
        "agent_runtime_id": agent_runtime_id,
        "agent_private_key": private_key_pkcs8_b64,
        "account_id": account_id,
        "chatgpt_user_id": chatgpt_user_id,
        "email": email,
        "plan_type": plan_type,
        "chatgpt_account_is_fedramp": chatgpt_account_is_fedramp,
    }
    return {
        "auth_mode": "agentIdentity",
        "agent_identity": agent_identity,
    }


def create_codex_agent_identity(
    access_token: str,
    *,
    verify_task: bool = True,
    timeout: int = 15,
) -> dict[str, Any]:
    session = get_session_from_access_token(access_token)
    account_id = str(session["accountId"] or "")
    chatgpt_user_id = str(session["userId"] or "")
    if not account_id or not chatgpt_user_id:
        raise RuntimeError(f"JWT 缺少必要字段: account_id={account_id}, user_id={chatgpt_user_id}")

    private_key_b64, public_key_ssh = generate_ed25519_keypair()
    agent_runtime_id = register_agent(access_token, public_key_ssh, timeout=timeout)
    return generate_auth_json(
        agent_runtime_id=agent_runtime_id,
        private_key_pkcs8_b64=private_key_b64,
        account_id=account_id,
        chatgpt_user_id=chatgpt_user_id,
        email=str(session.get("email") or ""),
        plan_type=str(session.get("planType") or "free"),
        chatgpt_account_is_fedramp=False,
    )
