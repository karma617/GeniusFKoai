from core.account_display import build_account_display_summary
from domain.accounts import AccountRecord
from infrastructure.accounts_repository import _matches_tag_filter


def _account(*, lifecycle_status="registered", overview=None):
    return AccountRecord(
        id=1,
        platform="chatgpt",
        email="fixture@example.com",
        password="",
        lifecycle_status=lifecycle_status,
        display_status=lifecycle_status,
        overview=overview or {},
    )


def test_agent_identity_status_exposes_ai_generated_badge_and_filter():
    account = _account(lifecycle_status="agent_identity_uploaded")

    summary = build_account_display_summary(
        platform=account.platform,
        email=account.email,
        lifecycle_status=account.lifecycle_status,
        validity_status=account.validity_status,
        plan_state=account.plan_state,
        plan_name=account.plan_name,
        display_status=account.display_status,
        overview=account.overview,
    )

    assert any(badge["label"] == "AI已生成" for badge in summary["badges"])
    assert _matches_tag_filter(account, "AI已生成") is True
    assert _matches_tag_filter(account, "PLUS") is False


def test_uploaded_overview_status_exposes_ai_generated_badge_and_filter():
    account = _account(overview={"agent_identity_upload_status": "uploaded"})

    summary = build_account_display_summary(
        platform=account.platform,
        email=account.email,
        lifecycle_status=account.lifecycle_status,
        validity_status=account.validity_status,
        plan_state=account.plan_state,
        plan_name=account.plan_name,
        display_status=account.display_status,
        overview=account.overview,
    )

    assert any(badge["label"] == "AI已生成" for badge in summary["badges"])
    assert _matches_tag_filter(account, "ai已生成") is True


def test_failed_agent_identity_upload_does_not_expose_ai_generated_tag():
    account = _account(overview={"agent_identity_upload_status": "failed"})

    summary = build_account_display_summary(
        platform=account.platform,
        email=account.email,
        lifecycle_status=account.lifecycle_status,
        validity_status=account.validity_status,
        plan_state=account.plan_state,
        plan_name=account.plan_name,
        display_status=account.display_status,
        overview=account.overview,
    )

    assert not any(badge["label"] == "AI已生成" for badge in summary["badges"])
    assert _matches_tag_filter(account, "AI已生成") is False
