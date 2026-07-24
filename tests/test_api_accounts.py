"""Account CRUD endpoint tests."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from application.account_exports import AccountExportsService
from application.phone_binding import PhoneBindingService, SmsApiPhoneCallback, parse_phone_bind_lines
from core.base_platform import Account, AccountStatus
from core.db import save_account
from domain.accounts import AccountCreateCommand, AccountExportSelection
from infrastructure.accounts_repository import AccountsRepository


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _create_account(client, **overrides):
    payload = {
        "platform": "chatgpt",
        "email": "test@example.com",
        "password": "TestPass123!",
        **overrides,
    }
    return client.post("/api/accounts", json=payload)


def test_create_account(client):
    resp = _create_account(client)
    assert resp.status_code == 200
    data = resp.json()
    assert data["platform"] == "chatgpt"
    assert data["email"] == "test@example.com"
    assert "id" in data


def test_list_accounts_empty(client):
    resp = client.get("/api/accounts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


def test_list_accounts_after_create(client):
    _create_account(client)
    resp = client.get("/api/accounts")
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["email"] == "test@example.com"


def test_get_account_by_id(client):
    create_resp = _create_account(client)
    account_id = create_resp.json()["id"]
    resp = client.get(f"/api/accounts/{account_id}")
    assert resp.status_code == 200
    assert resp.json()["email"] == "test@example.com"


def test_get_account_not_found(client):
    resp = client.get("/api/accounts/99999")
    assert resp.status_code == 404


def test_delete_account(client):
    create_resp = _create_account(client)
    account_id = create_resp.json()["id"]
    del_resp = client.delete(f"/api/accounts/{account_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["ok"] is True
    # Verify it's gone
    get_resp = client.get(f"/api/accounts/{account_id}")
    assert get_resp.status_code == 404


def test_delete_invalid_and_banned_accounts_only_current_platform(client):
    invalid_id = _create_account(
        client,
        email="delete-invalid@test.com",
        lifecycle_status="invalid",
    ).json()["id"]
    banned_id = _create_account(
        client,
        email="delete-banned@test.com",
        lifecycle_status="banned",
    ).json()["id"]
    registered_id = _create_account(
        client,
        email="keep-registered@test.com",
        lifecycle_status="registered",
    ).json()["id"]
    other_platform_id = _create_account(
        client,
        platform="cursor",
        email="keep-cursor-invalid@test.com",
        lifecycle_status="invalid",
    ).json()["id"]

    resp = client.delete("/api/accounts/invalid-and-banned", params={"platform": "chatgpt"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["deleted"] == 2
    assert set(payload["deleted_ids"]) == {invalid_id, banned_id}
    assert client.get(f"/api/accounts/{invalid_id}").status_code == 404
    assert client.get(f"/api/accounts/{banned_id}").status_code == 404
    assert client.get(f"/api/accounts/{registered_id}").status_code == 200
    assert client.get(f"/api/accounts/{other_platform_id}").status_code == 200


def test_update_account(client):
    create_resp = _create_account(client)
    account_id = create_resp.json()["id"]
    patch_resp = client.patch(
        f"/api/accounts/{account_id}",
        json={"password": "NewPass456!"},
    )
    assert patch_resp.status_code == 200


def test_filter_accounts_by_platform(client):
    _create_account(client, platform="chatgpt", email="a@test.com")
    _create_account(client, platform="cursor", email="b@test.com")
    resp = client.get("/api/accounts", params={"platform": "cursor"})
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["platform"] == "cursor"


def test_filter_accounts_by_tag(client):
    _create_account(client, platform="chatgpt", email="bugfree@test.com", overview={"chips": ["BUGFREE"]})
    _create_account(client, platform="chatgpt", email="sticky-bugfree@test.com", overview={"chips": ["Free"], "bugfree": True})
    _create_account(client, platform="chatgpt", email="plus@test.com", lifecycle_status="subscribed")
    _create_account(client, platform="chatgpt", email="free@test.com")

    bugfree_resp = client.get("/api/accounts", params={"platform": "chatgpt", "tag": "BUGFREE"})
    plus_resp = client.get("/api/accounts", params={"platform": "chatgpt", "tag": "PLUS"})
    bugfree_items = bugfree_resp.json()["items"]
    sticky_item = next(item for item in bugfree_items if item["email"] == "sticky-bugfree@test.com")
    sticky_badges = [badge["label"] for badge in sticky_item["display_summary"]["badges"]]

    assert {item["email"] for item in bugfree_items} == {"bugfree@test.com", "sticky-bugfree@test.com"}
    assert "BUGFREE" in sticky_badges
    assert [item["email"] for item in plus_resp.json()["items"]] == ["plus@test.com"]


def test_registration_mode_badge_and_tag_filter(client):
    _create_account(
        client,
        platform="chatgpt",
        email="protocol-mode@test.com",
        overview={"registration_mode_label": "协议模式", "registration_mode": "protocol"},
    )
    _create_account(
        client,
        platform="chatgpt",
        email="headless-mode@test.com",
        overview={"registration_mode_label": "无头浏览器", "registration_mode": "headless_browser"},
    )

    list_resp = client.get("/api/accounts", params={"platform": "chatgpt", "tag": "协议模式"})
    items = list_resp.json()["items"]
    badges = [badge["label"] for badge in items[0]["display_summary"]["badges"]]

    assert [item["email"] for item in items] == ["protocol-mode@test.com"]
    assert "协议模式" in badges


def test_codex_probe_401_warning_has_business_message(client):
    _create_account(
        client,
        platform="chatgpt",
        email="codex-401@test.com",
        overview={"valid": True, "codex_usage_error": "openai codex probe returned status 401:"},
    )

    resp = client.get("/api/accounts", params={"platform": "chatgpt"})
    warnings = [
        warning
        for warning in resp.json()["items"][0]["display_summary"]["warnings"]
        if warning["key"] == "codex_usage_error"
    ]

    assert warnings[0]["key"] == "codex_usage_error"
    assert "Codex 探测返回 401" in warnings[0]["message"]
    assert "不等同于账号失效" in warnings[0]["message"]


def test_export_accounts_by_tag_filter(client):
    _create_account(client, platform="chatgpt", email="bugfree@test.com", overview={"chips": ["Free"], "bugfree": True})
    _create_account(client, platform="chatgpt", email="free@test.com")

    resp = client.post(
        "/api/accounts/export/json",
        json={"platform": "chatgpt", "select_all": True, "tag_filter": "BUGFREE"},
    )

    assert resp.status_code == 200
    assert [item["email"] for item in resp.json()] == ["bugfree@test.com"]


def test_list_accounts_paginates_before_serializing_current_page(client):
    first = _create_account(client, platform="chatgpt", email="first@test.com").json()
    second = _create_account(client, platform="chatgpt", email="second@test.com").json()
    third = _create_account(client, platform="chatgpt", email="third@test.com").json()

    resp = client.get(
        "/api/accounts",
        params={"platform": "chatgpt", "page": 2, "page_size": 1},
    )

    data = resp.json()
    assert data["total"] == 3
    assert [item["id"] for item in data["items"]] == [second["id"]]
    assert third["id"] > second["id"] > first["id"]


def test_account_stats(client):
    _create_account(client)
    resp = client.get("/api/accounts/stats")
    assert resp.status_code == 200


def test_export_kiro_go(client):
    # Create a kiro account first
    client.post("/api/accounts", json={
        "platform": "kiro",
        "email": "kiro@test.com",
        "password": "",
    })
    resp = client.post("/api/accounts/export/kiro-go", json={
        "platform": "kiro",
        "select_all": True,
    })
    assert resp.status_code == 200
    assert "kiro_go_config" in resp.headers.get("content-disposition", "")


def test_export_any2api_multi_platform(client):
    client.post("/api/accounts", json={"platform": "kiro", "email": "k@test.com", "password": ""})
    client.post("/api/accounts", json={"platform": "grok", "email": "g@test.com", "password": ""})
    client.post("/api/accounts", json={"platform": "cursor", "email": "c@test.com", "password": ""})
    resp = client.post("/api/accounts/export/any2api", json={"select_all": True})
    assert resp.status_code == 200
    assert "any2api_admin" in resp.headers.get("content-disposition", "")


def test_export_cpa_uses_standard_payload_schema():
    exp_timestamp = 1777166030
    expected_expired = datetime.fromtimestamp(
        exp_timestamp, tz=timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    access_token = _make_jwt({
        "exp": exp_timestamp,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-standard",
        },
    })
    id_token = _make_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-standard",
        },
    })
    repository = AccountsRepository()
    repository.create(
        AccountCreateCommand(
            platform="chatgpt",
            email="cpa@test.com",
            password="TestPass123!",
            user_id="acct-standard",
            credentials={
                "access_token": access_token,
                "refresh_token": "rt_standard",
                "id_token": id_token,
            },
        )
    )
    service = AccountExportsService(repository)

    artifact = service.export_chatgpt_cpa(AccountExportSelection(platform="chatgpt", select_all=True))
    payload = json.loads(artifact.content)
    assert list(payload.keys()) == [
        "access_token",
        "account_id",
        "email",
        "expired",
        "id_token",
        "last_refresh",
        "refresh_token",
        "type",
    ]
    assert payload["access_token"] == access_token
    assert payload["account_id"] == "acct-standard"
    assert payload["email"] == "cpa@test.com"
    assert payload["expired"] == expected_expired
    assert payload["id_token"] == id_token
    assert payload["last_refresh"].endswith("+08:00")
    assert payload["refresh_token"] == "rt_standard"
    assert payload["type"] == "codex"


def test_export_cpa_falls_back_to_stored_user_id_for_account_id():
    repository = AccountsRepository()
    repository.create(
        AccountCreateCommand(
            platform="chatgpt",
            email="fallback@test.com",
            password="TestPass123!",
            user_id="acct-from-user-id",
            credentials={
                "access_token": _make_jwt({"exp": 1777166030}),
                "refresh_token": "rt_fallback",
            },
        )
    )
    service = AccountExportsService(repository)

    artifact = service.export_chatgpt_cpa(AccountExportSelection(platform="chatgpt", select_all=True))
    payload = json.loads(artifact.content)
    assert payload["account_id"] == "acct-from-user-id"
    assert payload["refresh_token"] == "rt_fallback"


def test_export_email_api_txt_uses_selected_chatgpt_accounts():
    repository = AccountsRepository()
    created = repository.create(
        AccountCreateCommand(
            platform="chatgpt",
            email="mailapi@test.com",
            password="TestPass123!",
        )
    )
    repository.create(
        AccountCreateCommand(
            platform="chatgpt",
            email="other@test.com",
            password="TestPass123!",
        )
    )
    service = AccountExportsService(repository)

    artifact = service.export_chatgpt_email_api_txt(
        AccountExportSelection(platform="chatgpt", ids=[created.id])
    )

    assert artifact.media_type == "text/plain"
    assert artifact.filename.endswith(".txt")
    assert artifact.content == (
        "mailapi@test.com "
        "https://hsxhome.com/api/find/openai?email=mailapi@test.com&t=fzKIywnF4KEGGB_i"
    )


def test_export_cockpit_uses_flat_codex_token_schema(client):
    access_token = _make_jwt({
        "exp": 1777166030,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-cockpit",
        },
    })
    id_token = _make_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-cockpit",
        },
    })
    create_resp = client.post(
        "/api/accounts",
        json={
            "platform": "chatgpt",
            "email": "cockpit@test.com",
            "password": "TestPass123!",
            "user_id": "acct-cockpit",
            "credentials": {
                "access_token": access_token,
                "refresh_token": "rt_cockpit",
                "id_token": id_token,
            },
        },
    )
    account_id = create_resp.json()["id"]

    resp = client.post(
        "/api/accounts/export/cockpit",
        json={"platform": "chatgpt", "ids": [account_id]},
    )

    assert resp.status_code == 200
    assert "cockpit" in resp.headers.get("content-disposition", "")
    payload = resp.json()
    assert payload == {
        "type": "codex",
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": "rt_cockpit",
        "account_id": "acct-cockpit",
        "last_refresh": payload["last_refresh"],
        "email": "cockpit@test.com",
        "expired": payload["expired"],
        "account_note": "",
    }
    assert payload["expired"].endswith("Z")


def test_parse_phone_bind_lines_accepts_multiple_phone_api_pairs():
    entries = parse_phone_bind_lines(
        "\n".join(
            [
                "7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc",
                "+17857019647 ---- https://mail-api.yuecheng.shop/api/sms/recordText?key=def",
            ]
        )
    )

    assert [entry.phone for entry in entries] == ["+17857019646", "+17857019647"]
    assert entries[0].sms_api.endswith("key=abc")


def test_sms_api_phone_callback_returns_phone_then_unique_codes(monkeypatch):
    entry = parse_phone_bind_lines(
        "7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc"
    )[0]
    calls: list[set[str]] = []

    def fake_fetch(phone_entry, *, excluded_pins=None):
        calls.append(set(excluded_pins or set()))
        return "123456" if not excluded_pins else "654321"

    import application.phone_binding as phone_binding_module

    monkeypatch.setattr(phone_binding_module, "_fetch_phone_sms_code", fake_fetch)
    callback = SmsApiPhoneCallback(entry)

    assert callback() == "+17857019646"
    assert callback() == "123456"
    assert callback() == "654321"
    assert calls == [set(), {"123456"}]


def test_phone_binding_marks_selected_accounts_and_reports_phone_usage():
    repository = AccountsRepository()
    first = repository.create(
        AccountCreateCommand(
            platform="chatgpt",
            email="bind1@test.com",
            password="TestPass123!",
            overview={"plan": "plus"},
        )
    )
    second = repository.create(
        AccountCreateCommand(
            platform="chatgpt",
            email="bind2@test.com",
            password="TestPass123!",
            overview={"plan": "plus"},
        )
    )
    calls: list[tuple[str, str]] = []

    def fake_binder(account, phone_entry):
        calls.append((account.email, phone_entry.phone))
        return {"ok": True}

    service = PhoneBindingService(repository=repository, binder=fake_binder)
    result = service.bind(
        ids=[first.id, second.id],
        phone_lines="7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc",
    )

    assert result["total"] == 2
    assert result["success_count"] == 2
    assert result["failure_count"] == 0
    assert result["phones"][0]["phone"] == "+17857019646"
    assert result["phones"][0]["used"] == 2
    assert calls == [
        ("bind1@test.com", "+17857019646"),
        ("bind2@test.com", "+17857019646"),
    ]
    updated = repository.get(first.id)
    assert updated is not None
    assert updated.overview["phone_binding"]["status"] == "bound"
    assert updated.overview["phone_binding"]["phone"] == "+17857019646"


def test_phone_binding_persists_auth_tokens_returned_by_binder():
    repository = AccountsRepository()
    account = repository.create(
        AccountCreateCommand(
            platform="chatgpt",
            email="authbind@test.com",
            password="TestPass123!",
            overview={"plan": "plus"},
            credentials={"access_token": "old_access"},
        )
    )

    def fake_binder(account, phone_entry):
        return {
            "ok": True,
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "id_token": "new_id",
            "account_id": "acct-from-auth",
        }

    service = PhoneBindingService(repository=repository, binder=fake_binder)
    result = service.bind(
        ids=[account.id],
        phone_lines="7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc",
    )

    assert result["success_count"] == 1
    updated = repository.get(account.id)
    assert updated is not None
    credentials = {
        item["key"]: item["value"]
        for item in updated.credentials
        if item.get("key") in {"access_token", "refresh_token", "id_token", "account_id"}
    }
    assert credentials["access_token"] == "new_access"
    assert credentials["refresh_token"] == "new_refresh"
    assert credentials["id_token"] == "new_id"
    assert credentials["account_id"] == "acct-from-auth"
    assert updated.user_id == "acct-from-auth"


def test_phone_binding_accepts_uncapped_concurrency():
    repository = AccountsRepository()
    accounts = [
        repository.create(
            AccountCreateCommand(
                platform="chatgpt",
                email=f"concurrent{index}@test.com",
                password="TestPass123!",
                overview={"plan": "plus"},
            )
        )
        for index in range(2)
    ]
    calls: list[int] = []

    def fake_binder(account, phone_entry):
        calls.append(account.id)
        return {"ok": True}

    service = PhoneBindingService(repository=repository, binder=fake_binder)
    result = service.bind(
        ids=[item.id for item in accounts],
        phone_lines="\n".join(
            [
                "7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc",
                "7857019647----https://mail-api.yuecheng.shop/api/sms/recordText?key=def",
            ]
        ),
        concurrency=99,
    )

    assert sorted(calls) == sorted(item.id for item in accounts)
    assert result["success_count"] == 2
    assert result["concurrency"] == 2


def test_phone_binding_uses_fallback_unbound_accounts_when_no_ids_selected():
    repository = AccountsRepository()
    bound = repository.create(
        AccountCreateCommand(
            platform="chatgpt",
            email="already@test.com",
            password="TestPass123!",
            overview={
                "plan": "plus",
                "phone_binding": {"status": "bound", "phone": "+10000000000"},
            },
        )
    )
    unbound = repository.create(
        AccountCreateCommand(
            platform="chatgpt",
            email="unbound@test.com",
            password="TestPass123!",
            overview={"plan": "plus"},
        )
    )
    calls: list[str] = []

    def fake_binder(account, phone_entry):
        calls.append(account.email)
        return {"ok": True}

    service = PhoneBindingService(repository=repository, binder=fake_binder)
    result = service.bind(
        ids=[],
        fallback_ids=[bound.id, unbound.id],
        phone_lines="7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc",
    )

    assert result["target_ids"] == [unbound.id]
    assert result["success_count"] == 1
    assert calls == ["unbound@test.com"]


def test_phone_binding_rejects_accounts_over_phone_capacity():
    repository = AccountsRepository()
    accounts = [
        repository.create(
            AccountCreateCommand(
                platform="chatgpt",
                email=f"capacity{index}@test.com",
                password="TestPass123!",
                overview={"plan": "plus"},
            )
        )
        for index in range(4)
    ]
    service = PhoneBindingService(repository=repository, binder=lambda account, phone_entry: {"ok": True})

    try:
        service.bind(
            ids=[item.id for item in accounts],
            phone_lines="7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc",
        )
    except ValueError as exc:
        assert "capacity" in str(exc)
    else:
        raise AssertionError("expected capacity error")


def test_phone_bind_api_returns_batch_result(client, monkeypatch):
    class FakePhoneBindingService:
        def bind(self, **kwargs):
            return {
                "total": 1,
                "success_count": 1,
                "failure_count": 0,
                "target_ids": kwargs["ids"],
                "phones": [{"phone": "+17857019646", "used": 1, "success": 1, "failed": 0}],
                "results": [],
            }

    import api.accounts as accounts_api

    monkeypatch.setattr(accounts_api, "phone_binding_service", FakePhoneBindingService())
    resp = client.post(
        "/api/accounts/phone-bind",
        json={
            "ids": [123],
            "fallback_ids": [],
            "phone_lines": "7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["success_count"] == 1


def test_batch_status_update_marks_chatgpt_rt_states(client):
    first = _create_account(client, email="batch-rt-1@test.com").json()
    second = _create_account(client, email="batch-rt-2@test.com").json()
    ids = [first["id"], second["id"]]

    pending_resp = client.post(
        "/api/accounts/batch-status",
        json={
            "platform": "chatgpt",
            "ids": ids,
            "lifecycle_status": "rt_pending_upload",
        },
    )

    assert pending_resp.status_code == 200
    assert pending_resp.json()["updated_ids"] == ids
    for account_id in ids:
        detail = client.get(f"/api/accounts/{account_id}").json()
        assert detail["lifecycle_status"] == "rt_pending_upload"
        assert detail["display_status"] == "rt_pending_upload"
        assert detail["overview"]["rt_upload_status"] == "pending_upload"
        assert detail["overview"]["rt_acquired_at"]

    uploaded_resp = client.post(
        "/api/accounts/batch-status",
        json={
            "platform": "chatgpt",
            "ids": ids,
            "lifecycle_status": "rt_uploaded",
        },
    )

    assert uploaded_resp.status_code == 200
    for account_id in ids:
        detail = client.get(f"/api/accounts/{account_id}").json()
        assert detail["lifecycle_status"] == "rt_uploaded"
        assert detail["display_status"] == "rt_uploaded"
        assert detail["overview"]["rt_upload_status"] == "uploaded"
        assert detail["overview"]["rt_uploaded_at"]


def test_batch_status_update_can_reset_to_registered_without_dropping_credentials(client):
    created = _create_account(
        client,
        email="batch-registered@test.com",
        credentials={"refresh_token": "rt_keep"},
    ).json()
    account_id = created["id"]

    client.post(
        "/api/accounts/batch-status",
        json={
            "platform": "chatgpt",
            "ids": [account_id],
            "lifecycle_status": "rt_uploaded",
        },
    )
    resp = client.post(
        "/api/accounts/batch-status",
        json={
            "platform": "chatgpt",
            "ids": [account_id],
            "lifecycle_status": "registered",
        },
    )

    assert resp.status_code == 200
    detail = client.get(f"/api/accounts/{account_id}").json()
    assert detail["lifecycle_status"] == "registered"
    assert detail["display_status"] == "registered"
    assert "rt_upload_status" not in detail["overview"]
    credentials = {item["key"]: item["value"] for item in detail["credentials"]}
    assert credentials["refresh_token"] == "rt_keep"


def test_chatgpt_registered_account_persists_full_session_for_copy(client):
    session_payload = {
        "user": {"email": "session-copy@test.com"},
        "accessToken": "access-token-from-session",
        "expires": "2026-06-19T00:00:00.000Z",
    }
    save_account(
        Account(
            platform="chatgpt",
            email="session-copy@test.com",
            password="TestPass123!",
            user_id="acct-session-copy",
            token="access-token-from-session",
            status=AccountStatus.REGISTERED,
            extra={
                "account_id": "acct-session-copy",
                "access_token": "access-token-from-session",
                "session": session_payload,
            },
        )
    )

    resp = client.get("/api/accounts", params={"platform": "chatgpt"})

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["overview"]["session"] == session_payload
    assert "session" not in dict(item["overview"].get("legacy_extra") or {})


def test_chatgpt_registered_account_persists_k12_session_for_copy(client):
    k12_session_payload = {
        "user": {"email": "k12-session-copy@test.com"},
        "accessToken": "k12-access-token-from-session",
        "expires": "2026-06-19T00:00:00.000Z",
    }
    save_account(
        Account(
            platform="chatgpt",
            email="k12-session-copy@test.com",
            password="TestPass123!",
            user_id="acct-k12-session-copy",
            token="k12-access-token-from-session",
            status=AccountStatus.REGISTERED,
            extra={
                "account_id": "acct-k12-session-copy",
                "access_token": "k12-access-token-from-session",
                "k12_workspace_id": "workspace-k12-copy",
                "k12_session": k12_session_payload,
            },
        )
    )

    resp = client.get("/api/accounts", params={"platform": "chatgpt"})

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["overview"]["k12_session"] == k12_session_payload
    assert item["overview"]["k12_workspace_id"] == "workspace-k12-copy"
    legacy_extra = dict(item["overview"].get("legacy_extra") or {})
    assert "k12_session" not in legacy_extra
    assert "k12_workspace_id" not in legacy_extra


def test_batch_status_update_rejects_invalid_request(client):
    account = _create_account(client, email="batch-invalid@test.com").json()

    empty_resp = client.post(
        "/api/accounts/batch-status",
        json={"platform": "chatgpt", "ids": [], "lifecycle_status": "registered"},
    )
    assert empty_resp.status_code == 400

    platform_resp = client.post(
        "/api/accounts/batch-status",
        json={"platform": "cursor", "ids": [account["id"]], "lifecycle_status": "registered"},
    )
    assert platform_resp.status_code == 400

    status_resp = client.post(
        "/api/accounts/batch-status",
        json={"platform": "chatgpt", "ids": [account["id"]], "lifecycle_status": "not_a_status"},
    )
    assert status_resp.status_code == 400


def test_ctf_gpt_plus_export_status_marks_accounts(client):
    create_resp = client.post(
        "/api/accounts",
        json={
            "platform": "chatgpt",
            "email": "exported@test.com",
            "password": "TestPass123!",
            "overview": {"plan": "plus"},
        },
    )
    account_id = create_resp.json()["id"]

    resp = client.post(
        "/api/accounts/ctf-gpt-plus/export-status",
        json={"ids": [account_id], "exported": True},
    )

    assert resp.status_code == 200
    assert resp.json()["updated_ids"] == [account_id]
    detail = client.get(f"/api/accounts/{account_id}").json()
    assert detail["overview"]["ctf_gpt_plus"]["exported"] is True
    assert detail["overview"]["ctf_gpt_plus"]["exported_at"]


def test_codex_oauth_complete_url_updates_tokens(client):
    create_resp = client.post(
        "/api/accounts",
        json={
            "platform": "chatgpt",
            "email": "oauth@test.com",
            "password": "TestPass123!",
            "overview": {"plan": "plus"},
        },
    )
    account_id = create_resp.json()["id"]
    callback_url = (
        "http://localhost:1455/auth/callback#"
        "access_token=at_new&refresh_token=rt_new&id_token=id_new&"
        "account_id=acct_new&email=oauth@test.com&expired=2026-06-06T03:47:30.000Z"
    )

    resp = client.post(
        f"/api/accounts/{account_id}/codex-oauth/complete",
        json={"callback_url": callback_url},
    )

    assert resp.status_code == 200
    detail = client.get(f"/api/accounts/{account_id}").json()
    credentials = {
        item["key"]: item["value"]
        for item in detail["credentials"]
        if item["key"] in {"access_token", "refresh_token", "id_token", "account_id"}
    }
    assert credentials == {
        "access_token": "at_new",
        "refresh_token": "rt_new",
        "id_token": "id_new",
        "account_id": "acct_new",
    }
    assert detail["user_id"] == "acct_new"


def test_codex_oauth_start_uses_existing_oauth_generator(client):
    create_resp = client.post(
        "/api/accounts",
        json={
            "platform": "chatgpt",
            "email": "fixed-oauth@test.com",
            "password": "TestPass123!",
            "overview": {"plan": "plus"},
        },
    )
    account_id = create_resp.json()["id"]

    resp = client.post(f"/api/accounts/{account_id}/codex-oauth/start")

    assert resp.status_code == 200
    data = resp.json()
    assert data["auth_url"].startswith("https://auth.openai.com/oauth/authorize?")
    assert "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in data["auth_url"]
    assert "codex_cli_simplified_flow=true" in data["auth_url"]
    assert "code_challenge=" in data["auth_url"]
    assert data["state"]
