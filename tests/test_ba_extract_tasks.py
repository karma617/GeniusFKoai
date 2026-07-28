from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlmodel import Session

from core.account_graph import patch_account_graph
from core.db import AccountModel, engine
from application import ba_extract_tasks as task_mod


@pytest.fixture(autouse=True)
def _reset_ba_extract_task_manager():
    task_mod.get_ba_extract_task_manager()._tasks.clear()


def _create_account(client, email: str, token: str):
    resp = client.post(
        "/api/accounts",
        json={
            "platform": "chatgpt",
            "email": email,
            "password": "Pass123!",
            "primary_token": token,
            "overview": {"plan_state": "free"},
            "credentials": {"access_token": token},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()



def _persist_ba_extract_state(account_id: int, **updates):
    with Session(engine) as session:
        model = session.get(AccountModel, int(account_id))
        assert model is not None
        patch_account_graph(session, model, summary_updates=updates)
        session.commit()


def test_ba_extract_background_tasks_are_isolated(client, monkeypatch):
    account_a = _create_account(client, "ba-a@example.com", "token-a")
    account_b = _create_account(client, "ba-b@example.com", "token-b")
    calls = []

    def fake_extract_ba_link(*, email, progress_cb=None, **kwargs):
        calls.append(email)
        if progress_cb:
            progress_cb({"type": "progress", "step": 1, "total": 7, "desc": f"start {email}", "attempt": 1})
        return {"ok": True, "ba_token": f"BA-{email.split('@')[0].replace('-', '').upper()}123456", "ba_url": f"https://paypal.test/{email}"}

    monkeypatch.setattr(task_mod, "extract_ba_link", fake_extract_ba_link)

    resp_a = client.post(f"/api/pp-plus/accounts/{account_a['id']}/extract-ba-task", json={"max_attempts": 2})
    resp_b = client.post(f"/api/pp-plus/accounts/{account_b['id']}/extract-ba-task", json={"max_attempts": 2})
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    task_a = resp_a.json()["task"]
    task_b = resp_b.json()["task"]
    assert task_a["task_id"] != task_b["task_id"]

    deadline = time.time() + 5
    status_a = {}
    status_b = {}
    while time.time() < deadline:
        status_a = client.get(f"/api/pp-plus/accounts/{account_a['id']}/extract-ba-task").json()["task"]
        status_b = client.get(f"/api/pp-plus/accounts/{account_b['id']}/extract-ba-task").json()["task"]
        if status_a.get("status") == "success" and status_b.get("status") == "success":
            break
        time.sleep(0.05)

    assert status_a["status"] == "success"
    assert status_b["status"] == "success"
    assert status_a["ba_token"].startswith("BA-BAA")
    assert status_b["ba_token"].startswith("BA-BAB")
    assert any("ba-a@example.com" in line for line in status_a["logs"])
    assert any("ba-b@example.com" in line for line in status_b["logs"])
    assert not any("ba-b@example.com" in line for line in status_a["logs"])
    assert not any("ba-a@example.com" in line for line in status_b["logs"])
    assert set(calls) == {"ba-a@example.com", "ba-b@example.com"}


def test_ba_extract_success_logs_region_combo(client, monkeypatch):
    account = _create_account(client, "ba-region@example.com", "token-region")

    def fake_extract_ba_link(**kwargs):
        return {
            "ok": True,
            "ba_token": "BA-REGION123456",
            "ba_url": "https://paypal.test/region",
            "billing_country": "BA",
            "promo_country": "JP",
        }

    monkeypatch.setattr(task_mod, "extract_ba_link", fake_extract_ba_link)

    start = client.post(
        f"/api/pp-plus/accounts/{account['id']}/extract-ba-task",
        json={"billing_country": "BA", "promo_country": "JP", "max_attempts": 1},
    )
    assert start.status_code == 200, start.text

    deadline = time.time() + 5
    latest = {}
    while time.time() < deadline:
        latest = client.get(f"/api/pp-plus/accounts/{account['id']}/extract-ba-task").json()["task"]
        if latest.get("status") == "success":
            break
        time.sleep(0.05)

    assert latest["status"] == "success"
    assert latest["region_combo"] == "BA+JP"
    assert any("BA+JP" in line for line in latest["logs"])
    assert all(line.startswith("[") and "年" in line[:16] and "秒]" in line[:24] for line in latest["logs"])


def test_ba_extract_cancel_finishes_immediately_and_ignores_late_progress(client, monkeypatch):
    account = _create_account(client, "ba-cancel@example.com", "token-cancel")
    entered = threading.Event()
    release = threading.Event()

    def fake_extract_ba_link(*, progress_cb=None, cancel_check=None, **kwargs):
        entered.set()
        if progress_cb:
            progress_cb({"type": "progress", "step": 1, "total": 7, "desc": "开始执行", "attempt": 1})
        release.wait(1)
        if progress_cb:
            progress_cb({"type": "progress", "step": 2, "total": 7, "desc": "终止后的迟到进度", "attempt": 1})
        return {"ok": False, "error": "任务已终止" if callable(cancel_check) and cancel_check() else "late"}

    monkeypatch.setattr(task_mod, "extract_ba_link", fake_extract_ba_link)

    start = client.post(f"/api/pp-plus/accounts/{account['id']}/extract-ba-task", json={"max_attempts": 2})
    assert start.status_code == 200, start.text
    assert entered.wait(2)

    cancel = client.post(f"/api/pp-plus/accounts/{account['id']}/extract-ba-task/cancel", json={})
    assert cancel.status_code == 200, cancel.text
    task = cancel.json()["task"]
    assert task["status"] == "cancelled"
    assert task["stage"] == "任务已终止"

    release.set()
    time.sleep(0.1)
    latest = client.get(f"/api/pp-plus/accounts/{account['id']}/extract-ba-task").json()["task"]
    assert latest["status"] == "cancelled"
    assert not any("迟到进度" in line for line in latest["logs"])


def test_ba_extract_force_restart_after_cancel_uses_new_task_and_new_logs(client, monkeypatch):
    account = _create_account(client, "ba-retry@example.com", "token-retry")
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    def fake_extract_ba_link(*, email, progress_cb=None, cancel_check=None, **kwargs):
        calls.append(email)
        if len(calls) == 1:
            first_entered.set()
            if progress_cb:
                progress_cb({"type": "progress", "step": 1, "total": 7, "desc": "第一次开始", "attempt": 1})
            release_first.wait(1)
            return {"ok": False, "error": "任务已终止"}
        second_entered.set()
        if progress_cb:
            progress_cb({"type": "progress", "step": 1, "total": 7, "desc": "第二次开始", "attempt": 1})
        return {"ok": True, "ba_token": "BA-RETRY123456", "ba_url": "https://paypal.test/retry"}

    monkeypatch.setattr(task_mod, "extract_ba_link", fake_extract_ba_link)

    first = client.post(f"/api/pp-plus/accounts/{account['id']}/extract-ba-task", json={"max_attempts": 2}).json()["task"]
    assert first_entered.wait(2)
    cancel = client.post(f"/api/pp-plus/accounts/{account['id']}/extract-ba-task/cancel", json={})
    assert cancel.json()["task"]["status"] == "cancelled"

    second = client.post(
        f"/api/pp-plus/accounts/{account['id']}/extract-ba-task",
        json={"max_attempts": 2, "force": True},
    ).json()["task"]
    assert second["task_id"] != first["task_id"]
    assert second_entered.wait(2)
    release_first.set()

    deadline = time.time() + 5
    latest = {}
    while time.time() < deadline:
        latest = client.get(f"/api/pp-plus/accounts/{account['id']}/extract-ba-task").json()["task"]
        if latest.get("status") == "success":
            break
        time.sleep(0.05)

    assert latest["status"] == "success"
    assert latest["task_id"] == second["task_id"]
    assert any("第二次开始" in line for line in latest["logs"])
    assert not any("第一次开始" in line for line in latest["logs"])


def test_ba_extract_get_task_settles_orphaned_running_after_restart(client):
    account = _create_account(client, "ba-orphan-get@example.com", "token-orphan-get")
    _persist_ba_extract_state(
        account["id"],
        ba_extract_task_id="orphan-running-task",
        ba_extract_status="running",
        ba_extract_stage="步骤 6/7: poll 2/10",
        ba_extract_step=6,
        ba_extract_total=7,
        ba_extract_attempt=1,
        ba_extract_max_attempts=20,
        ba_extract_logs=["步骤 6/7: poll 2/10"],
    )
    task_mod.get_ba_extract_task_manager()._tasks.clear()

    resp = client.get(f"/api/pp-plus/accounts/{account['id']}/extract-ba-task")

    assert resp.status_code == 200, resp.text
    task = resp.json()["task"]
    assert task["status"] == "error"
    assert task["stage"] == "服务重启后任务已结束，可重新提取"
    assert any("服务重启后任务已结束" in line for line in task["logs"])


def test_ba_extract_cancel_orphaned_running_after_restart_returns_cancelled(client):
    account = _create_account(client, "ba-orphan-cancel@example.com", "token-orphan-cancel")
    _persist_ba_extract_state(
        account["id"],
        ba_extract_task_id="orphan-cancel-task",
        ba_extract_status="running",
        ba_extract_stage="步骤 0/7: 正在终止任务...",
        ba_extract_step=0,
        ba_extract_total=7,
        ba_extract_attempt=0,
        ba_extract_max_attempts=20,
        ba_extract_logs=["步骤 0/7: 正在终止任务..."],
    )
    task_mod.get_ba_extract_task_manager()._tasks.clear()

    resp = client.post(f"/api/pp-plus/accounts/{account['id']}/extract-ba-task/cancel", json={})

    assert resp.status_code == 200, resp.text
    task = resp.json()["task"]
    assert task["status"] == "cancelled"
    assert task["stage"] == "任务已终止"
    assert any("已终止" in line for line in task["logs"])


def test_ba_extract_event_stream_releases_condition_before_each_yield():
    manager = task_mod.BaExtractTaskManager()
    task = task_mod.BaExtractTaskView(task_id="stream-task", account_id=1, status="success")
    with manager._condition:
        manager._tasks[1] = task
        manager._append_event_locked(task, {"type": "done", "ok": True})

    stream = manager.stream_events(1)
    with ThreadPoolExecutor(max_workers=1) as first_executor, ThreadPoolExecutor(max_workers=1) as second_executor:
        event = first_executor.submit(next, stream).result(timeout=1)
        with pytest.raises(StopIteration):
            second_executor.submit(next, stream).result(timeout=1)

    assert event["type"] == "done"
    assert task.status == "success"

