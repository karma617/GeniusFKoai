from __future__ import annotations

from types import SimpleNamespace

from application import tasks as tasks_module


def test_auto_upload_sub2api_skips_registered_only_account(monkeypatch):
    """仅注册账号无 refresh_token，自动上传应跳过且不调用 SUB2API。"""

    class FakeConfigStore:
        def get(self, key, default=""):
            if key == "sub2api_url":
                return "https://sub.example"
            return default

    logs: list[str] = []

    monkeypatch.setattr(tasks_module, "config_store", FakeConfigStore(), raising=False)
    monkeypatch.setattr(
        "core.config_store.config_store",
        FakeConfigStore(),
        raising=False,
    )

    account = SimpleNamespace(
        platform="chatgpt",
        email="user@example.com",
        user_id="acct-1",
        token="access-token",
        extra={"access_token": "access-token"},
    )
    logger = SimpleNamespace(log=lambda message, level="info": logs.append(str(message)))

    tasks_module._auto_upload_sub2api(logger, account)

    assert any("仅注册状态不上传" in item for item in logs)
