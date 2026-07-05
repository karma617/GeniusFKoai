from __future__ import annotations

from sqlmodel import Session

from application.gmail_api_code_usage import gmail_api_code_alias_usage
from core.db import AccountModel, ProviderResourceModel, TaskEventModel, TaskModel, engine


def test_gmail_api_code_alias_usage_counts_success_and_unconfirmed_allocations():
    with Session(engine) as session:
        account = AccountModel(
            platform="chatgpt",
            email="main+done@gmail.com",
            password="Secret123!",
            user_id="main+done@gmail.com",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        resource = ProviderResourceModel(
            account_id=account.id or 0,
            provider_type="mailbox",
            provider_name="gmail_api_code",
            resource_type="mailbox",
            resource_identifier="main@gmail.com",
            handle="main+done@gmail.com",
            display_name="main+done@gmail.com",
        )
        resource.set_metadata({"account_id": "main@gmail.com", "email": "main+done@gmail.com"})
        session.add(resource)
        task = TaskModel(type="register", platform="chatgpt")
        task.id = "task-1"
        task.set_payload({"extra": {"mail_provider": "gmail_api_code"}})
        session.add(task)
        session.add(
            TaskEventModel(
                task_id="task-1",
                message="Email alias allocated: main+pending@gmail.com parent=main@gmail.com aliases=1/5 total=1/6",
            )
        )
        session.commit()

    data = gmail_api_code_alias_usage()
    item = next(item for item in data["items"] if item["parent_email"] == "main@gmail.com")

    assert item["successful_alias_count"] == 1
    assert item["allocated_only_count"] == 1
    assert item["confirmed_remaining"] == 4
    assert item["conservative_remaining"] == 3
    assert item["successful_aliases"] == ["main+done@gmail.com"]
    assert item["allocated_only_aliases"] == ["main+pending@gmail.com"]
    assert data["summary"]["successful_alias_count"] == 1
    assert data["summary"]["allocated_only_count"] == 1


def test_gmail_api_code_alias_usage_marks_invalid_parent_unusable():
    with Session(engine) as session:
        account = AccountModel(
            platform="chatgpt",
            email="invalid@gmail.com",
            password="Secret123!",
            user_id="invalid@gmail.com",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        resource = ProviderResourceModel(
            account_id=account.id or 0,
            provider_type="mailbox",
            provider_name="gmail_api_code",
            resource_type="mailbox",
            resource_identifier="invalid@gmail.com",
            handle="invalid@gmail.com",
            display_name="invalid@gmail.com",
        )
        resource.set_metadata(
            {
                "email": "invalid@gmail.com",
                "registration_status": "invalid",
                "registration_invalid": True,
                "registration_invalid_reason": "user_already_exists",
            }
        )
        session.add(resource)
        session.commit()

    data = gmail_api_code_alias_usage()
    item = next(item for item in data["items"] if item["parent_email"] == "invalid@gmail.com")

    assert item["email_status"] == "unusable"
    assert item["email_status_reason"] == "user_already_exists"
    assert item["confirmed_remaining"] == 0
    assert item["conservative_remaining"] == 0
    assert data["summary"]["unusable_parent_count"] >= 1


def test_gmail_api_code_alias_usage_ignores_non_gmail_parent_noise():
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
