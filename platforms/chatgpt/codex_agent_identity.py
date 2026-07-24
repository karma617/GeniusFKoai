"""Codex Agent Identity auth.json 生成工具。"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_der_private_key,
    load_pem_private_key,
)

AUTHAPI_BASE = "https://auth.openai.com/api/accounts"
IMPERSONATE = "chrome"
CHROME_VERSION = "146"
USER_AGENT = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_VERSION}.0.0.0 Safari/537.36"
)
AGENT_VERSION = "0.138.0-alpha.6"
AGENT_HARNESS_ID = "codex-cli"
RUNNING_LOCATION = "local"
AGENT_CAPABILITIES = ["responsesapi"]
AGENT_IDENTITY_JWT_AUDIENCE = "codex-app-server"
AGENT_IDENTITY_JWT_ISSUER = "https://chatgpt.com/codex-backend/agent-identity"


class AgentRegistryNotEnabledError(RuntimeError):
    """目标账号未开放 Agent Registry；改走 Sub2API 旧 Runtime 恢复链路。"""


@dataclass(slots=True)
class RecoveredAgentRuntime:
    agent_runtime_id: str
    private_key_pkcs8_b64: str


def _response_json_or_raise(response: Any, action: str) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception as exc:
        status_code = int(getattr(response, "status_code", 0) or 0)
        headers = getattr(response, "headers", {}) or {}
        content_type = ""
        if isinstance(headers, dict):
            content_type = str(headers.get("content-type") or headers.get("Content-Type") or "")
        text = str(getattr(response, "text", "") or "")
        snippet = text.replace("\r", "\\r").replace("\n", "\\n")[:300]
        raise RuntimeError(
            f"{action} 返回非 JSON：HTTP {status_code} content-type={content_type or '-'} body={snippet or '-'}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{action} 返回 JSON 不是对象: {data}")
    return data


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


def decode_agent_identity_jwt_claims(agent_identity_jwt: str) -> dict[str, Any]:
    claims = decode_jwt_claims(agent_identity_jwt)
    issuer = str(claims.get("iss") or "")
    audience = claims.get("aud")
    if issuer != AGENT_IDENTITY_JWT_ISSUER:
        raise ValueError(f"Agent Identity JWT issuer mismatch: {issuer or '(empty)'}")
    if audience != AGENT_IDENTITY_JWT_AUDIENCE:
        raise ValueError(f"Agent Identity JWT audience mismatch: {audience or '(empty)'}")
    return claims


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


def build_agent_registration_payload(public_key_ssh: str) -> dict[str, Any]:
    return {
        "abom": {
            "agent_version": AGENT_VERSION,
            "agent_harness_id": AGENT_HARNESS_ID,
            "running_location": RUNNING_LOCATION,
        },
        "agent_public_key": public_key_ssh,
        "capabilities": list(AGENT_CAPABILITIES),
        "ttl": None,
    }


def _openai_error_code(data: dict[str, Any]) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or "").strip()
    return str(data.get("code") or "").strip() if isinstance(data, dict) else ""


def register_agent(access_token: str, public_key_ssh: str, *, timeout: int = 15) -> str:
    response = requests.post(
        f"{AUTHAPI_BASE}/v1/agent/register",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
        },
        json=build_agent_registration_payload(public_key_ssh),
        impersonate=IMPERSONATE,
        timeout=timeout,
    )
    if response.status_code < 200 or response.status_code >= 300:
        data = {}
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code == 403 and _openai_error_code(data) == "agent_registry_not_enabled":
            raise AgentRegistryNotEnabledError("agent_registry_not_enabled")
        raise RuntimeError(f"Agent registration failed: {response.status_code} {response.text}")
    data = _response_json_or_raise(response, "Agent registration")
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
    _ = access_token
    pkcs8_der = base64.b64decode(private_key_pkcs8_b64)
    pem = b"-----BEGIN PRIVATE KEY-----\n" + base64.encodebytes(pkcs8_der) + b"-----END PRIVATE KEY-----\n"
    private_key = load_pem_private_key(pem, password=None)

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = f"{agent_runtime_id}:{timestamp}"
    signature_b64 = base64.b64encode(private_key.sign(payload.encode())).decode()

    response = requests.post(
        f"{AUTHAPI_BASE}/v1/agent/{agent_runtime_id}/task/register",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        json={"timestamp": timestamp, "signature": signature_b64},
        impersonate=IMPERSONATE,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Task registration failed: {response.status_code} {response.text}")
    data = _response_json_or_raise(response, "Task registration")
    task_id = data.get("task_id") or data.get("taskId")
    if task_id:
        return str(task_id)
    return ""


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
    if task_id:
        agent_identity["task_id"] = task_id
    return {
        "auth_mode": "agentIdentity",
        "agent_identity": agent_identity,
    }


def generate_auth_json_from_agent_identity_credentials(raw: dict[str, Any]) -> dict[str, Any]:
    credentials = dict(raw.get("credentials") or raw.get("agent_identity") or raw)
    account_id = credentials.get("account_id") or credentials.get("accountId") or credentials.get("chatgpt_account_id")
    chatgpt_user_id = credentials.get("chatgpt_user_id") or credentials.get("chatgptUserId")
    return generate_auth_json(
        agent_runtime_id=str(credentials.get("agent_runtime_id") or credentials.get("agentRuntimeId") or ""),
        private_key_pkcs8_b64=str(credentials.get("agent_private_key") or credentials.get("agentPrivateKey") or ""),
        account_id=str(account_id or ""),
        chatgpt_user_id=str(chatgpt_user_id or ""),
        email=str(credentials.get("email") or ""),
        plan_type=str(credentials.get("plan_type") or credentials.get("planType") or "free"),
        task_id=str(credentials.get("task_id") or credentials.get("taskId") or ""),
        chatgpt_account_is_fedramp=bool(
            credentials.get("chatgpt_account_is_fedramp")
            or credentials.get("chatgptAccountIsFedramp")
            or False
        ),
    )


def generate_auth_json_from_agent_identity_jwt(agent_identity_jwt: str) -> dict[str, Any]:
    claims = decode_agent_identity_jwt_claims(agent_identity_jwt)
    return generate_auth_json(
        agent_runtime_id=str(claims.get("agent_runtime_id") or ""),
        private_key_pkcs8_b64=str(claims.get("agent_private_key") or ""),
        account_id=str(claims.get("account_id") or ""),
        chatgpt_user_id=str(claims.get("chatgpt_user_id") or ""),
        email=str(claims.get("email") or ""),
        plan_type=str(claims.get("plan_type") or "free"),
        chatgpt_account_is_fedramp=bool(claims.get("chatgpt_account_is_fedramp") or False),
    )



def _config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def _canonical_ed25519_private_key_b64(private_key_pkcs8_b64: str) -> str:
    try:
        private_key = load_der_private_key(base64.b64decode(private_key_pkcs8_b64, validate=True), password=None)
    except Exception as exc:
        raise RuntimeError("Sub2API 导出的 Agent Identity 私钥不是有效 PKCS8 DER") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise RuntimeError("Sub2API 导出的 Agent Identity 私钥不是 Ed25519")
    canonical = private_key.private_bytes(
        encoding=Encoding.DER,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    return base64.b64encode(canonical).decode()


def _agent_identity_candidates(origin: str, token: str, *, timeout: int) -> list[tuple[int, int, str, str, str]]:
    from platforms.chatgpt.sub2api_upload import _request_json

    candidates: list[tuple[int, int, str, str, str]] = []
    for page in range(1, 21):
        data = _request_json(
            origin,
            (
                "/api/v1/admin/accounts"
                f"?page={page}&page_size=100&platform=openai&type=oauth"
                "&sort_by=created_at&sort_order=desc"
            ),
            token=token,
            timeout=timeout,
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict) or item.get("status") != "active":
                continue
            credentials = item.get("credentials")
            status = item.get("credentials_status")
            if not isinstance(credentials, dict) or not isinstance(status, dict):
                continue
            runtime_id = str(credentials.get("agent_runtime_id") or "").strip()
            task_id = str(credentials.get("task_id") or "").strip()
            account_id = str(credentials.get("chatgpt_account_id") or "").strip()
            user_id = str(credentials.get("chatgpt_user_id") or "").strip()
            if (
                credentials.get("auth_mode") != "agentIdentity"
                or not runtime_id
                or not task_id
                or not account_id
                or not user_id
                or status.get("has_agent_private_key") is not True
            ):
                continue
            try:
                sub2api_account_id = int(item.get("id"))
            except Exception:
                continue
            if sub2api_account_id > 0:
                candidates.append((1, sub2api_account_id, runtime_id, account_id, user_id))
        pages = int(data.get("pages") or page) if isinstance(data, dict) else page
        if page >= pages or not items:
            break
    return candidates


def recover_agent_runtime_from_sub2api(
    *,
    chatgpt_account_id: str,
    chatgpt_user_id: str,
    api_url: str | None = None,
    email: str | None = None,
    password: str | None = None,
    timeout: int = 30,
) -> RecoveredAgentRuntime:
    from platforms.chatgpt.sub2api_upload import _request_json, login_sub2api

    api_url = api_url or _config_value("sub2api_url")
    email = email or _config_value("sub2api_email")
    password = password if password not in (None, "") else _config_value("sub2api_password")
    origin, admin_token = login_sub2api(api_url or "", email or "", password or "", timeout=timeout)

    candidates = _agent_identity_candidates(origin, admin_token, timeout=timeout)
    target_account_id = str(chatgpt_account_id or "").strip()
    target_user_id = str(chatgpt_user_id or "").strip()
    scored = []
    for _priority, account_row_id, runtime_id, source_account_id, source_user_id in candidates:
        exact = source_account_id == target_account_id and source_user_id == target_user_id
        scored.append((0 if exact else 1, account_row_id, runtime_id, source_account_id, source_user_id))
    scored.sort(key=lambda item: item[0])

    for _priority, account_row_id, listed_runtime_id, listed_account_id, listed_user_id in scored:
        exported = _request_json(
            origin,
            f"/api/v1/admin/accounts/data?ids={account_row_id}&include_proxies=false",
            token=admin_token,
            timeout=timeout,
        )
        accounts = exported.get("accounts", []) if isinstance(exported, dict) else []
        if len(accounts) != 1 or not isinstance(accounts[0], dict):
            continue
        account = accounts[0]
        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            continue
        runtime_id = str(credentials.get("agent_runtime_id") or "").strip()
        private_key = str(credentials.get("agent_private_key") or "").strip()
        if (
            account.get("platform") != "openai"
            or account.get("type") != "oauth"
            or credentials.get("auth_mode") != "agentIdentity"
            or runtime_id != listed_runtime_id
            or str(credentials.get("chatgpt_account_id") or "") != listed_account_id
            or str(credentials.get("chatgpt_user_id") or "") != listed_user_id
            or not private_key
        ):
            continue
        return RecoveredAgentRuntime(
            agent_runtime_id=runtime_id,
            private_key_pkcs8_b64=_canonical_ed25519_private_key_b64(private_key),
        )

    raise RuntimeError("Sub2API 账号池里没有可复用的旧 AgentRuntime + Ed25519 私钥")

def create_codex_agent_identity(
    access_token: str,
    *,
    verify_task: bool = True,
    timeout: int = 15,
    recover_from_sub2api: bool = True,
    sub2api_url: str | None = None,
    sub2api_email: str | None = None,
    sub2api_password: str | None = None,
) -> dict[str, Any]:
    session = get_session_from_access_token(access_token)
    account_id = str(session["accountId"] or "")
    chatgpt_user_id = str(session["userId"] or "")
    if not account_id or not chatgpt_user_id:
        raise RuntimeError(f"JWT 缺少必要字段: account_id={account_id}, user_id={chatgpt_user_id}")

    private_key_b64, public_key_ssh = generate_ed25519_keypair()
    task_id = ""
    try:
        agent_runtime_id = register_agent(access_token, public_key_ssh, timeout=timeout)
        if verify_task:
            task_id = register_task(access_token, agent_runtime_id, private_key_b64, timeout=timeout)
    except AgentRegistryNotEnabledError:
        if not recover_from_sub2api:
            raise
        recovered = recover_agent_runtime_from_sub2api(
            chatgpt_account_id=account_id,
            chatgpt_user_id=chatgpt_user_id,
            api_url=sub2api_url,
            email=sub2api_email,
            password=sub2api_password,
            timeout=timeout,
        )
        agent_runtime_id = recovered.agent_runtime_id
        private_key_b64 = recovered.private_key_pkcs8_b64
        # 新链路：OpenAI 拒绝新 Runtime 时，复用旧 Runtime/私钥，但必须注册当前账号的新 task。
        task_id = register_task(access_token, agent_runtime_id, private_key_b64, timeout=timeout)
    return generate_auth_json(
        agent_runtime_id=agent_runtime_id,
        private_key_pkcs8_b64=private_key_b64,
        account_id=account_id,
        chatgpt_user_id=chatgpt_user_id,
        email=str(session.get("email") or ""),
        plan_type=str(session.get("planType") or "free"),
        task_id=task_id,
        chatgpt_account_is_fedramp=False,
    )
