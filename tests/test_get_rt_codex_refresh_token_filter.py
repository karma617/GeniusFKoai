"""获取rt任务 Codex RT 过滤语义 focused tests。

- 安卓协议注册账号自带的注册 refresh_token 不算 Codex RT：
  单轮/目标模式过滤与 create_get_rt_task 任务创建阶段都应放行。
- 出现 rt_pending_upload / rt_uploaded 状态、overview oauth / codex_oauth
  摘要或 rt_acquired_at 等运行时标记时，视为已有 Codex RT 并跳过。
- 普通账号存在任意 refresh_token 仍按原行为跳过；无 RT 账号可处理。
- 数据库使用 tmp_path 隔离 sqlite 引擎（SQLModel.metadata.create_all）。
"""
import json

import pytest
from sqlmodel import Session, SQLModel

import application.tasks as tasks_module
from application.tasks import (
    _filter_get_rt_target_ids,
    _filter_registered_get_rt_ids,
    _get_rt_graph_has_codex_refresh_token,
    _get_rt_graph_has_refresh_token,
    create_get_rt_task,
)
from core.db import (
    AccountCredentialModel,
    AccountModel,
    AccountOverviewModel,
    TaskModel,
    create_configured_engine,
)

ANDROID_LEGACY_EXTRA = {
    "registration_mode": "protocol",
    "registration_mode_label": "安卓协议",
    "registration_protocol_variant": "android",
}


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    engine = create_configured_engine(
        "sqlite:///" + str(tmp_path / "get_rt_codex_filter.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(tasks_module, "engine", engine)
    return engine


def _create_account(
    engine,
    *,
    email,
    lifecycle_status="registered",
    legacy_extra=None,
    summary_updates=None,
    refresh_token=None,
):
    """按注册持久化形态创建账号：overview summary + 平台 credentials。"""
    summary = {
        "platform": "chatgpt",
        "lifecycle_status": lifecycle_status,
        "display_status": lifecycle_status,
    }
    if legacy_extra:
        summary["legacy_extra"] = dict(legacy_extra)
    if summary_updates:
        summary.update(summary_updates)
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email=email, password="pw")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id)
        overview = AccountOverviewModel(
            account_id=account_id,
            lifecycle_status=lifecycle_status,
            display_status=lifecycle_status,
            remote_email=email,
        )
        overview.set_summary(summary)
        session.add(overview)
        if refresh_token:
            session.add(
                AccountCredentialModel(
                    account_id=account_id,
                    scope="platform",
                    provider_name="chatgpt",
                    credential_type="token",
                    key="refresh_token",
                    value=refresh_token,
                    is_primary=False,
                    source="test",
                )
            )
        session.commit()
    return account_id


def _graph(*, legacy_extra=None, overview_updates=None, refresh_token="registration-rt"):
    overview = {
        "platform": "chatgpt",
        "lifecycle_status": "registered",
        "display_status": "registered",
    }
    if legacy_extra is not None:
        overview["legacy_extra"] = dict(legacy_extra)
    if overview_updates:
        overview.update(overview_updates)
    return {
        "overview": overview,
        "credentials": [{"key": "refresh_token", "value": refresh_token}] if refresh_token else [],
        "lifecycle_status": overview["lifecycle_status"],
        "display_status": overview["display_status"],
    }


def test_no_refresh_token_is_never_codex_rt():
    graph = _graph(refresh_token=None)
    graph["overview"]["lifecycle_status"] = "rt_pending_upload"
    graph["overview"]["codex_oauth"] = {"type": "codex"}
    assert _get_rt_graph_has_refresh_token(graph) is False
    assert _get_rt_graph_has_codex_refresh_token(graph) is False


def test_non_android_account_with_refresh_token_keeps_original_behavior():
    graph = _graph(legacy_extra={"registration_protocol_variant": "web"})
    assert _get_rt_graph_has_refresh_token(graph) is True
    assert _get_rt_graph_has_codex_refresh_token(graph) is True

    graph_no_variant = _graph()
    graph_no_variant["overview"].pop("legacy_extra", None)
    assert _get_rt_graph_has_codex_refresh_token(graph_no_variant) is True


def test_android_registration_refresh_token_alone_is_not_codex_rt():
    graph = _graph(legacy_extra=ANDROID_LEGACY_EXTRA)
    assert _get_rt_graph_has_refresh_token(graph) is True
    assert _get_rt_graph_has_codex_refresh_token(graph) is False


@pytest.mark.parametrize(
    ("variant_key", "variant_value"),
    [
        (key, value)
        for key in ("registration_protocol_variant", "chatgpt_protocol_variant", "protocol_variant")
        for value in ("android", "android_app", "android_protocol")
    ],
)
def test_android_variant_aliases_detected_in_legacy_extra(variant_key, variant_value):
    graph = _graph(legacy_extra={variant_key: variant_value})
    assert _get_rt_graph_has_codex_refresh_token(graph) is False


@pytest.mark.parametrize(
    "variant_key",
    ["registration_protocol_variant", "chatgpt_protocol_variant", "protocol_variant"],
)
def test_android_variant_aliases_detected_in_overview_top_level(variant_key):
    graph = _graph(overview_updates={variant_key: "android"})
    graph["overview"].pop("legacy_extra", None)
    assert _get_rt_graph_has_codex_refresh_token(graph) is False


@pytest.mark.parametrize(
    "overview_updates",
    [
        {"lifecycle_status": "rt_pending_upload", "display_status": "rt_pending_upload"},
        {"display_status": "rt_uploaded"},
        {"oauth": {"type": "codex"}},
        {"codex_oauth": {"type": "codex", "refreshed_at": "2025-01-01T00:00:00Z"}},
        {"rt_acquired_at": "2025-01-01T00:00:00Z"},
        {"authorized_at": "2025-01-01T00:00:00Z"},
        {"rt_upload_status": "pending_upload"},
        {"rt_uploaded_at": "2025-01-01T00:00:00Z"},
        {"token_backup_path": "C:/tmp/token.json"},
    ],
)
def test_android_account_with_codex_markers_counts_as_codex_rt(overview_updates):
    graph = _graph(legacy_extra=ANDROID_LEGACY_EXTRA, overview_updates=overview_updates)
    if overview_updates.get("lifecycle_status"):
        graph["lifecycle_status"] = overview_updates["lifecycle_status"]
    if overview_updates.get("display_status"):
        graph["display_status"] = overview_updates["display_status"]
    assert _get_rt_graph_has_codex_refresh_token(graph) is True


def test_android_registration_rt_allowed_in_both_filters(isolated_db):
    account_id = _create_account(
        isolated_db,
        email="android-rt@example.com",
        legacy_extra=ANDROID_LEGACY_EXTRA,
        refresh_token="registration-rt",
    )
    allowed, skipped = _filter_registered_get_rt_ids([account_id], platform="chatgpt")
    assert allowed == [account_id]
    assert skipped == []
    allowed, skipped = _filter_get_rt_target_ids([account_id], platform="chatgpt")
    assert allowed == [account_id]
    assert skipped == []


def test_android_account_with_codex_rt_skipped_by_both_filters(isolated_db):
    account_id = _create_account(
        isolated_db,
        email="android-codex@example.com",
        lifecycle_status="rt_uploaded",
        legacy_extra=ANDROID_LEGACY_EXTRA,
        summary_updates={"rt_uploaded_at": "2025-01-01T00:00:00Z", "rt_upload_status": "uploaded"},
        refresh_token="codex-rt",
    )
    allowed, skipped = _filter_registered_get_rt_ids([account_id], platform="chatgpt")
    assert allowed == []
    assert skipped == [account_id]
    allowed, skipped = _filter_get_rt_target_ids([account_id], platform="chatgpt")
    assert allowed == []
    assert skipped == [account_id]


def test_android_codex_rt_pending_upload_target_mode_allows_reupload(isolated_db):
    account_id = _create_account(
        isolated_db,
        email="android-pending@example.com",
        lifecycle_status="rt_pending_upload",
        legacy_extra=ANDROID_LEGACY_EXTRA,
        summary_updates={"rt_acquired_at": "2025-01-01T00:00:00Z"},
        refresh_token="codex-rt",
    )
    allowed, skipped = _filter_registered_get_rt_ids([account_id], platform="chatgpt")
    assert allowed == []
    assert skipped == [account_id]
    allowed, skipped = _filter_get_rt_target_ids([account_id], platform="chatgpt")
    assert allowed == [account_id]
    assert skipped == []


def test_regular_account_with_refresh_token_skipped(isolated_db):
    account_id = _create_account(
        isolated_db,
        email="web-rt@example.com",
        legacy_extra={"registration_protocol_variant": "web"},
        refresh_token="web-rt",
    )
    allowed, skipped = _filter_registered_get_rt_ids([account_id], platform="chatgpt")
    assert allowed == []
    assert skipped == [account_id]
    allowed, skipped = _filter_get_rt_target_ids([account_id], platform="chatgpt")
    assert allowed == []
    assert skipped == [account_id]


def test_accounts_without_refresh_token_allowed(isolated_db):
    android_id = _create_account(
        isolated_db,
        email="android-empty@example.com",
        legacy_extra=ANDROID_LEGACY_EXTRA,
    )
    web_id = _create_account(isolated_db, email="web-empty@example.com")
    allowed, skipped = _filter_registered_get_rt_ids([android_id, web_id], platform="chatgpt")
    assert allowed == [android_id, web_id]
    assert skipped == []
    allowed, skipped = _filter_get_rt_target_ids([android_id, web_id], platform="chatgpt")
    assert allowed == [android_id, web_id]
    assert skipped == []


def test_create_get_rt_task_single_mode_keeps_android_registration_rt(isolated_db):
    android_id = _create_account(
        isolated_db,
        email="android-create@example.com",
        legacy_extra=ANDROID_LEGACY_EXTRA,
        refresh_token="registration-rt",
    )
    web_id = _create_account(
        isolated_db,
        email="web-create@example.com",
        legacy_extra={"registration_protocol_variant": "web"},
        refresh_token="web-rt",
    )
    created = create_get_rt_task({"ids": [android_id, web_id], "platform": "chatgpt"})
    with Session(isolated_db) as session:
        task = session.get(TaskModel, created["task_id"])
        payload = json.loads(task.payload_json)
    assert payload["ids"] == [android_id]
    assert payload["skipped_get_rt_ineligible_ids"] == [web_id]
    assert payload["skipped_non_registered_ids"] == [web_id]


def test_create_get_rt_task_target_mode_skips_android_with_codex_rt(isolated_db):
    android_id = _create_account(
        isolated_db,
        email="android-target@example.com",
        legacy_extra=ANDROID_LEGACY_EXTRA,
        refresh_token="registration-rt",
    )
    codex_id = _create_account(
        isolated_db,
        email="android-codex-target@example.com",
        lifecycle_status="rt_uploaded",
        legacy_extra=ANDROID_LEGACY_EXTRA,
        summary_updates={"rt_uploaded_at": "2025-01-01T00:00:00Z"},
        refresh_token="codex-rt",
    )
    created = create_get_rt_task(
        {"ids": [android_id, codex_id], "platform": "chatgpt", "task_mode": "target"}
    )
    with Session(isolated_db) as session:
        task = session.get(TaskModel, created["task_id"])
        payload = json.loads(task.payload_json)
    assert payload["ids"] == [android_id]
    assert payload["skipped_get_rt_ineligible_ids"] == [codex_id]
