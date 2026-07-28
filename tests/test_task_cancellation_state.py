from core.db import TaskModel
from application.tasks import (
    TASK_STATUS_CANCEL_REQUESTED,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_RUNNING,
    _request_cancel_mutation,
    serialize_task,
)


def _task(status: str, current: int = 0, total: int = 1) -> TaskModel:
    return TaskModel(
        id="task-test",
        type="momo_trial_probe",
        platform="chatgpt",
        status=status,
        progress_current=current,
        progress_total=total,
    )


def test_serialize_completed_cancel_requested_as_cancelled():
    task = _task(TASK_STATUS_CANCEL_REQUESTED, current=228, total=228)

    data = serialize_task(task)

    assert data["status"] == TASK_STATUS_CANCELLED
    assert data["terminal"] is True
    assert data["cancellable"] is False


def test_serialize_incomplete_cancel_requested_is_not_cancellable():
    task = _task(TASK_STATUS_CANCEL_REQUESTED, current=1, total=2)

    data = serialize_task(task)

    assert data["status"] == TASK_STATUS_CANCEL_REQUESTED
    assert data["terminal"] is False
    assert data["cancellable"] is False


def test_cancel_requested_complete_can_be_closed_by_cancel_again():
    task = _task(TASK_STATUS_CANCEL_REQUESTED, current=2, total=2)

    _request_cancel_mutation(task)

    assert task.status == TASK_STATUS_CANCELLED
    assert task.finished_at is not None


def test_running_cancel_request_moves_to_cancel_requested():
    task = _task(TASK_STATUS_RUNNING, current=0, total=2)

    _request_cancel_mutation(task)

    assert task.status == TASK_STATUS_CANCEL_REQUESTED
    assert task.finished_at is None
