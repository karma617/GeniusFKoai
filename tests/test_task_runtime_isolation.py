from __future__ import annotations

from sqlmodel import Session

from application import tasks as tasks_module
from core.db import engine


def _set_task_status(task_id: str, status: str) -> None:
    with Session(engine) as session:
        model = session.get(tasks_module.TaskModel, task_id)
        assert model is not None
        model.status = status
        session.add(model)
        session.commit()


def _task_status(task_id: str) -> str:
    with Session(engine) as session:
        model = session.get(tasks_module.TaskModel, task_id)
        assert model is not None
        return str(model.status)


def test_claim_next_runnable_task_allows_different_type_same_platform():
    running_register = tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 198},
        progress_total=198,
    )
    _set_task_status(running_register["id"], tasks_module.TASK_STATUS_RUNNING)

    pending_health = tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_ACCOUNT_HEALTH_CHECK,
        platform="chatgpt",
        payload={"platform": "chatgpt", "ids": []},
        progress_total=1,
    )

    claimed = tasks_module.claim_next_runnable_task(max_parallel_per_task_group=1)

    assert claimed is not None
    assert claimed["id"] == pending_health["id"]
    assert claimed["task_group"] == "chatgpt:account_health_check"
    assert _task_status(pending_health["id"]) == tasks_module.TASK_STATUS_CLAIMED


def test_claim_next_runnable_task_keeps_same_type_same_platform_queued():
    running_register = tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
        progress_total=1,
    )
    _set_task_status(running_register["id"], tasks_module.TASK_STATUS_RUNNING)

    pending_register = tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
        progress_total=1,
    )

    claimed = tasks_module.claim_next_runnable_task(max_parallel_per_task_group=1)

    assert claimed is None
    assert _task_status(pending_register["id"]) == tasks_module.TASK_STATUS_PENDING
