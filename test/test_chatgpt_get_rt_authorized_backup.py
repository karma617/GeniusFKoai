from __future__ import annotations

import json
from pathlib import Path

from core.base_platform import Account
from core.account_graph import load_account_graphs
from core.db import AccountCredentialModel, AccountModel, AccountOverviewModel
from infrastructure.platform_runtime import _build_oauth_result_overview
from platforms.chatgpt import plugin as chatgpt_plugin
from sqlmodel import SQLModel, Session, create_engine


def test_oauth_result_overview_marks_rt_pending_upload_when_token_exists():
    overview = _build_oauth_result_overview(
        "chatgpt",
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "email": "user@example.com",
            "token_backup_path": r"E:\AI\GeniusFKoai\data\chatgpt_token_backups\backup.json",
        },
    )

    assert overview is not None
    assert overview["lifecycle_status"] == "rt_pending_upload"
    assert overview["display_status"] == "rt_pending_upload"
    assert overview["valid"] is True
    assert overview["remote_email"] == "user@example.com"
    assert overview["token_backup_path"].endswith("backup.json")
    assert overview["oauth"]["token_backup_path"].endswith("backup.json")


def test_oauth_result_overview_keeps_registered_without_refresh_token():
    overview = _build_oauth_result_overview(
        "chatgpt",
        {
            "access_token": "access-token",
            "email": "user@example.com",
        },
    )

    assert overview is not None
    assert "lifecycle_status" not in overview
    assert "display_status" not in overview
    assert overview["remote_email"] == "user@example.com"


def test_save_get_rt_token_backup_writes_raw_token_json(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(chatgpt_plugin, "_chatgpt_token_backup_dir", lambda: tmp_path)
    account = Account(
        platform="chatgpt",
        email="user@example.com",
        password="secret",
        user_id="acct_local",
    )

    backup_path = chatgpt_plugin._save_get_rt_token_backup(
        account,
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "account_id": "acct_remote",
            "email": "remote@example.com",
        },
        action_label="get_rt",
    )

    assert backup_path
    data = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    assert data["platform"] == "chatgpt"
    assert data["email"] == "remote@example.com"
    assert data["account_id"] == "acct_remote"
    assert data["tokens"]["access_token"] == "access-token"
    assert data["tokens"]["refresh_token"] == "refresh-token"
    assert data["result"]["id_token"] == "id-token"


def test_existing_oauth_token_graph_displays_rt_pending_upload():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        account = AccountModel(platform="chatgpt", email="user@example.com", password="secret")
        session.add(account)
        session.commit()
        session.refresh(account)

        overview = AccountOverviewModel(
            account_id=int(account.id or 0),
            lifecycle_status="registered",
            display_status="registered",
        )
        overview.set_summary({"oauth": {"type": "codex", "email": "user@example.com"}})
        session.add(overview)
        session.add(
            AccountCredentialModel(
                account_id=int(account.id or 0),
                scope="platform",
                provider_name="chatgpt",
                credential_type="token",
                key="refresh_token",
                value="refresh-token",
            )
        )
        session.commit()

        graph = load_account_graphs(session, [int(account.id or 0)])[int(account.id or 0)]

    assert graph["lifecycle_status"] == "rt_pending_upload"
    assert graph["display_status"] == "rt_pending_upload"
    assert graph["validity_status"] == "valid"
    assert graph["overview"]["valid"] is True


def test_existing_access_token_only_graph_stays_registered():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        account = AccountModel(platform="chatgpt", email="user@example.com", password="secret")
        session.add(account)
        session.commit()
        session.refresh(account)

        overview = AccountOverviewModel(
            account_id=int(account.id or 0),
            lifecycle_status="registered",
            display_status="registered",
        )
        overview.set_summary({"oauth": {"type": "codex", "email": "user@example.com"}})
        session.add(overview)
        session.add(
            AccountCredentialModel(
                account_id=int(account.id or 0),
                scope="platform",
                provider_name="chatgpt",
                credential_type="token",
                key="access_token",
                value="access-token",
            )
        )
        session.commit()

        graph = load_account_graphs(session, [int(account.id or 0)])[int(account.id or 0)]

    assert graph["lifecycle_status"] == "registered"
    assert graph["display_status"] == "registered"
