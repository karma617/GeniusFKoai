"""ChatGPT 邮箱换绑任务与独立配置 focused tests。

- 协议函数 rebind_account_email 全部 mock（_ProtocolStub）。
- 数据库使用 tmp_path 隔离 sqlite 引擎（SQLModel.metadata.create_all）。
- 覆盖：单个/批量成功、协议失败不落库、重复邮箱拒绝、仅 registered 查询、
  配置域名归一与 secret 遮蔽/保留、PUT 部分更新（exclude_unset）、
  Cloudflare provision 能力声明（ready/implemented）。
"""
import json
import re

import pytest
from sqlmodel import Session, SQLModel, select

import application.chatgpt_rebind as rebind_service
import application.tasks as tasks_module
import core.config_store as config_store_module
from api.chatgpt_rebind import MailConfigUpdateRequest
from application.cloudflare_email_routing import CloudflareAPIError
from application.tasks import TaskLogger, _execute_email_rebind_task, create_email_rebind_task
from core.db import (
    AccountCredentialModel,
    AccountModel,
    AccountOverviewModel,
    ProviderResourceModel,
    TaskEventModel,
    TaskModel,
    create_configured_engine,
)

CONFIG = {
    "api_url": "https://mail.example.com",
    "api_token": "token-abc-123456",
    "domains": ["cloud.example.com"],
    "cloudflare_api_token": "cf-token-9999",
    "cloudflare_account_id": "acct-1",
}


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    engine = create_configured_engine(
        "sqlite:///" + str(tmp_path / "email_rebind.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(tasks_module, "engine", engine)
    monkeypatch.setattr(config_store_module, "engine", engine)
    monkeypatch.setattr(rebind_service, "engine", engine)
    return engine


class _ProtocolStub:
    """按账号 email 返回预置结果的 rebind_account_email 替身，并记录调用参数。"""

    def __init__(self):
        self.calls = []
        self.results = {}

    def __call__(self, account, mail_config, *, proxy="", log_fn=None):
        account = account if isinstance(account, dict) else {}
        email = str(account.get("email") or "")
        self.calls.append(
            {
                "email": email,
                "password": str(account.get("password") or ""),
                "extra_keys": sorted(dict(account.get("extra") or {}).keys()),
                "mail_config": dict(mail_config or {}),
                "proxy": proxy,
            }
        )
        result = self.results.get(email)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return {"ok": False, "old_email": email, "error": "no stub result"}
        return dict(result)


@pytest.fixture()
def protocol(monkeypatch):
    import platforms.chatgpt.email_rebind as email_rebind_module

    stub = _ProtocolStub()
    monkeypatch.setattr(email_rebind_module, "rebind_account_email", stub)
    return stub


class _CloudflareStub:
    """CloudflareClient 替身：记录 get_zone/list/enable 调用；enable 可注入错误。"""

    def __init__(self):
        self.calls = []
        self.zone_result = {"id": "zone-1", "name": "", "status": "active"}
        self.existing_mx_records = []
        self.enable_error = None

    def get_zone(self, domain):
        self.calls.append({"op": "get_zone", "domain": domain})
        if self.zone_result is None:
            return None
        return {**self.zone_result, "name": domain}

    def list_dns_records(self, zone_id, *, name, record_type):
        self.calls.append({"op": "list_dns_records", "zone_id": zone_id, "name": name, "record_type": record_type})
        return list(self.existing_mx_records)

    def enable_email_routing_dns(self, zone_id, *, name=""):
        self.calls.append({"op": "enable_email_routing_dns", "zone_id": zone_id, "name": name})
        if self.enable_error is not None:
            raise self.enable_error
        # 模拟 CF onboarding 为子域创建托管 MX（供启用后的复核 GET 命中）
        self.existing_mx_records.append(
            {
                "id": f"rec-{name or zone_id}",
                "type": "MX",
                "name": name,
                "content": "route1.mx.cloudflare.net",
                "priority": 10,
                "ttl": 1,
                "proxied": False,
            }
        )
        return {}


@pytest.fixture(autouse=True)
def cloudflare_stub(monkeypatch):
    """让换绑任务的子域 onboarding 默认幂等成功；记录调用供用例断言。"""
    import application.cloudflare_email_routing as cloudflare_email_routing_module

    stub = _CloudflareStub()
    monkeypatch.setattr(
        cloudflare_email_routing_module,
        "CloudflareClient",
        lambda token, account_id="": stub,
    )
    return stub


def _configure_mail() -> None:
    rebind_service.update_mail_config(
        {
            "domains": CONFIG["domains"][0],
            "api_url": CONFIG["api_url"],
            "api_token": CONFIG["api_token"],
            "cloudflare_api_token": CONFIG["cloudflare_api_token"],
            "cloudflare_account_id": CONFIG["cloudflare_account_id"],
        }
    )


def _create_account(
    engine,
    *,
    email,
    password="pw-old",
    lifecycle_status="registered",
    credentials=None,
    provider_resources=None,
):
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email=email, password=password)
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id)
        session.add(
            AccountOverviewModel(
                account_id=account_id,
                lifecycle_status=lifecycle_status,
                remote_email=email,
            )
        )
        for item in credentials or []:
            session.add(
                AccountCredentialModel(
                    account_id=account_id,
                    scope="platform",
                    provider_name="chatgpt",
                    credential_type=item.get("credential_type", "token"),
                    key=item["key"],
                    value=item["value"],
                    is_primary=item.get("is_primary", False),
                    source="test",
                )
            )
        for item in provider_resources or []:
            session.add(
                ProviderResourceModel(
                    account_id=account_id,
                    provider_type="mailbox",
                    provider_name=item.get("provider_name", "imap_mail"),
                    resource_type="mailbox",
                    resource_identifier=item["resource_identifier"],
                    handle=item.get("handle", item["resource_identifier"]),
                    display_name=item.get("display_name", ""),
                )
            )
        session.commit()
    return account_id


def _add_raw_account(engine, *, platform, email):
    with Session(engine) as session:
        session.add(AccountModel(platform=platform, email=email, password="x"))
        session.commit()


def _account_snapshot(engine, account_id):
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        overview = session.get(AccountOverviewModel, account_id)
        credentials = session.exec(
            select(AccountCredentialModel).where(AccountCredentialModel.account_id == account_id)
        ).all()
        resources = session.exec(
            select(ProviderResourceModel).where(ProviderResourceModel.account_id == account_id)
        ).all()
        return {
            "email": model.email,
            "overview": dict(overview.get_summary()) if overview else {},
            "remote_email": overview.remote_email if overview else "",
            "credentials": sorted((item.key, item.value) for item in credentials),
            "resources": sorted(item.resource_identifier for item in resources),
        }


def _mailbox_resource(email):
    return {
        "provider_type": "mailbox",
        "provider_name": "cloud_mail",
        "resource_type": "mailbox",
        "resource_identifier": email,
        "handle": email,
        "display_name": email,
        "metadata": {"email": email, "api_mode": "cloud_mail"},
    }


def _protocol_success(new_email, old_email):
    return {
        "ok": True,
        "old_email": old_email,
        "new_email": new_email,
        "access_token": "at-new",
        "refresh_token": "rt-new",
        "id_token": "id-new",
        "mailbox_resource": _mailbox_resource(new_email),
    }


def _run_task(engine, payload, *, task_id="task-rebind-test"):
    with Session(engine) as session:
        session.add(
            TaskModel(
                id=task_id,
                type="email_rebind",
                platform="chatgpt",
                status="pending",
                payload_json=json.dumps(payload),
            )
        )
        session.commit()
    _execute_email_rebind_task(payload, TaskLogger(task_id))
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return {
            "status": task.status,
            "result": task.get_result(),
            "success_count": task.success_count,
            "error_count": task.error_count,
        }


def test_config_domains_normalization_and_secret_masking(isolated_db):
    masked = rebind_service.update_mail_config(
        {
            "domains": "Cloud.example.com\n@cloud2.example.com; cloud.example.com, ,",
            "api_url": CONFIG["api_url"],
            "api_token": CONFIG["api_token"],
            "cloudflare_api_token": "cf-token-9999",
            "cloudflare_account_id": "acct-1",
            "forward_to": "me@example.com",
        }
    )
    assert masked["domains"] == ["cloud.example.com", "cloud2.example.com"]
    assert masked["api_token"].startswith("******")
    assert CONFIG["api_token"] not in json.dumps(masked)
    assert masked["cloudflare_api_token"].startswith("******")
    # 前端契约：api_token_masked / cloudflare_api_token_masked 别名
    assert masked["api_token_masked"] == masked["api_token"]
    assert masked["cloudflare_api_token_masked"] == masked["cloudflare_api_token"]
    assert masked["has_api_token"] is True
    assert masked["has_cloudflare_api_token"] is True
    # Cloudflare 自动配置能力已实现（application.cloudflare_email_routing）
    assert masked["provision"]["status"] == "ready"
    assert masked["provision"]["capabilities"]["cloudflare_mx_provision"] == {"implemented": True}
    assert masked["provision"]["capabilities"]["cloudflare_email_routing_provision"] == {"implemented": True}

    # PUT 遮蔽值/空值 -> 保留原 secret；新值 -> 覆盖
    masked_keep = rebind_service.update_mail_config(
        {"api_token": rebind_service.mask_secret(CONFIG["api_token"])}
    )
    assert rebind_service.load_rebind_mail_config()["api_token"] == CONFIG["api_token"]
    assert masked_keep["api_token"] == rebind_service.mask_secret(CONFIG["api_token"])
    rebind_service.update_mail_config({"api_token": "brand-new-token"})
    assert rebind_service.load_rebind_mail_config()["api_token"] == "brand-new-token"
    rebind_service.update_mail_config({"api_token": ""})
    assert rebind_service.load_rebind_mail_config()["api_token"] == "brand-new-token"
    # 单独更新 domains 不丢 secret
    rebind_service.update_mail_config({"domains": "d3.example.com"})
    final = rebind_service.load_rebind_mail_config()
    assert final["domains"] == ["d3.example.com"]
    assert final["api_token"] == "brand-new-token"
    assert final["api_url"] == CONFIG["api_url"]


def test_mail_config_partial_update_preserves_other_fields(isolated_db):
    """API/model 层：仅提交一个字段不清空其他字段（exclude_unset=True）。"""
    rebind_service.update_mail_config(
        {
            "domains": "a.example.com",
            "api_url": "https://mail.example.com",
            "api_token": CONFIG["api_token"],
            "cloudflare_api_token": "cf-token-9999",
            "cloudflare_account_id": "acct-1",
            "cloudflare_worker_name": "cloud-mail-worker",
            "forward_to": "me@example.com",
        }
    )

    # 仅提交 forward_to：model_dump() 不得带出未提交字段的默认空值
    body = MailConfigUpdateRequest.model_validate({"forward_to": "new@example.com"})
    partial = body.model_dump(exclude_unset=True)
    assert set(partial.keys()) == {"forward_to"}
    rebind_service.update_mail_config(partial)

    saved = rebind_service.load_rebind_mail_config()
    assert saved["forward_to"] == "new@example.com"
    assert saved["api_url"] == "https://mail.example.com"
    assert saved["domains"] == ["a.example.com"]
    assert saved["api_token"] == CONFIG["api_token"]
    assert saved["cloudflare_api_token"] == "cf-token-9999"
    assert saved["cloudflare_account_id"] == "acct-1"
    assert saved["cloudflare_worker_name"] == "cloud-mail-worker"

    # 显式提交空值：普通字段清除，secret 保留
    clear_url = MailConfigUpdateRequest.model_validate({"api_url": ""})
    assert set(clear_url.model_dump(exclude_unset=True).keys()) == {"api_url"}
    rebind_service.update_mail_config(clear_url.model_dump(exclude_unset=True))
    assert rebind_service.load_rebind_mail_config()["api_url"] == ""
    assert rebind_service.load_rebind_mail_config()["forward_to"] == "new@example.com"

    keep_secret = MailConfigUpdateRequest.model_validate({"api_token": ""})
    rebind_service.update_mail_config(keep_secret.model_dump(exclude_unset=True))
    assert rebind_service.load_rebind_mail_config()["api_token"] == CONFIG["api_token"]


def test_create_email_rebind_task_validates_ids_and_concurrency(isolated_db):
    with pytest.raises(ValueError):
        create_email_rebind_task({"ids": [], "account_id": 0})

    task = create_email_rebind_task({"ids": [3, 1], "account_id": 2, "concurrency": 99})
    assert task["type"] == "email_rebind"
    assert task["platform"] == "chatgpt"
    assert task["progress_detail"]["total"] == 3
    with Session(isolated_db) as session:
        payload = session.get(TaskModel, task["id"]).get_payload()
    assert payload["ids"] == [3, 1, 2]
    assert payload["account_id"] == 2
    assert payload["concurrency"] == 5

    task_low = create_email_rebind_task({"ids": [9], "concurrency": 0})
    with Session(isolated_db) as session:
        payload_low = session.get(TaskModel, task_low["id"]).get_payload()
    assert payload_low["concurrency"] == 1


def test_single_rebind_success_updates_email_tokens_and_summary(isolated_db, protocol):
    _configure_mail()
    account_id = _create_account(
        isolated_db,
        email="old@example.com",
        credentials=[{"key": "access_token", "value": "at-old", "is_primary": True}],
        provider_resources=[{"resource_identifier": "old@example.com", "provider_name": "imap_mail"}],
    )
    protocol.results["old@example.com"] = _protocol_success("new.cloud@example.com", "old@example.com")

    outcome = _run_task(
        isolated_db,
        {"ids": [account_id], "concurrency": 3, "proxy": "http://127.0.0.1:7890"},
    )

    assert outcome["status"] == "succeeded"
    assert outcome["success_count"] == 1
    assert outcome["error_count"] == 0
    data = outcome["result"]["data"]
    assert data["total"] == 1
    assert data["success_count"] == 1
    assert data["failure_count"] == 0
    assert data["results"][0]["ok"] is True
    assert data["results"][0]["new_email"] == "new.cloud@example.com"

    snapshot = _account_snapshot(isolated_db, account_id)
    assert snapshot["email"] == "new.cloud@example.com"
    assert snapshot["remote_email"] == "new.cloud@example.com"
    assert snapshot["overview"]["email_rebind_previous_email"] == "old@example.com"
    assert snapshot["overview"]["email_rebind_at"]
    cred_map = dict(snapshot["credentials"])
    assert cred_map["access_token"] == "at-new"
    assert cred_map["refresh_token"] == "rt-new"
    assert cred_map["id_token"] == "id-new"
    assert snapshot["resources"] == ["new.cloud@example.com"]

    # 协议调用参数：账号 email/password/extra + 独立配置 + 代理
    call = protocol.calls[0]
    assert call["email"] == "old@example.com"
    assert call["password"] == "pw-old"
    assert "provider_resources" in call["extra_keys"]
    assert call["mail_config"]["api_url"] == CONFIG["api_url"]
    assert call["mail_config"]["api_token"] == CONFIG["api_token"]
    assert call["mail_config"]["domains"] == CONFIG["domains"]
    assert call["proxy"] == "http://127.0.0.1:7890"


def test_batch_rebind_mixed_results(isolated_db, protocol):
    _configure_mail()
    id_ok = _create_account(isolated_db, email="ok@example.com")
    id_fail = _create_account(isolated_db, email="fail@example.com")
    id_conflict = _create_account(isolated_db, email="conflict@example.com")
    _create_account(isolated_db, email="taken@example.com")
    protocol.results["ok@example.com"] = _protocol_success("ok-new.cloud@example.com", "ok@example.com")
    protocol.results["fail@example.com"] = {
        "ok": False,
        "old_email": "fail@example.com",
        "new_email": "",
        "error": "重新登录失败: otp timeout",
    }
    protocol.results["conflict@example.com"] = _protocol_success("taken@example.com", "conflict@example.com")

    outcome = _run_task(isolated_db, {"ids": [id_ok, id_fail, id_conflict], "concurrency": 2})

    assert outcome["status"] == "succeeded"
    assert outcome["success_count"] == 1
    assert outcome["error_count"] == 2
    data = outcome["result"]["data"]
    assert data["total"] == 3
    assert data["success_count"] == 1
    assert data["failure_count"] == 2
    by_account = {item["account_id"]: item for item in data["results"]}
    assert by_account[id_ok]["ok"] is True
    assert by_account[id_fail]["ok"] is False
    assert "重新登录失败" in by_account[id_fail]["error"]
    assert by_account[id_conflict]["ok"] is False
    assert "已被账号" in by_account[id_conflict]["error"]

    assert _account_snapshot(isolated_db, id_ok)["email"] == "ok-new.cloud@example.com"
    assert _account_snapshot(isolated_db, id_fail)["email"] == "fail@example.com"
    conflict_snapshot = _account_snapshot(isolated_db, id_conflict)
    assert conflict_snapshot["email"] == "conflict@example.com"
    assert conflict_snapshot["remote_email"] == "conflict@example.com"
    assert dict(conflict_snapshot["credentials"]).get("refresh_token") is None


def test_protocol_failure_leaves_account_unchanged(isolated_db, protocol):
    _configure_mail()
    account_id = _create_account(
        isolated_db,
        email="old@example.com",
        credentials=[{"key": "access_token", "value": "at-old", "is_primary": True}],
        provider_resources=[{"resource_identifier": "old@example.com"}],
    )
    protocol.results["old@example.com"] = {
        "ok": False,
        "old_email": "old@example.com",
        "new_email": "",
        "error": "change_email/begin 403: forbidden",
    }

    outcome = _run_task(isolated_db, {"ids": [account_id]})

    assert outcome["status"] == "failed"
    assert outcome["success_count"] == 0
    assert outcome["error_count"] == 1
    snapshot = _account_snapshot(isolated_db, account_id)
    assert snapshot["email"] == "old@example.com"
    assert snapshot["remote_email"] == "old@example.com"
    assert dict(snapshot["credentials"]) == {"access_token": "at-old"}
    assert snapshot["resources"] == ["old@example.com"]
    assert "change_email/begin 403" in json.dumps(outcome["result"]["errors"], ensure_ascii=False)


def test_duplicate_new_email_rejected(isolated_db, protocol):
    _configure_mail()
    account_id = _create_account(isolated_db, email="old@example.com")
    other_id = _create_account(isolated_db, email="taken@example.com")
    protocol.results["old@example.com"] = _protocol_success("taken@example.com", "old@example.com")

    outcome = _run_task(isolated_db, {"account_id": account_id})

    assert outcome["success_count"] == 0
    assert outcome["error_count"] == 1
    assert _account_snapshot(isolated_db, account_id)["email"] == "old@example.com"
    assert _account_snapshot(isolated_db, other_id)["email"] == "taken@example.com"
    item = outcome["result"]["data"]["results"][0]
    assert item["ok"] is False
    assert "已被账号" in item["error"]


def test_registered_accounts_listing_filters_and_paginates(isolated_db):
    _create_account(isolated_db, email="a1@example.com", lifecycle_status="registered")
    _create_account(isolated_db, email="a2@example.com", lifecycle_status="invalid")
    _create_account(isolated_db, email="a3@example.com", lifecycle_status="rt_uploaded")
    _create_account(isolated_db, email="a4@example.com", lifecycle_status="banned")
    _add_raw_account(isolated_db, platform="chatgpt", email="a5@example.com")
    _add_raw_account(isolated_db, platform="gopay", email="g@example.com")

    page1 = rebind_service.list_registered_accounts(page=1, page_size=1)
    page2 = rebind_service.list_registered_accounts(page=2, page_size=1)

    assert page1["total"] == 2
    assert page1["page"] == 1
    assert page1["page_size"] == 1
    # 仅 lifecycle_status 精确为 registered 的 chatgpt 账号；id 倒序分页
    assert [item["email"] for item in page1["items"]] == ["a5@example.com"]
    assert page2["page"] == 2
    assert [item["email"] for item in page2["items"]] == ["a1@example.com"]
    for item in page1["items"] + page2["items"]:
        assert item["lifecycle_status"] == "registered"
        # 前端条目契约：status / registered_at / created_at / current_email
        assert item["status"] == "registered"
        assert item["email"] == item["current_email"]
        assert item["registered_at"] == item["created_at"]
        assert item["created_at"]


def test_registered_accounts_email_search_filter(isolated_db):
    _create_account(isolated_db, email="alpha.one@example.com")
    _create_account(isolated_db, email="alpha.two@example.com")
    _create_account(isolated_db, email="beta@example.com")
    _create_account(isolated_db, email="alpha.invalid@example.com", lifecycle_status="invalid")

    result = rebind_service.list_registered_accounts(page=1, page_size=10, email="alpha")
    assert result["total"] == 2
    assert sorted(item["email"] for item in result["items"]) == [
        "alpha.one@example.com",
        "alpha.two@example.com",
    ]
    # 搜索与仅 registered 语义同时生效：匹配但非 registered 的不返回
    miss = rebind_service.list_registered_accounts(page=1, page_size=10, email="alpha.invalid")
    assert miss["total"] == 0
    assert miss["items"] == []
    # 空搜索退化为全量分页
    all_registered = rebind_service.list_registered_accounts(page=1, page_size=10, email="  ")
    assert all_registered["total"] == 3

# ---------------------------------------------------------------------------
# 子域名邮箱分配：配额（每主域名 10 个）、复用、格式、持久化与任务集成
# ---------------------------------------------------------------------------


def test_allocate_rebind_subdomain_quota_reuse_and_format(isolated_db):
    _configure_mail()
    first_sub, first_main = rebind_service.allocate_rebind_subdomain("User1@Example.com")
    assert first_main == "cloud.example.com"
    assert re.fullmatch(r"[a-z0-9]{6,8}", first_sub)
    # 同一账号（大小写不敏感）复用既有分配（任务重试幂等）
    assert rebind_service.allocate_rebind_subdomain("user1@example.com") == (first_sub, first_main)
    # 其余账号分配互不冲突，直到 10/10 满额
    subs = {first_sub}
    for index in range(2, 11):
        sub, main = rebind_service.allocate_rebind_subdomain(f"user{index}@example.com")
        assert main == "cloud.example.com"
        assert sub not in subs
        subs.add(sub)
    with pytest.raises(RuntimeError) as exc_info:
        rebind_service.allocate_rebind_subdomain("user11@example.com")
    assert "主域名 cloud.example.com 子域名配额已用完(10/10)" in str(exc_info.value)
    allocations = rebind_service.load_subdomain_allocations()
    assert len(allocations["cloud.example.com"]) == 10
    assert re.fullmatch(r"[a-z0-9]{6,8}", allocations["cloud.example.com"][0]["subdomain"])


def test_allocate_rebind_subdomain_falls_back_to_next_domain(isolated_db):
    """显式/首个主域名满额时自动选下一个仍有余量的主域名。"""
    rebind_service.update_mail_config({"domains": "full.example.com\nspare.example.com"})
    rebind_service.save_subdomain_allocations(
        {
            "full.example.com": [
                {"subdomain": f"s{index}", "account_email": f"u{index}@x.com", "created_at": ""}
                for index in range(10)
            ]
        }
    )

    sub, main = rebind_service.allocate_rebind_subdomain("fresh@example.com")

    assert main == "spare.example.com"
    assert re.fullmatch(r"[a-z0-9]{6,8}", sub)


def test_task_allocates_subdomain_and_passes_domain(isolated_db, protocol):
    """任务执行：先持久化子域名分配，再以 domain=子域名.主域名 调协议；日志含分配节点。"""
    _configure_mail()
    account_id = _create_account(isolated_db, email="old@example.com")
    protocol.results["old@example.com"] = _protocol_success("moved@later.example.com", "old@example.com")

    outcome = _run_task(isolated_db, {"ids": [account_id]})

    assert outcome["status"] == "succeeded"
    call = protocol.calls[0]
    allocations = rebind_service.load_subdomain_allocations()
    entries = allocations["cloud.example.com"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["account_email"] == "old@example.com"
    assert entry["created_at"]
    expected_domain = entry["subdomain"] + ".cloud.example.com"
    assert call["mail_config"]["domain"] == expected_domain
    assert call["mail_config"]["domains"] == CONFIG["domains"]
    with Session(isolated_db) as session:
        events = session.exec(
            select(TaskEventModel).where(TaskEventModel.task_id == "task-rebind-test")
        ).all()
    messages = " ".join(event.message for event in events)
    assert "已分配子域名: " + expected_domain in messages


def test_task_fails_when_all_domain_quota_exhausted(isolated_db, protocol):
    """全部主域名配额用尽 -> 任务失败，错误含「主域名 X 子域名配额已用完(10/10)」。"""
    _configure_mail()
    rebind_service.save_subdomain_allocations(
        {
            "cloud.example.com": [
                {"subdomain": f"s{index}", "account_email": f"u{index}@x.com", "created_at": ""}
                for index in range(10)
            ]
        }
    )
    account_id = _create_account(isolated_db, email="old@example.com")

    outcome = _run_task(isolated_db, {"ids": [account_id]})

    assert outcome["status"] == "failed"
    assert outcome["error_count"] == 1
    item = outcome["result"]["data"]["results"][0]
    assert item["ok"] is False
    assert "主域名 cloud.example.com 子域名配额已用完(10/10)" in item["error"]
    assert protocol.calls == []
    assert _account_snapshot(isolated_db, account_id)["email"] == "old@example.com"


def test_task_retry_reuses_allocation_after_protocol_failure(isolated_db, protocol):
    """协议失败后再重试：同一账号复用已分配子域名，不新增占用。"""
    _configure_mail()
    account_id = _create_account(isolated_db, email="old@example.com")
    protocol.results["old@example.com"] = {
        "ok": False,
        "old_email": "old@example.com",
        "new_email": "",
        "error": "change_email/begin 403: forbidden",
    }

    first = _run_task(isolated_db, {"ids": [account_id]}, task_id="task-rebind-1")
    assert first["status"] == "failed"
    assert len(rebind_service.load_subdomain_allocations()["cloud.example.com"]) == 1
    first_domain = protocol.calls[0]["mail_config"]["domain"]

    protocol.results["old@example.com"] = _protocol_success(
        "moved@" + first_domain, "old@example.com"
    )
    second = _run_task(isolated_db, {"ids": [account_id]}, task_id="task-rebind-2")
    assert second["status"] == "succeeded"
    assert len(rebind_service.load_subdomain_allocations()["cloud.example.com"]) == 1
    assert protocol.calls[1]["mail_config"]["domain"] == first_domain


def test_rebind_onboards_subdomain_email_routing(isolated_db, protocol, cloudflare_stub):
    """分配子域后、协议调用前：get_zone 解析主域名 zone 并以 name={sub}.{main} 启用 onboarding。"""
    _configure_mail()
    account_id = _create_account(isolated_db, email="old@example.com")
    protocol.results["old@example.com"] = _protocol_success("moved@later.example.com", "old@example.com")

    outcome = _run_task(isolated_db, {"ids": [account_id]})

    assert outcome["status"] == "succeeded"
    allocations = rebind_service.load_subdomain_allocations()["cloud.example.com"]
    expected_domain = allocations[0]["subdomain"] + ".cloud.example.com"
    assert [c for c in cloudflare_stub.calls if c["op"] == "get_zone"] == [
        {"op": "get_zone", "domain": "cloud.example.com"}
    ]
    enables = [c for c in cloudflare_stub.calls if c["op"] == "enable_email_routing_dns"]
    assert enables == [{"op": "enable_email_routing_dns", "zone_id": "zone-1", "name": expected_domain}]
    # 协议在 onboarding 之后调用，且 domain 即完成 onboarding 的子域
    assert protocol.calls[0]["mail_config"]["domain"] == expected_domain
    with Session(isolated_db) as session:
        events = session.exec(
            select(TaskEventModel).where(TaskEventModel.task_id == "task-rebind-test")
        ).all()
    messages = " ".join(event.message for event in events)
    assert f"子域名邮件路由已就绪: {expected_domain}" in messages


def test_rebind_skips_onboarding_when_subdomain_mx_exists(isolated_db, protocol, cloudflare_stub):
    """子域已有托管 MX（已 onboarding）：跳过 enable，仅幂等检测，任务继续成功。"""
    _configure_mail()
    account_id = _create_account(isolated_db, email="old@example.com")
    protocol.results["old@example.com"] = _protocol_success("moved@later.example.com", "old@example.com")
    cloudflare_stub.existing_mx_records = [
        {
            "id": "rec-mx",
            "type": "MX",
            "name": "anything.cloud.example.com",
            "content": "route1.mx.cloudflare.net",
            "priority": 10,
            "ttl": 1,
            "proxied": False,
        }
    ]

    outcome = _run_task(isolated_db, {"ids": [account_id]})

    assert outcome["status"] == "succeeded"
    assert outcome["result"]["data"]["results"][0]["ok"] is True
    assert not any(c["op"] == "enable_email_routing_dns" for c in cloudflare_stub.calls)
    assert any(
        c["op"] == "list_dns_records" and c["record_type"] == "MX" for c in cloudflare_stub.calls
    )
    assert protocol.calls[0]["mail_config"]["domain"].endswith(".cloud.example.com")


def test_rebind_onboarding_failure_fails_account_before_protocol(isolated_db, protocol, cloudflare_stub):
    """onboarding 失败 -> 账号结构化失败：不调用协议、不落库，错误含明细与补救提示。"""
    _configure_mail()
    account_id = _create_account(isolated_db, email="old@example.com")
    cloudflare_stub.enable_error = CloudflareAPIError(
        "POST",
        "/zones/zone-1/email/routing/dns",
        403,
        [{"code": 9103, "message": "Unauthorized to access requested resource"}],
    )

    outcome = _run_task(isolated_db, {"ids": [account_id]})

    assert outcome["status"] == "failed"
    assert outcome["success_count"] == 0
    assert outcome["error_count"] == 1
    item = outcome["result"]["data"]["results"][0]
    assert item["ok"] is False
    assert "子域名邮件路由启用失败" in item["error"]
    assert "HTTP 403" in item["error"]
    assert "Unauthorized to access requested resource" in item["error"]
    assert "区域→Zone Settings→编辑" in item["error"]
    assert protocol.calls == []
    assert _account_snapshot(isolated_db, account_id)["email"] == "old@example.com"
    with Session(isolated_db) as session:
        events = session.exec(
            select(TaskEventModel).where(TaskEventModel.task_id == "task-rebind-test")
        ).all()
    messages = " ".join(event.message for event in events)
    assert "子域名邮件路由启用失败" in messages
    assert "区域→Zone Settings→编辑" in messages


def test_rebind_onboarding_fails_when_main_domain_zone_not_visible(isolated_db, protocol, cloudflare_stub):
    """主域名不在当前 Token 可见范围 -> 账号失败，日志说明 Zone 解析失败。"""
    _configure_mail()
    account_id = _create_account(isolated_db, email="old@example.com")
    cloudflare_stub.zone_result = None

    outcome = _run_task(isolated_db, {"ids": [account_id]})

    assert outcome["status"] == "failed"
    assert outcome["error_count"] == 1
    item = outcome["result"]["data"]["results"][0]
    assert item["ok"] is False
    assert "未托管在当前 Token 可见范围" in item["error"]
    assert protocol.calls == []
    assert _account_snapshot(isolated_db, account_id)["email"] == "old@example.com"


def test_subdomain_allocations_survive_partial_update_and_get_config(isolated_db):
    """PUT 部分更新不误清 subdomain_allocations；GET 返回分配与配额能力，且无敏感值。"""
    _configure_mail()
    sub, main = rebind_service.allocate_rebind_subdomain("old@example.com")

    rebind_service.update_mail_config({"forward_to": "ops@example.com"})
    allocations = rebind_service.load_subdomain_allocations()
    assert allocations[main][0]["subdomain"] == sub
    assert allocations[main][0]["account_email"] == "old@example.com"

    # PUT 显式提交其他字段（含伪造的 subdomain_allocations）都不会替换真实分配
    rebind_service.update_mail_config({"domains": "cloud.example.com", "subdomain_allocations": {"evil.com": []}})
    assert rebind_service.load_subdomain_allocations()[main][0]["subdomain"] == sub

    masked = rebind_service.get_mail_config()
    assert masked["subdomain_allocations"] == rebind_service.load_subdomain_allocations()
    capabilities = masked["provision"]["capabilities"]
    assert capabilities["subdomain_limit_per_domain"] == rebind_service.SUBDOMAIN_LIMIT_PER_DOMAIN == 10
    assert CONFIG["api_token"] not in json.dumps(masked, ensure_ascii=False)
