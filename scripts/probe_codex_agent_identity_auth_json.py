from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platforms.chatgpt.codex_agent_identity import (
    create_codex_agent_identity,
    generate_auth_json_from_agent_identity_jwt,
)


DEFAULT_EMAIL = "TeriGertrude87@hotmail.com"


def _load_json(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_access_token(db_path: Path, email: str) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        account = conn.execute(
            "select id from accounts where lower(email)=lower(?) and platform='chatgpt' limit 1",
            (email,),
        ).fetchone()
        if not account:
            raise RuntimeError(f"本地账号不存在: {email}")
        account_id = int(account["id"])
        credential = conn.execute(
            """
            select value from account_credentials
            where account_id=? and key='access_token'
            order by is_primary desc, id desc
            limit 1
            """,
            (account_id,),
        ).fetchone()
        if credential and str(credential["value"] or "").strip():
            return str(credential["value"]).strip()
        overview = conn.execute(
            "select summary_json from account_overviews where account_id=?",
            (account_id,),
        ).fetchone()
        summary = _load_json(str(overview["summary_json"] or "")) if overview else {}
        session = summary.get("session") if isinstance(summary.get("session"), dict) else {}
        token = str(session.get("accessToken") or session.get("access_token") or "").strip()
        if token:
            return token
        raise RuntimeError(f"本地账号缺少 access_token: {email}")
    finally:
        conn.close()


def _redact_auth_json(auth_json: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(auth_json, ensure_ascii=False))
    agent_identity = redacted.get("agent_identity")
    if isinstance(agent_identity, dict):
        key = str(agent_identity.get("agent_private_key") or "")
        if key:
            agent_identity["agent_private_key"] = f"{key[:10]}...{key[-6:]}(len={len(key)})"
    return redacted


def main() -> int:
    parser = argparse.ArgumentParser(description="从本地账号 AT 探测生成 Codex Agent Identity auth.json")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--db", default="account_manager.db")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--output", default="")
    parser.add_argument("--agent-identity-jwt", default="")
    parser.add_argument("--agent-identity-jwt-file", default="")
    parser.add_argument("--no-verify-task", action="store_true")
    args = parser.parse_args()

    try:
        agent_identity_jwt = str(args.agent_identity_jwt or "").strip()
        if args.agent_identity_jwt_file:
            agent_identity_jwt = Path(args.agent_identity_jwt_file).read_text(encoding="utf-8").strip()
        if agent_identity_jwt:
            auth_json = generate_auth_json_from_agent_identity_jwt(agent_identity_jwt)
        else:
            access_token = _load_access_token(Path(args.db), args.email)
            auth_json = create_codex_agent_identity(
                access_token,
                verify_task=not args.no_verify_task,
                timeout=args.timeout,
            )
    except Exception as exc:
        print(f"生成失败: {exc}")
        return 2

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(auth_json, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入: {output}")
    print(json.dumps(_redact_auth_json(auth_json), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
