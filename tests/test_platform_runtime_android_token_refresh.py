"""安卓协议 refresh_token 刷新元数据持久化 focused tests。

- _build_android_token_refresh_overview 仅在 platform=chatgpt 且
  token_refresh_source=android_protocol 时产出 overview 更新，
  记录 android_token_refreshed_at / android_token_refresh_status /
  android_token_expires_at / android_token_expires_in /
  android_token_type / android_token_scope（有值才写）。
- 不创建 oauth/codex_oauth、rt_acquired_at、rt_upload_status，
  不改写 lifecycle/display 状态。
- 普通刷新结果（无 token_refresh_source 或其他平台）不写安卓元数据；
  token 凭证仍由现有 PERSISTED_ACTION_DATA_KEYS 路径更新。
- 数据库使用 tmp_path 隔离 sqlite 引擎（SQLModel.metadata.create_all），
  平台插件以 stub 替换，全部无网络。
"""
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, select

import infrastructure.platform_runtime as runtime_module
from core.db import (
    AccountCredentialModel,
    AccountModel,
    AccountOverviewModel,
    create_configured_engine,
)
from domain.actions import ActionExecutionCommand
from infrastructure.platform_runtime import (
    PlatformRuntime,
    _build_android_token_refresh_overview,
)

ANDROID_REFRESH_DATA = {
    "access_token": "android-access-token",
    "refresh_token": "android-refresh-token",
    "token_refresh_source": "android_protocol",
    "expires_at": "2025-06-01T12:00:00Z",
    "expires_in": 3600,
    "token_type": "Bearer",
    "scope": "openid email profile",
}

FORBIDDEN_OVERVIEW_KEYS = (
    "oauth",
    "codex_oauth",
    "rt_acquired_at",
    "rt_upload_status",
    "lifecycle_status",
    "display_status",
    "valid",
)


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    engine = create_configured_engine(
        "sqlite:///" + str(tmp_path / "android_token_refresh.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(runtime_module, "engine", engine)
    return engine


def _create_account(
    engine,
    *,
    email,
    platform="chatgpt",
    lifecycle_status="registered",
    summary_updates=None,
):
    summary = {
        "platform": platform,
        "lifecycle_status": lifecycle_status,
        "display_status": lifecycle_status,
    }
    if summary_updates:
        summary.update(summary_updates)
    with Session(engine) as session:
        model = AccountModel(platform=platform, email=email, password="pw")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id)
        overview = AccountOverviewModel(
            account_id=account_id,
            lifecycle_status=lifecycle_status,
            display_status=lifecycle_status,
        )
        overview.set_summary(summary)
        session.add(overview)
        session.commit()
    return account_id


def _patch_stub_platform(monkeypatch, result):
    class _StubPlatform:
        def __init__(self, config=None):
            self.config = config

        def execute_action(self, action_id, account, params):
            return dict(result)

    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: _StubPlatform)


def _execute_refresh_token(account_id, *, platform="chatgpt"):
    runtime = PlatformRuntime()
    return runtime.execute_action(
        ActionExecutionCommand(
            platform=platform,
            account_id=account_id,
            action_id="refresh_token",
        )
    )


def _load_overview(engine, account_id):
    with Session(engine) as session:
        overview = session.get(AccountOverviewModel, account_id)
        assert overview is not None
        return overview, overview.get_summary()


def _load_credentials(engine, account_id):
    with Session(engine) as session:
        rows = session.exec(
            select(AccountCredentialModel).where(
                AccountCredentialModel.account_id == account_id
            )
        ).all()
        return {row.key: row.value for row in rows}


def test_builder_records_android_refresh_fields():
    before = datetime.now(timezone.utc)
    overview = _build_android_token_refresh_overview("chatgpt", dict(ANDROID_REFRESH_DATA))
    assert overview is not None
    assert overview["platform"] == "chatgpt"
    assert overview["android_token_refresh_status"] == "refreshed"

    refreshed_at = datetime.fromisoformat(
        str(overview["android_token_refreshed_at"]).replace("Z", "+00:00")
    )
    assert refreshed_at.tzinfo is timezone.utc
    assert refreshed_at >= before

    assert overview["android_token_expires_at"] == "2025-06-01T12:00:00Z"
    assert overview["android_token_expires_in"] == 3600
    assert overview["android_token_type"] == "Bearer"
    assert overview["android_token_scope"] == "openid email profile"

    for key in FORBIDDEN_OVERVIEW_KEYS:
        assert key not in overview


def test_builder_omits_absent_optional_fields():
    overview = _build_android_token_refresh_overview(
        "chatgpt",
        {"token_refresh_source": "android_protocol"},
    )
    assert overview is not None
    assert overview["android_token_refresh_status"] == "refreshed"
    assert overview["android_token_refreshed_at"]
    for key in (
        "android_token_expires_at",
        "android_token_expires_in",
        "android_token_type",
        "android_token_scope",
    ):
        assert key not in overview


@pytest.mark.parametrize("source", [None, "", "oauth", "browser", "android"])
def test_builder_ignores_non_android_source(source):
    data = dict(ANDROID_REFRESH_DATA)
    if source is None:
        data.pop("token_refresh_source")
    else:
        data["token_refresh_source"] = source
    assert _build_android_token_refresh_overview("chatgpt", data) is None


@pytest.mark.parametrize("platform", ["cursor", "kiro", "trae"])
def test_builder_ignores_other_platforms(platform):
    assert _build_android_token_refresh_overview(platform, dict(ANDROID_REFRESH_DATA)) is None


def test_builder_ignores_non_dict_data():
    assert _build_android_token_refresh_overview("chatgpt", None) is None


def test_execute_action_persists_android_refresh_metadata(isolated_db, monkeypatch):
    account_id = _create_account(isolated_db, email="android-refresh@example.com")
    _patch_stub_platform(monkeypatch, {"ok": True, "data": dict(ANDROID_REFRESH_DATA)})

    result = _execute_refresh_token(account_id)
    assert result.ok is True

    overview, summary = _load_overview(isolated_db, account_id)
    assert summary["android_token_refresh_status"] == "refreshed"
    assert summary["android_token_refreshed_at"]
    assert summary["android_token_expires_at"] == "2025-06-01T12:00:00Z"
    assert summary["android_token_expires_in"] == 3600
    assert summary["android_token_type"] == "Bearer"
    assert summary["android_token_scope"] == "openid email profile"
    # 持久化摘要由 patch_account_graph 规范化，lifecycle/display 键总会存在，
    # 因此这里只断言安卓刷新不得新增的 RT/OAuth 标记，值层面在下方单独校验。
    for key in ("oauth", "codex_oauth", "rt_acquired_at", "rt_upload_status", "valid"):
        assert key not in summary
    assert overview.lifecycle_status == "registered"
    assert overview.display_status == "registered"
    assert overview.checked_at is not None

    credentials = _load_credentials(isolated_db, account_id)
    assert credentials["access_token"] == "android-access-token"
    assert credentials["refresh_token"] == "android-refresh-token"


def test_execute_action_android_refresh_keeps_rt_upload_state(isolated_db, monkeypatch):
    account_id = _create_account(
        isolated_db,
        email="android-codex@example.com",
        lifecycle_status="rt_uploaded",
        summary_updates={
            "rt_upload_status": "uploaded",
            "rt_uploaded_at": "2025-01-01T00:00:00Z",
            "valid": True,
        },
    )
    _patch_stub_platform(monkeypatch, {"ok": True, "data": dict(ANDROID_REFRESH_DATA)})

    result = _execute_refresh_token(account_id)
    assert result.ok is True

    overview, summary = _load_overview(isolated_db, account_id)
    assert overview.lifecycle_status == "rt_uploaded"
    assert overview.display_status == "rt_uploaded"
    assert overview.validity_status == "valid"
    assert summary["rt_upload_status"] == "uploaded"
    assert summary["rt_uploaded_at"] == "2025-01-01T00:00:00Z"
    assert summary["valid"] is True
    assert summary["android_token_refresh_status"] == "refreshed"
    for key in ("oauth", "codex_oauth", "rt_acquired_at"):
        assert key not in summary


def test_execute_action_ignores_normal_refresh_result(isolated_db, monkeypatch):
    account_id = _create_account(isolated_db, email="web-refresh@example.com")
    _patch_stub_platform(
        monkeypatch,
        {"ok": True, "data": {"access_token": "web-access", "refresh_token": "web-refresh"}},
    )

    result = _execute_refresh_token(account_id)
    assert result.ok is True

    _, summary = _load_overview(isolated_db, account_id)
    assert "android_token_refresh_status" not in summary
    assert "android_token_refreshed_at" not in summary
    credentials = _load_credentials(isolated_db, account_id)
    assert credentials["access_token"] == "web-access"
    assert credentials["refresh_token"] == "web-refresh"


def test_execute_action_normal_refresh_without_credentials_skips_save(isolated_db, monkeypatch):
    account_id = _create_account(isolated_db, email="web-plain@example.com")
    _patch_stub_platform(monkeypatch, {"ok": True, "data": {"message": "刷新成功"}})

    result = _execute_refresh_token(account_id)
    assert result.ok is True

    _, summary = _load_overview(isolated_db, account_id)
    assert set(summary) == {"platform", "lifecycle_status", "display_status"}
    assert _load_credentials(isolated_db, account_id) == {}


def test_execute_action_non_android_platform_refresh_token_no_metadata(isolated_db, monkeypatch):
    account_id = _create_account(isolated_db, email="cursor-refresh@example.com", platform="cursor")
    _patch_stub_platform(monkeypatch, {"ok": True, "data": dict(ANDROID_REFRESH_DATA)})

    result = _execute_refresh_token(account_id, platform="cursor")
    assert result.ok is True

    _, summary = _load_overview(isolated_db, account_id)
    assert "android_token_refresh_status" not in summary
    credentials = _load_credentials(isolated_db, account_id)
    assert credentials["access_token"] == "android-access-token"
    assert credentials["refresh_token"] == "android-refresh-token"
