from __future__ import annotations

import json

from core.base_mailbox import MailboxAccount
from core.db import AccountModel, ProviderResourceModel, engine
from core.gmail_oauth_mailbox import GmailOAuthMailbox
from sqlmodel import Session, select


def setup_function():
    GmailOAuthMailbox._ACTIVE_CLAIMS.clear()


def _pool_json(master: str, aliases: list[str]) -> str:
    return json.dumps(
        [
            {
                "master_email": master,
                "credentials_json": {"installed": {"client_id": "client"}},
                "token_json": {"token": "token"},
                "aliases": aliases,
            }
        ]
    )


def test_gmail_oauth_mark_registration_success_updates_provider_resource():
    master = "mother@gmail.com"
    alias = "mother+child@gmail.com"

    with Session(engine) as session:
        account = AccountModel(platform="chatgpt", email=alias, password="Secret123!")
        session.add(account)
        session.commit()
        session.refresh(account)
        resource = ProviderResourceModel(
            account_id=int(account.id),
            provider_type="mailbox",
            provider_name="gmail_oauth_fission",
            resource_type="mailbox",
            resource_identifier=master,
            handle=alias,
            display_name=alias,
        )
        resource.set_metadata({"email": alias, "master_email": master})
        session.add(resource)
        session.commit()

    mailbox = GmailOAuthMailbox(pool_json=_pool_json(master, [alias]))

    applied = mailbox.mark_registration_success(MailboxAccount(email=alias, account_id=master))

    assert applied == ["Gmail子号已注册"]
    with Session(engine) as session:
        resource = session.exec(
            select(ProviderResourceModel).where(ProviderResourceModel.handle == alias)
        ).one()
        metadata = resource.get_metadata()
    assert metadata["registration_status"] == "registered"
    assert metadata["gmail_oauth_registered"] is True


def test_gmail_oauth_skips_registered_provider_resource_alias():
    master = "mother@gmail.com"
    registered_alias = "mother+used@gmail.com"
    free_alias = "mother+free@gmail.com"

    with Session(engine) as session:
        account = AccountModel(platform="chatgpt", email="holder@example.com", password="Secret123!")
        session.add(account)
        session.commit()
        session.refresh(account)
        resource = ProviderResourceModel(
            account_id=int(account.id),
            provider_type="mailbox",
            provider_name="gmail_oauth_fission",
            resource_type="mailbox",
            resource_identifier=master,
            handle=registered_alias,
            display_name=registered_alias,
        )
        resource.set_metadata(
            {
                "email": registered_alias,
                "master_email": master,
                "registration_status": "registered",
                "gmail_oauth_registered": True,
            }
        )
        session.add(resource)
        session.commit()

    mailbox = GmailOAuthMailbox(pool_json=_pool_json(master, [registered_alias, free_alias]))

    assert mailbox._select_email_for_mother(mailbox._mothers[0]) == free_alias


def test_gmail_oauth_claims_alias_during_allocation_to_avoid_parallel_duplicate():
    master = "mother@gmail.com"
    first_alias = "mother+first@gmail.com"
    second_alias = "mother+second@gmail.com"
    mailbox = GmailOAuthMailbox(pool_json=_pool_json(master, [first_alias, second_alias]))

    first = mailbox.get_email()
    second = mailbox.get_email()

    assert first.email != second.email
    assert {first.email, second.email} == {first_alias, second_alias}
    assert first.extra["provider_resource"]["metadata"]["gmail_oauth_claimed"] is True
