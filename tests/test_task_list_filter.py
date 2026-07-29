from __future__ import annotations

from application import tasks as tasks_module


def test_list_tasks_filters_by_task_type():
    tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={},
    )
    tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_GOPAY_PAY_CHATGPT,
        platform="chatgpt",
        payload={},
    )

    result = tasks_module.list_tasks(task_type=tasks_module.TASK_TYPE_REGISTER)

    assert result["total"] == 1
    assert result["items"][0]["type"] == tasks_module.TASK_TYPE_REGISTER
