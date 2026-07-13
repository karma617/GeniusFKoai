import sqlite3

from sqlalchemy.exc import OperationalError

import application.tasks as tasks_module


def _locked_error() -> OperationalError:
    return OperationalError("commit", {}, sqlite3.OperationalError("database is locked"))


class FakeSession:
    commit_calls = 0

    def __init__(self, _engine):
        self.items = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def add(self, item):
        self.items.append(item)

    def commit(self):
        FakeSession.commit_calls += 1
        if FakeSession.commit_calls == 1:
            raise _locked_error()

    def refresh(self, _item):
        return None


def test_create_task_retries_sqlite_database_locked(monkeypatch):
    FakeSession.commit_calls = 0
    monkeypatch.setattr(tasks_module, "Session", FakeSession)
    monkeypatch.setattr(tasks_module, "_sleep_db_write_retry", lambda _attempt: None)

    task = tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )

    assert task["type"] == tasks_module.TASK_TYPE_REGISTER
    assert task["status"] == tasks_module.TASK_STATUS_PENDING
    assert FakeSession.commit_calls == 3


def test_append_task_event_retries_sqlite_database_locked(monkeypatch):
    FakeSession.commit_calls = 0
    monkeypatch.setattr(tasks_module, "Session", FakeSession)
    monkeypatch.setattr(tasks_module, "_sleep_db_write_retry", lambda _attempt: None)

    event = tasks_module.append_task_event("task_1", "任务已创建: register", event_type="state")

    assert event["task_id"] == "task_1"
    assert event["type"] == "state"
    assert FakeSession.commit_calls == 2
