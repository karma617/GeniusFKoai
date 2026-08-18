from __future__ import annotations

from sqlmodel import Session

import application.gmail_api_code_usage as usage_module
from application.gmail_api_code_usage import gmail_api_code_alias_usage
from core.db import AccountModel, ProviderResourceModel, ProviderSettingModel, TaskEventModel, TaskModel, engine


def test_gmail_api_code_alias_usage_counts_icloud_success_and_unconfirmed_allocations(monkeypatch):
    monkeypatch.setattr(usage_module, "_configured_parent_emails", lambda: ({"main@icloud.com"}, True))

    with Session(engine) as session:
        account = AccountModel(
            platform="chatgpt",
            email="main+done@icloud.com",
            password="Secret123!",
            user_id="main+done@icloud.com",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        resource = ProviderResourceModel(
            account_id=account.id or 0,
            provider_type="mailbox",
            provider_name="gmail_api_code",
            resource_type="mailbox",
            resource_identifier="main@icloud.com",
            handle="main+done@icloud.com",
            display_name="main+done@icloud.com",
        )
        resource.set_metadata({"account_id": "main@icloud.com", "email": "main+done@icloud.com"})
        session.add(resource)
        task = TaskModel(type="register", platform="chatgpt")
        task.id = "task-1"
        task.set_payload({"extra": {"mail_provider": "gmail_api_code"}})
        session.add(task)
        session.add(
            TaskEventModel(
                task_id="task-1",
                message="Email alias allocated: main+pending@icloud.com parent=main@icloud.com aliases=1/1 total=1/1",
            )
        )
        session.commit()

    data = gmail_api_code_alias_usage()
    item = next(item for item in data["items"] if item["parent_email"] == "main@icloud.com")

    assert item["mailbox_type"] == "icloud"
    assert item["alias_limit"] == 2
    assert item["successful_alias_count"] == 1
    assert item["allocated_only_count"] == 1
    assert item["confirmed_remaining"] == 1
    assert item["conservative_remaining"] == 0
    assert item["successful_aliases"] == ["main+done@icloud.com"]
    assert item["allocated_only_aliases"] == ["main+pending@icloud.com"]
    assert data["summary"]["successful_alias_count"] == 1
    assert data["summary"]["allocated_only_count"] == 1
    assert data["summary"]["parent_count"] == 1
    assert data["summary"]["configured_parent_count"] == 1


def test_gmail_api_code_alias_usage_marks_exhausted_parent_registered_without_remaining(monkeypatch):
    monkeypatch.setattr(usage_module, "_configured_parent_emails", lambda: ({"invalid@icloud.com"}, True))

    with Session(engine) as session:
        account = AccountModel(
            platform="chatgpt",
            email="invalid@icloud.com",
            password="Secret123!",
            user_id="invalid@icloud.com",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        resource = ProviderResourceModel(
            account_id=account.id or 0,
            provider_type="mailbox",
            provider_name="gmail_api_code",
            resource_type="mailbox",
            resource_identifier="invalid@icloud.com",
            handle="invalid@icloud.com",
            display_name="invalid@icloud.com",
        )
        resource.set_metadata(
            {
                "email": "invalid@icloud.com",
                "registration_status": "registered_exhausted",
                "registration_alias_exhausted": True,
                "registration_alias_exhausted_reason": "registration_disallowed",
            }
        )
        session.add(resource)
        session.commit()

    data = gmail_api_code_alias_usage()
    item = next(item for item in data["items"] if item["parent_email"] == "invalid@icloud.com")

    assert item["email_status"] == "registered_exhausted"
    assert item["email_status_reason"] == "registration_disallowed"
    assert item["alias_exhausted"] is True
    assert item["confirmed_remaining"] == 0
    assert item["conservative_remaining"] == 0
    assert data["summary"]["registered_parent_count"] >= 1
    assert data["summary"]["parent_count"] == 1


def test_gmail_api_code_alias_usage_keeps_child_remaining_for_registered_icloud(monkeypatch):
    monkeypatch.setattr(usage_module, "_configured_parent_emails", lambda: ({"registered@icloud.com"}, True))
    monkeypatch.setattr(
        usage_module,
        "_configured_parent_statuses",
        lambda: {"registered@icloud.com": ("registered", "pool_registered")},
    )

    data = gmail_api_code_alias_usage()
    item = next(item for item in data["items"] if item["parent_email"] == "registered@icloud.com")

    assert item["email_status"] == "registered"
    assert item["main_registered"] is True
    assert item["successful_alias_count"] == 1
    assert item["confirmed_remaining"] == 1
    assert item["conservative_remaining"] == 1
    assert item["status"] == "available"
    assert data["summary"]["usable_parent_count"] == 1


def test_gmail_api_code_alias_usage_reads_pool_status_markers():
    with Session(engine) as session:
        setting = ProviderSettingModel(
            provider_type="mailbox",
            provider_key="gmail_api_code",
            display_name="API接码邮箱",
            enabled=True,
            is_default=True,
        )
        setting.set_config(
            {
                "gmail_api_code_pool_text": (
                    "# registered used@gmail.com----https://example.test/used\n"
                    "# invalid dead@gmail.com----https://example.test/dead\n"
                    "# deleted old@gmail.com----https://example.test/old\n"
                    "active@gmail.com----https://example.test/active\n"
                    "active@icloud.com----https://example.test/icloud"
                )
            }
        )
        session.add(setting)
        session.commit()

    data = gmail_api_code_alias_usage()
    by_email = {item["parent_email"]: item for item in data["items"]}

    assert by_email["used@gmail.com"]["email_status"] == "registered"
    assert by_email["dead@gmail.com"]["email_status"] == "unusable"
    assert by_email["active@gmail.com"]["email_status"] == "usable"
    assert by_email["active@gmail.com"]["mailbox_type"] == "gmail"
    assert by_email["active@gmail.com"]["alias_limit"] == 1
    assert by_email["active@icloud.com"]["mailbox_type"] == "icloud"
    assert by_email["active@icloud.com"]["alias_limit"] == 2
    assert "old@gmail.com" not in by_email


def test_gmail_api_code_alias_usage_ignores_non_gmail_parent_noise(monkeypatch):
    monkeypatch.setattr(usage_module, "_configured_parent_emails", lambda: ({"main@gmail.com"}, True))

    with Session(engine) as session:
        account = AccountModel(
            platform="chatgpt",
            email="noise-parent+alias@outlook.com",
            password="Secret123!",
            user_id="noise-parent+alias@outlook.com",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        resource = ProviderResourceModel(
            account_id=account.id or 0,
            provider_type="mailbox",
            provider_name="gmail_api_code",
            resource_type="mailbox",
            resource_identifier="noise-parent-9676",
            handle="noise-parent+alias@outlook.com",
            display_name="noise-parent+alias@outlook.com",
        )
        resource.set_metadata({"account_id": "noise-parent-9676", "email": "noise-parent+alias@outlook.com"})
        session.add(resource)
        task = TaskModel(type="register", platform="chatgpt")
        task.id = "noise-task-9676"
        task.set_payload({"extra": {"mail_provider": "gmail_api_code"}})
        session.add(task)
        session.add(
            TaskEventModel(
                task_id="noise-task-9676",
                message=(
                    "Email alias allocated: noise-parent+pending@outlook.com "
                    "parent=noise-parent-9676 aliases=0/5 total=0/6"
                ),
            )
        )
        session.commit()

    data = gmail_api_code_alias_usage()
    parent_emails = {item["parent_email"] for item in data["items"]}

    assert "noise-parent-9676" not in parent_emails
    assert "noise-parent@outlook.com" not in parent_emails


def test_gmail_api_code_alias_usage_ignores_gmail_parent_not_in_current_pool(monkeypatch):
    monkeypatch.setattr(usage_module, "_configured_parent_emails", lambda: ({"current@gmail.com"}, True))

    with Session(engine) as session:
        account = AccountModel(
            platform="chatgpt",
            email="old+done@gmail.com",
            password="Secret123!",
            user_id="old+done@gmail.com",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        resource = ProviderResourceModel(
            account_id=account.id or 0,
            provider_type="mailbox",
            provider_name="gmail_api_code",
            resource_type="mailbox",
            resource_identifier="old@gmail.com",
            handle="old+done@gmail.com",
            display_name="old+done@gmail.com",
        )
        resource.set_metadata({"account_id": "old@gmail.com", "email": "old+done@gmail.com"})
        session.add(resource)
        task = TaskModel(type="register", platform="chatgpt")
        task.id = "old-pool-task"
        task.set_payload({"extra": {"mail_provider": "gmail_api_code"}})
        session.add(task)
        session.add(
            TaskEventModel(
                task_id="old-pool-task",
                message="Email alias allocated: old+pending@gmail.com parent=old@gmail.com aliases=1/5 total=1/6",
            )
        )
        session.commit()

    data = gmail_api_code_alias_usage()
    parent_emails = {item["parent_email"] for item in data["items"]}

    assert parent_emails == {"current@gmail.com"}
    assert data["summary"]["parent_count"] == 1
    assert data["summary"]["successful_alias_count"] == 0
    assert data["summary"]["allocated_only_count"] == 0
    assert data["summary"]["confirmed_remaining"] == usage_module.DIRECT_MAILBOX_LIMIT
    assert data["summary"]["conservative_remaining"] == usage_module.DIRECT_MAILBOX_LIMIT
