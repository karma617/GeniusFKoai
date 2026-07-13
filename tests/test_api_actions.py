from __future__ import annotations

from api import task_commands as api_task_commands
from platforms.chatgpt.plugin import ChatGPTPlatform


def test_chatgpt_account_state_action_is_sync_and_returns_data_without_task(client, monkeypatch):
    created = client.post(
        "/api/accounts",
        json={
            "platform": "chatgpt",
            "email": "state-sync@test.com",
            "password": "Secret123!",
            "credentials": {"access_token": "access-token"},
        },
    ).json()

    def fake_query_state(self, account, params):
        return {
            "ok": True,
            "data": {
                "valid": True,
                "membership_type": "plus",
                "remote_user": {"email": account.email},
            },
        }

    monkeypatch.setattr(ChatGPTPlatform, "_handle_query_state", fake_query_state)

    actions_resp = client.get("/api/actions/chatgpt")
    state_action = next(
        item for item in actions_resp.json()["actions"] if item["id"] == "get_account_state"
    )

    resp = client.post(
        f"/api/actions/chatgpt/{created['id']}/get_account_state",
        json={"params": {}},
    )
    payload = resp.json()

    assert state_action["sync"] is True
    assert resp.status_code == 200
    assert payload["sync"] is True
    assert payload["ok"] is True
    assert payload["data"]["membership_type"] == "plus"
    assert "task_id" not in payload


def test_refresh_session_task_endpoint_creates_task(client, monkeypatch):
    captured = {}

    def fake_create_refresh_session_task(payload):
        captured["payload"] = payload
        return {"task_id": "task-refresh-session"}

    monkeypatch.setattr(
        api_task_commands.command_service,
        "create_refresh_session_task",
        fake_create_refresh_session_task,
    )

    resp = client.post(
        "/api/tasks/refresh-session",
        json={"platform": "chatgpt", "ids": [11, 12], "concurrency": 2},
    )

    assert resp.status_code == 200
    assert resp.json()["task_id"] == "task-refresh-session"
    assert captured["payload"] == {"platform": "chatgpt", "ids": [11, 12], "concurrency": 2}
