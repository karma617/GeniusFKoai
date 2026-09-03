# -*- coding: utf-8 -*-
"""Cloudflare Email Routing provision focused tests（全 mock，无真实网络/真实 DB）。"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import pytest
from fastapi import HTTPException
from sqlmodel import SQLModel

import api.chatgpt_rebind as rebind_api
import application.chatgpt_rebind as rebind_service
import core.config_store as config_store_module
from api.chatgpt_rebind import CloudflareProvisionRequest
from application.cloudflare_email_routing import (
    CATCH_ALL_RULE_NAME,
    CloudflareAPIError,
    CloudflareClient,
    provision_cloudflare_email_routing,
    validate_domain,
)
from core.db import create_configured_engine

TOKEN = "cf-secret-token-123456"
WORKER = "cloud-mail-worker"
ZONE_ID = "zone-abc123"
DOMAIN = "cloud.example.com"
ACCOUNT_ID = "acct-1"

CONFIG = {
    "cloudflare_api_token": TOKEN,
    "cloudflare_worker_name": WORKER,
    "cloudflare_account_id": "",
    "domains": [DOMAIN],
}

MX_RECORDS = [
    {"type": "MX", "name": DOMAIN, "content": "route1.mx.cloudflare.net", "priority": 13, "locked": True},
    {"type": "MX", "name": DOMAIN, "content": "route2.mx.cloudflare.net", "priority": 86, "locked": True},
]


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        if text:
            self.text = text
        elif payload is not None:
            self.text = json.dumps(payload)
        else:
            self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class Responder:
    """按 (method, path 正则) 分发预设响应；未匹配直接让测试失败。"""

    def __init__(self):
        self._routes = []

    def add(self, method, pattern, handler):
        self._routes.append((method, re.compile(pattern), handler))

    def __call__(self, method, path, json_body, params):
        for route_method, pattern, handler in self._routes:
            if route_method == method and pattern.match(path):
                if callable(handler):
                    return handler(json_body, params)
                return handler
        raise AssertionError(f"unexpected cloudflare request: {method} {path}")


class FakeSession:
    def __init__(self, responder):
        self._responder = responder
        self.calls = []

    def request(self, method, url, headers=None, json=None, params=None, timeout=None, files=None):
        path = urlsplit(url).path
        if path.startswith("/client/v4"):
            path = path[len("/client/v4") :] or "/"
        self.calls.append(
            {
                "method": method,
                "path": path,
                "json": json,
                "params": params,
                "headers": dict(headers or {}),
                "timeout": timeout,
                "files": files,
            }
        )
        return self._responder(method, path, json, params)


class StaticSession:
    """始终返回同一响应（或响应工厂）并计数的会话替身，用于重试语义。"""

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def request(self, method, url, headers=None, json=None, params=None, timeout=None):
        self.calls += 1
        if callable(self.response):
            return self.response()
        return self.response


def queue(*responses):
    items = list(responses)

    def handler(*args, **kwargs):
        if not items:
            raise AssertionError("response queue exhausted")
        return items.pop(0)

    return handler


def ok(result):
    return FakeResponse(200, {"success": True, "errors": [], "result": result})


def cf_error(status, code, message, docs=""):
    errors = [{"code": code, "message": message}]
    if docs:
        errors[0]["documentation_url"] = docs
    return FakeResponse(status, {"success": False, "errors": errors})


def build_responder(
    *,
    routing_enabled=False,
    catch_all=None,
    zones_result=None,
    dns_records=None,
    accounts_result=None,
    worker_bindings=None,
    patch_worker_settings_response=None,
    wildcard_mx_records=None,
    wildcard_txt_records=None,
    wildcard_create_responses=None,
):
    responder = Responder()
    responder.add("GET", r"^/user/tokens/verify$", ok({"status": "active"}))
    if zones_result is None:
        zones_result = [{"id": ZONE_ID, "name": DOMAIN, "status": "active"}]
    responder.add("GET", r"^/zones$", ok(zones_result))
    responder.add(
        "GET",
        r"^/zones/[^/]+/email/routing$",
        ok({"enabled": routing_enabled, "status": "ready" if routing_enabled else "disabled"}),
    )
    responder.add("POST", r"^/zones/[^/]+/email/routing/dns$", ok({"record": []}))
    if dns_records is None:
        dns_records = MX_RECORDS + [
            {"type": "TXT", "name": DOMAIN, "content": "v=spf1 include:_spf.mx.cloudflare.net ~all", "locked": True},
        ]
    responder.add("GET", r"^/zones/[^/]+/email/routing/dns$", ok({"record": dns_records}))
    responder.add("GET", r"^/zones/[^/]+/email/routing/rules/catch_all$", ok(catch_all if catch_all is not None else {}))
    responder.add("PUT", r"^/zones/[^/]+/email/routing/rules/catch_all$", ok({"id": "rule-1", "enabled": True}))
    # wildcard 步骤：默认 *.{DOMAIN} 的 MX/SPF 已齐全且 SPF 带引号（幂等 skipped，不发 POST/PATCH）
    expected_wildcard_mx = [
        {"type": "MX", "name": "*." + DOMAIN, "content": "route1.mx.cloudflare.net", "priority": 10, "ttl": 1, "proxied": False},
        {"type": "MX", "name": "*." + DOMAIN, "content": "route2.mx.cloudflare.net", "priority": 20, "ttl": 1, "proxied": False},
        {"type": "MX", "name": "*." + DOMAIN, "content": "route3.mx.cloudflare.net", "priority": 30, "ttl": 1, "proxied": False},
    ]
    expected_wildcard_txt = [
        {"type": "TXT", "name": "*." + DOMAIN, "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"', "ttl": 1, "proxied": False},
    ]

    def dns_records_handler(json_body, params):
        record_type = str((params or {}).get("type") or "")
        if record_type == "MX":
            return ok(expected_wildcard_mx if wildcard_mx_records is None else wildcard_mx_records)
        if record_type == "TXT":
            return ok(expected_wildcard_txt if wildcard_txt_records is None else wildcard_txt_records)
        return ok([])

    responder.add("GET", r"^/zones/[^/]+/dns_records$", dns_records_handler)
    if wildcard_create_responses is not None:
        responder.add("POST", r"^/zones/[^/]+/dns_records$", queue(*wildcard_create_responses))
    else:
        responder.add("POST", r"^/zones/[^/]+/dns_records$", ok({"id": "rec-new", "name": "*." + DOMAIN}))
    responder.add("PATCH", r"^/zones/[^/]+/dns_records/[^/]+$", ok({"id": "rec-manual", "name": "*." + DOMAIN}))
    # worker settings 步骤：默认绑定已包含 DOMAIN（幂等 skipped）；account_id 为空走 /accounts 发现
    if accounts_result is None:
        accounts_result = [{"id": ACCOUNT_ID, "name": "Example Account"}]
    responder.add("GET", r"^/accounts$", ok(accounts_result))
    if worker_bindings is None:
        worker_bindings = [
            {"type": "json", "name": "DOMAINS", "json": [DOMAIN]},
            {"type": "json", "name": "RANDOM_SUBDOMAIN_DOMAINS", "json": [DOMAIN]},
        ]
    responder.add(
        "GET",
        r"^/accounts/[^/]+/workers/scripts/[^/]+/settings$",
        ok({"bindings": worker_bindings}),
    )
    if patch_worker_settings_response is None:
        patch_worker_settings_response = ok({"bindings": worker_bindings})
    responder.add(
        "PATCH",
        r"^/accounts/[^/]+/workers/scripts/[^/]+/settings$",
        patch_worker_settings_response,
    )
    return responder


def matched_catch_all_rule():
    return {
        "id": "rule-1",
        "name": CATCH_ALL_RULE_NAME,
        "enabled": True,
        "matchers": [{"type": "all"}],
        "actions": [{"type": "worker", "value": [WORKER]}],
    }


def make_client(session, *, retry_delays=(0.0,)):
    return CloudflareClient(TOKEN, account_id="", retry_delays=retry_delays, session=session)


def run_provision(session, *, domains=None, config=None, retry_delays=(0.0,)):
    logs = []
    result = provision_cloudflare_email_routing(
        dict(CONFIG if config is None else config),
        domains,
        client=make_client(session, retry_delays=retry_delays),
        log_fn=logs.append,
    )
    return result, logs


def test_provision_enables_routing_and_puts_catch_all():
    session = FakeSession(build_responder(routing_enabled=False, catch_all={}))

    result, logs = run_provision(session)

    assert result["ok"] is True
    assert result["summary"] == {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0}
    item = result["results"][0]
    assert item["domain"] == DOMAIN
    assert item["ok"] is True
    assert item["zone_id"] == ZONE_ID
    steps = item["steps"]
    assert steps["zone"]["status"] == "success"
    assert steps["email_routing"]["status"] == "success"
    assert "/email/routing/dns" in steps["email_routing"]["message"]
    assert steps["dns"]["status"] == "success"
    mx = [r for r in steps["dns"]["records"] if r["type"] == "MX"]
    assert len(mx) == 2
    assert all(r["locked"] for r in mx)
    assert steps["catch_all"]["status"] == "success"

    # 第 5 步 worker 域名变量：默认绑定已含 DOMAIN -> skipped（未发 PATCH），
    # account_id 为空 -> /accounts 自动发现唯一账号后 GET settings
    assert steps["worker"]["status"] == "skipped"
    assert DOMAIN in steps["worker"]["message"]
    assert not any(c["method"] == "PATCH" for c in session.calls)
    accounts_calls = [c for c in session.calls if c["path"] == "/accounts"]
    assert len(accounts_calls) == 1
    settings_calls = [c for c in session.calls if "/workers/scripts/" in c["path"]]
    assert [c["path"] for c in settings_calls] == [f"/accounts/{ACCOUNT_ID}/workers/scripts/{WORKER}/settings"]

    # 第 4 步 wildcard：默认通配 MX/SPF 已齐全 -> skipped（未发 POST /dns_records）
    assert steps["wildcard"]["status"] == "skipped"
    assert not any(c["method"] == "POST" and c["path"].endswith("/dns_records") for c in session.calls)
    wildcard_gets = [c for c in session.calls if c["method"] == "GET" and c["path"].endswith("/dns_records")]
    assert {c["params"]["type"] for c in wildcard_gets} == {"MX", "TXT"}
    assert all(c["params"]["name"] == "*." + DOMAIN for c in wildcard_gets)

    # 官方契约：apex 只用 /email/routing/dns 启用（不用手工 dns_records 代替）；禁止废弃 /enable
    assert not any(c["path"].endswith("/email/routing/enable") for c in session.calls)
    enable_calls = [c for c in session.calls if c["method"] == "POST" and c["path"].endswith("/email/routing/dns")]
    assert len(enable_calls) == 1
    # name 字段仅接受 zone 子域名（CF 2007），顶级域名启用必须省略 name
    assert enable_calls[0]["json"] == {}

    put_calls = [c for c in session.calls if c["method"] == "PUT"]
    assert len(put_calls) == 1
    assert put_calls[0]["json"] == {
        "actions": [{"type": "worker", "value": [WORKER]}],
        "matchers": [{"type": "all"}],
        "enabled": True,
        "name": CATCH_ALL_RULE_NAME,
    }

    # secret 仅用于请求头；结果与日志不泄露
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert 10 <= session.calls[0]["timeout"] <= 20
    assert TOKEN not in json.dumps(result)
    assert all(TOKEN not in line for line in logs)


def test_provision_skips_when_already_fully_configured():
    catch_all_rule = {
        "id": "rule-1",
        "name": CATCH_ALL_RULE_NAME,
        "enabled": True,
        "matchers": [{"type": "all"}],
        "actions": [{"type": "worker", "value": [WORKER]}],
    }
    session = FakeSession(build_responder(routing_enabled=True, catch_all=catch_all_rule))

    result, _ = run_provision(session)

    assert result["ok"] is True
    assert result["summary"]["skipped"] == 1
    steps = result["results"][0]["steps"]
    assert steps["email_routing"]["message"] == "Email Routing 已启用，跳过 DNS provisioning"
    assert steps["wildcard"]["status"] == "skipped"
    assert steps["catch_all"]["status"] == "skipped"
    assert steps["worker"]["status"] == "skipped"
    assert not any(c["method"] == "POST" for c in session.calls)
    assert not any(c["method"] == "PUT" for c in session.calls)
    assert not any(c["method"] == "PATCH" for c in session.calls)


def test_provision_updates_mismatched_catch_all():
    stale_rule = {
        "id": "rule-1",
        "name": "old rule",
        "enabled": True,
        "matchers": [{"type": "all"}],
        "actions": [{"type": "forward", "value": ["me@example.com"]}],
    }
    session = FakeSession(build_responder(routing_enabled=True, catch_all=stale_rule))

    result, _ = run_provision(session)

    assert result["ok"] is True
    assert result["results"][0]["steps"]["catch_all"]["status"] == "success"
    assert any(c["method"] == "PUT" for c in session.calls)


def test_catch_all_string_value_form_treated_as_match():
    rule = {
        "name": CATCH_ALL_RULE_NAME,
        "enabled": True,
        "matchers": [{"type": "all"}],
        "actions": [{"type": "worker", "value": WORKER}],
    }
    session = FakeSession(build_responder(routing_enabled=True, catch_all=rule))

    result, _ = run_provision(session)

    assert result["results"][0]["steps"]["catch_all"]["status"] == "skipped"
    assert not any(c["method"] == "PUT" for c in session.calls)


def test_token_failure_marks_all_domains_failed_without_secret():
    responder = Responder()
    responder.add(
        "GET",
        r"^/user/tokens/verify$",
        cf_error(401, 1000, "Invalid API Token", "https://developers.cloudflare.com/fundamentals/api/"),
    )
    session = FakeSession(responder)
    config = dict(CONFIG, domains=["a.example.com", "b.example.com"])

    result, logs = run_provision(session, config=config)

    assert result["ok"] is False
    assert "token 校验失败" in result["error"]
    assert "401" in result["error"]
    assert result["summary"]["total"] == 2
    assert result["summary"]["failed"] == 2
    assert [item["domain"] for item in result["results"]] == ["a.example.com", "b.example.com"]
    assert all(item["ok"] is False for item in result["results"])
    # secret 不进结果、不进日志；token 失败不触碰 zone 资源
    assert TOKEN not in json.dumps(result, ensure_ascii=False)
    assert all(TOKEN not in line for line in logs)
    assert not any(c["path"] == "/zones" for c in session.calls)


def test_inactive_token_marks_all_domains_failed():
    responder = Responder()
    responder.add("GET", r"^/user/tokens/verify$", ok({"status": "disabled"}))

    result, _ = run_provision(FakeSession(responder))

    assert result["ok"] is False
    assert result["summary"] == {"total": 1, "succeeded": 0, "failed": 1, "skipped": 0}
    assert "token status is disabled" in result["error"]


def test_zone_not_found_fails_domain_with_remaining_steps_skipped():
    session = FakeSession(build_responder(zones_result=[]))

    result, _ = run_provision(session)

    assert result["ok"] is False
    item = result["results"][0]
    assert item["ok"] is False
    assert "未找到域名" in item["error"]
    assert item["steps"]["zone"]["status"] == "failed"
    for key in ("email_routing", "wildcard", "dns", "catch_all", "worker"):
        assert item["steps"][key]["status"] == "skipped"


def test_multiple_domains_partial_failure_continues():
    responder = Responder()
    responder.add("GET", r"^/user/tokens/verify$", ok({"status": "active"}))

    def zones_handler(json_body, params):
        name = str((params or {}).get("name") or "")
        if name == "one.example.com":
            return ok([{"id": "zone-one", "name": name, "status": "active"}])
        return ok([])

    responder.add("GET", r"^/zones$", zones_handler)
    responder.add("GET", r"^/zones/[^/]+/email/routing$", ok({"enabled": True, "status": "ready"}))
    responder.add(
        "GET",
        r"^/zones/[^/]+/email/routing/dns$",
        ok({"record": [{"type": "MX", "name": "one.example.com", "content": "route1.mx.cloudflare.net", "priority": 13, "locked": True}]}),
    )
    responder.add("GET", r"^/zones/[^/]+/email/routing/rules/catch_all$", ok({}))
    responder.add("PUT", r"^/zones/[^/]+/email/routing/rules/catch_all$", ok({"id": "rule"}))
    responder.add("GET", r"^/accounts$", ok([{"id": ACCOUNT_ID, "name": "Example Account"}]))
    responder.add(
        "GET",
        r"^/accounts/[^/]+/workers/scripts/[^/]+/settings$",
        ok(
            {
                "bindings": [
                    {"type": "json", "name": "DOMAINS", "json": ["one.example.com"]},
                    {"type": "json", "name": "RANDOM_SUBDOMAIN_DOMAINS", "json": ["one.example.com"]},
                ]
            }
        ),
    )
    responder.add(
        "GET",
        r"^/zones/[^/]+/dns_records$",
        lambda json_body, params: ok(
            [
                {"type": "MX", "name": "*.one.example.com", "content": content, "priority": priority, "ttl": 1, "proxied": False}
                for content, priority in (("route1.mx.cloudflare.net", 10), ("route2.mx.cloudflare.net", 20), ("route3.mx.cloudflare.net", 30))
            ]
            if (params or {}).get("type") == "MX"
            else [
                {"type": "TXT", "name": "*.one.example.com", "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"', "ttl": 1, "proxied": False}
            ]
        ),
    )
    session = FakeSession(responder)

    result, _ = run_provision(session, domains="one.example.com\ntwo.example.com")

    assert result["ok"] is False
    assert result["summary"] == {"total": 2, "succeeded": 1, "failed": 1, "skipped": 0}
    by_domain = {item["domain"]: item for item in result["results"]}
    assert by_domain["one.example.com"]["ok"] is True
    assert by_domain["one.example.com"]["zone_id"] == "zone-one"
    assert by_domain["one.example.com"]["steps"]["worker"]["status"] == "skipped"
    assert by_domain["two.example.com"]["ok"] is False
    assert "未找到域名" in by_domain["two.example.com"]["error"]
    assert by_domain["two.example.com"]["steps"]["worker"]["status"] == "skipped"
    # 域 2 失败不中断域 1 的 catch-all 写入
    assert any(c["method"] == "PUT" for c in session.calls)


def test_dns_recheck_missing_mx_fails_domain():
    session = FakeSession(
        build_responder(routing_enabled=True, dns_records=[{"type": "TXT", "name": DOMAIN, "content": "v=spf1 ~all", "locked": True}])
    )

    result, _ = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is False
    assert "MX" in item["error"]
    assert item["steps"]["dns"]["status"] == "failed"
    assert item["steps"]["catch_all"]["status"] == "skipped"
    assert item["steps"]["worker"]["status"] == "skipped"


def test_429_retries_with_backoff_then_succeeds():
    responder = Responder()
    responder.add("GET", r"^/user/tokens/verify$", ok({"status": "active"}))
    responder.add(
        "GET",
        r"^/zones$",
        queue(cf_error(429, 971, "requests throttled"), ok([{"id": ZONE_ID, "name": DOMAIN, "status": "active"}])),
    )
    responder.add("GET", r"^/zones/[^/]+/email/routing$", ok({"enabled": True, "status": "ready"}))
    responder.add(
        "GET",
        r"^/zones/[^/]+/email/routing/dns$",
        ok({"record": [{"type": "MX", "name": DOMAIN, "content": "route1.mx.cloudflare.net", "priority": 13, "locked": True}]}),
    )
    responder.add("GET", r"^/zones/[^/]+/email/routing/rules/catch_all$", ok({}))
    responder.add("PUT", r"^/zones/[^/]+/email/routing/rules/catch_all$", ok({"id": "rule"}))
    responder.add("GET", r"^/accounts$", ok([{"id": ACCOUNT_ID, "name": "Example Account"}]))
    responder.add(
        "GET",
        r"^/accounts/[^/]+/workers/scripts/[^/]+/settings$",
        ok(
            {
                "bindings": [
                    {"type": "json", "name": "DOMAINS", "json": [DOMAIN]},
                    {"type": "json", "name": "RANDOM_SUBDOMAIN_DOMAINS", "json": [DOMAIN]},
                ]
            }
        ),
    )
    responder.add(
        "GET",
        r"^/zones/[^/]+/dns_records$",
        lambda json_body, params: ok(
            [
                {"type": "MX", "name": "*." + DOMAIN, "content": content, "priority": priority, "ttl": 1, "proxied": False}
                for content, priority in (("route1.mx.cloudflare.net", 10), ("route2.mx.cloudflare.net", 20), ("route3.mx.cloudflare.net", 30))
            ]
            if (params or {}).get("type") == "MX"
            else [
                {"type": "TXT", "name": "*." + DOMAIN, "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"', "ttl": 1, "proxied": False}
            ]
        ),
    )
    session = FakeSession(responder)

    result, _ = run_provision(session)

    assert result["ok"] is True
    assert len([c for c in session.calls if c["path"] == "/zones"]) == 2
    assert result["results"][0]["steps"]["wildcard"]["status"] == "skipped"
    assert result["results"][0]["steps"]["worker"]["status"] == "skipped"


def test_429_exhaustion_surfaces_as_api_error():
    session = StaticSession(cf_error(429, 971, "throttled"))
    client = CloudflareClient(TOKEN, retry_delays=(0.0, 0.0, 0.0), session=session)

    with pytest.raises(CloudflareAPIError) as excinfo:
        client.request("GET", "/zones")

    assert excinfo.value.status_code == 429
    assert session.calls == 4  # 1 次初始 + 3 次退避重试（有界）


def test_5xx_retry_then_success():
    session = StaticSession(queue(cf_error(500, -1, "boom"), ok({"status": "active"})))
    client = CloudflareClient(TOKEN, retry_delays=(0.0,), session=session)

    data = client.request("GET", "/user/tokens/verify")

    assert data["result"]["status"] == "active"
    assert session.calls == 2


def test_4xx_error_details_parsed_without_retry():
    session = StaticSession(
        cf_error(403, 9109, "Unauthorized to access requested resource", "https://developers.cloudflare.com/api/")
    )
    client = CloudflareClient(TOKEN, retry_delays=(), session=session)

    with pytest.raises(CloudflareAPIError) as excinfo:
        client.request("GET", "/zones/zone-1/email/routing")

    message = str(excinfo.value)
    assert "403" in message
    assert "9109" in message
    assert "Unauthorized to access requested resource" in message
    assert "developers.cloudflare.com" in message
    assert excinfo.value.errors[0]["code"] == 9109
    assert session.calls == 1  # 4xx 不重试


def test_http_200_success_false_is_treated_as_api_error():
    session = StaticSession(cf_error(200, 10000, "operation failed"))
    client = CloudflareClient(TOKEN, retry_delays=(), session=session)

    with pytest.raises(CloudflareAPIError) as excinfo:
        client.request("GET", "/zones")

    assert excinfo.value.status_code == 200
    assert "operation failed" in str(excinfo.value)
    assert session.calls == 1


def test_validate_domain_normalizes_and_rejects_invalid():
    assert validate_domain(" Cloud.Example.COM. ") == "cloud.example.com"
    assert validate_domain("@mail.example.com") == "mail.example.com"

    for bad in [
        "",
        "not a domain",
        "example",
        "a@b.com",
        "-bad.example.com",
        "bad-.example.com",
        "x" * 64 + ".com",
        "space .example.com",
    ]:
        with pytest.raises(ValueError):
            validate_domain(bad)


def test_provision_requires_token_worker_and_valid_domains():
    session = FakeSession(Responder())

    with pytest.raises(ValueError) as excinfo:
        provision_cloudflare_email_routing(
            {"cloudflare_api_token": "", "cloudflare_worker_name": WORKER, "domains": [DOMAIN]},
            client=make_client(session),
        )
    assert "cloudflare_api_token" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        provision_cloudflare_email_routing(
            {"cloudflare_api_token": TOKEN, "cloudflare_worker_name": "", "domains": [DOMAIN]},
            client=make_client(session),
        )
    assert "cloudflare_worker_name" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        provision_cloudflare_email_routing(
            {"cloudflare_api_token": TOKEN, "cloudflare_worker_name": WORKER, "domains": []},
            client=make_client(session),
        )
    assert "域名" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        provision_cloudflare_email_routing(
            {"cloudflare_api_token": TOKEN, "cloudflare_worker_name": WORKER, "domains": ["bad domain"]},
            client=make_client(session),
        )
    assert "域名格式不合法" in str(excinfo.value)

    # 请求空白 domains 回退到配置全部域名
    result, _ = run_provision(FakeSession(build_responder()), domains="   ")
    assert result["results"][0]["domain"] == DOMAIN


def test_worker_step_merges_domain_into_json_bindings():
    worker_bindings = [
        {"type": "plain_text", "name": "OTHER", "text": "keep-me"},
        {"type": "json", "name": "DOMAINS", "json": ["Other.Example.com"]},
        {"type": "kv_namespace", "name": "KV", "namespace_id": "kv-1"},
        {"type": "json", "name": "RANDOM_SUBDOMAIN_DOMAINS", "json": []},
    ]
    session = FakeSession(
        build_responder(routing_enabled=True, catch_all=matched_catch_all_rule(), worker_bindings=worker_bindings)
    )

    result, logs = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is True
    assert item["changed"] is True
    assert result["summary"] == {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0}
    steps = item["steps"]
    assert steps["worker"]["status"] == "success"
    assert DOMAIN in steps["worker"]["message"]

    patch_calls = [c for c in session.calls if c["method"] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0]["path"] == f"/accounts/{ACCOUNT_ID}/workers/scripts/{WORKER}/settings"
    # 官方契约：multipart/form-data，settings part 以 application/json 承载 {"bindings": [...]}
    settings_part = patch_calls[0]["files"]["settings"]
    assert settings_part[0] is None
    assert settings_part[2] == "application/json"
    payload = json.loads(settings_part[1])
    assert payload == {
        "bindings": [
            {"type": "plain_text", "name": "OTHER", "text": "keep-me"},
            {"type": "json", "name": "DOMAINS", "json": ["Other.Example.com", DOMAIN]},
            {"type": "kv_namespace", "name": "KV", "namespace_id": "kv-1"},
            {"type": "json", "name": "RANDOM_SUBDOMAIN_DOMAINS", "json": [DOMAIN]},
        ]
    }
    assert TOKEN not in json.dumps(result, ensure_ascii=False)
    assert all(TOKEN not in line for line in logs)


def test_worker_step_idempotent_skip_when_bindings_already_match():
    session = FakeSession(build_responder(routing_enabled=True, catch_all=matched_catch_all_rule()))

    result, _ = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is True
    assert item["changed"] is False
    assert result["summary"]["skipped"] == 1
    assert item["steps"]["worker"]["status"] == "skipped"
    assert DOMAIN in item["steps"]["worker"]["message"]
    assert not any(c["method"] == "PATCH" for c in session.calls)


def test_worker_step_creates_missing_json_bindings():
    worker_bindings = [{"type": "json", "name": "OTHER_JSON", "json": ["keep.example.com"]}]
    session = FakeSession(
        build_responder(routing_enabled=True, catch_all=matched_catch_all_rule(), worker_bindings=worker_bindings)
    )

    result, _ = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is True
    assert item["changed"] is True
    patch_calls = [c for c in session.calls if c["method"] == "PATCH"]
    assert len(patch_calls) == 1
    payload = json.loads(patch_calls[0]["files"]["settings"][1])
    assert payload["bindings"] == [
        {"type": "json", "name": "OTHER_JSON", "json": ["keep.example.com"]},
        {"type": "json", "name": "DOMAINS", "json": [DOMAIN]},
        {"type": "json", "name": "RANDOM_SUBDOMAIN_DOMAINS", "json": [DOMAIN]},
    ]
    assert item["steps"]["worker"]["status"] == "success"


def test_worker_step_non_array_json_value_fails_without_patch():
    worker_bindings = [{"type": "json", "name": "DOMAINS", "json": "not-a-list"}]
    session = FakeSession(
        build_responder(routing_enabled=True, catch_all=matched_catch_all_rule(), worker_bindings=worker_bindings)
    )

    result, _ = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is False
    assert item["steps"]["worker"]["status"] == "failed"
    assert "DOMAINS" in item["steps"]["worker"]["message"]
    assert "不是数组" in item["steps"]["worker"]["message"]
    assert result["summary"] == {"total": 1, "succeeded": 0, "failed": 1, "skipped": 0}
    assert not any(c["method"] == "PATCH" for c in session.calls)


def test_worker_step_secret_text_binding_blocks_patch():
    worker_bindings = [
        {"type": "secret_text", "name": "API_KEY"},
        {"type": "json", "name": "DOMAINS", "json": ["other.example.com"]},
        {"type": "json", "name": "RANDOM_SUBDOMAIN_DOMAINS", "json": [DOMAIN]},
    ]
    session = FakeSession(
        build_responder(routing_enabled=True, catch_all=matched_catch_all_rule(), worker_bindings=worker_bindings)
    )

    result, logs = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is False
    assert item["steps"]["worker"]["status"] == "failed"
    assert item["steps"]["worker"]["message"] == "该 Worker 含 secret 绑定，请手动在控制台添加域名变量"
    assert result["summary"]["failed"] == 1
    # 安全路径：绝不 PATCH（secret 无法经 GET->PATCH 还原）；secret 不入结果与日志
    assert not any(c["method"] == "PATCH" for c in session.calls)
    assert TOKEN not in json.dumps(result, ensure_ascii=False)
    assert all(TOKEN not in line for line in logs)


def test_worker_step_discovers_single_account_id():
    # CONFIG cloudflare_account_id="" -> GET /accounts 唯一账号自动发现
    session = FakeSession(build_responder(routing_enabled=True, catch_all=matched_catch_all_rule()))

    result, _ = run_provision(session)

    accounts_calls = [c for c in session.calls if c["path"] == "/accounts"]
    assert len(accounts_calls) == 1
    settings_paths = [c["path"] for c in session.calls if "/workers/scripts/" in c["path"]]
    assert settings_paths == [f"/accounts/{ACCOUNT_ID}/workers/scripts/{WORKER}/settings"]
    assert result["results"][0]["ok"] is True


def test_worker_step_uses_configured_account_id_without_discovery():
    config = dict(CONFIG, cloudflare_account_id=ACCOUNT_ID)
    session = FakeSession(build_responder(routing_enabled=True, catch_all=matched_catch_all_rule()))

    result, _ = run_provision(session, config=config)

    assert not any(c["path"] == "/accounts" for c in session.calls)
    settings_paths = [c["path"] for c in session.calls if "/workers/scripts/" in c["path"]]
    assert settings_paths == [f"/accounts/{ACCOUNT_ID}/workers/scripts/{WORKER}/settings"]
    assert result["results"][0]["ok"] is True


def test_worker_step_fails_when_account_discovery_ambiguous():
    for accounts_result in ([], [{"id": "acct-a"}, {"id": "acct-b"}]):
        session = FakeSession(
            build_responder(
                routing_enabled=True,
                catch_all=matched_catch_all_rule(),
                accounts_result=accounts_result,
            )
        )

        result, _ = run_provision(session)

        item = result["results"][0]
        assert item["ok"] is False
        assert item["steps"]["worker"]["status"] == "failed"
        assert "请在换绑配置中填写 Cloudflare Account ID（控制台域名概览页右下角）" in item["steps"]["worker"]["message"]
        # 发现失败时不触碰 worker settings 端点
        assert not any("/workers/scripts/" in c["path"] for c in session.calls)


def test_worker_step_patch_403_fails_domain():
    worker_bindings = [
        {"type": "json", "name": "DOMAINS", "json": []},
        {"type": "json", "name": "RANDOM_SUBDOMAIN_DOMAINS", "json": []},
    ]
    session = FakeSession(
        build_responder(
            routing_enabled=True,
            catch_all=matched_catch_all_rule(),
            worker_bindings=worker_bindings,
            patch_worker_settings_response=cf_error(403, 10000, "workers.api.error.unauthorized"),
        )
    )

    result, logs = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is False
    assert item["steps"]["worker"]["status"] == "failed"
    assert "403" in item["steps"]["worker"]["message"]
    assert "workers.api.error.unauthorized" in item["steps"]["worker"]["message"]
    assert result["summary"] == {"total": 1, "succeeded": 0, "failed": 1, "skipped": 0}
    assert TOKEN not in json.dumps(result, ensure_ascii=False)
    assert all(TOKEN not in line for line in logs)


def test_wildcard_step_skips_when_records_already_exist():
    session = FakeSession(build_responder(routing_enabled=True, catch_all=matched_catch_all_rule()))

    result, _ = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is True
    assert item["changed"] is False
    assert result["summary"]["skipped"] == 1
    steps = item["steps"]
    assert steps["wildcard"]["status"] == "skipped"
    assert "*." + DOMAIN in steps["wildcard"]["message"]
    assert not any(c["method"] == "POST" and c["path"].endswith("/dns_records") for c in session.calls)


def test_wildcard_step_creates_missing_mx_and_spf():
    session = FakeSession(
        build_responder(
            routing_enabled=True,
            catch_all=matched_catch_all_rule(),
            wildcard_mx_records=[],
            wildcard_txt_records=[],
        )
    )

    result, logs = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is True
    assert item["changed"] is True
    assert result["summary"] == {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0}
    assert item["steps"]["wildcard"]["status"] == "success"
    assert "*." + DOMAIN in item["steps"]["wildcard"]["message"]

    wildcard_gets = [c for c in session.calls if c["method"] == "GET" and c["path"].endswith("/dns_records")]
    assert [(c["params"]["type"], c["params"]["name"]) for c in wildcard_gets] == [
        ("MX", "*." + DOMAIN),
        ("TXT", "*." + DOMAIN),
    ]
    post_calls = [c for c in session.calls if c["method"] == "POST" and c["path"].endswith("/dns_records")]
    assert [c["json"] for c in post_calls] == [
        {"type": "MX", "name": "*." + DOMAIN, "content": "route1.mx.cloudflare.net", "priority": 10, "ttl": 1, "proxied": False},
        {"type": "MX", "name": "*." + DOMAIN, "content": "route2.mx.cloudflare.net", "priority": 20, "ttl": 1, "proxied": False},
        {"type": "MX", "name": "*." + DOMAIN, "content": "route3.mx.cloudflare.net", "priority": 30, "ttl": 1, "proxied": False},
        {"type": "TXT", "name": "*." + DOMAIN, "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"', "ttl": 1, "proxied": False},
    ]
    assert TOKEN not in json.dumps(result, ensure_ascii=False)
    assert all(TOKEN not in line for line in logs)


def test_wildcard_step_creates_only_missing_spf():
    session = FakeSession(
        build_responder(
            routing_enabled=True,
            catch_all=matched_catch_all_rule(),
            wildcard_txt_records=[],
        )
    )

    result, _ = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is True
    assert item["changed"] is True
    post_calls = [c for c in session.calls if c["method"] == "POST" and c["path"].endswith("/dns_records")]
    assert [c["json"] for c in post_calls] == [
        {"type": "TXT", "name": "*." + DOMAIN, "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"', "ttl": 1, "proxied": False},
    ]
    assert item["steps"]["wildcard"]["status"] == "success"


def test_wildcard_step_skips_when_quoted_spf_already_exists():
    # 手工记录（带引号）已存在：剥引号比较后视为已配置，且无任何写入
    session = FakeSession(
        build_responder(
            routing_enabled=True,
            catch_all=matched_catch_all_rule(),
            wildcard_txt_records=[
                {
                    "id": "rec-manual",
                    "type": "TXT",
                    "name": "*." + DOMAIN,
                    "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"',
                    "ttl": 1,
                    "proxied": False,
                }
            ],
        )
    )

    result, _ = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is True
    assert item["changed"] is False
    assert result["summary"]["skipped"] == 1
    assert item["steps"]["wildcard"]["status"] == "skipped"
    assert not any(c["method"] == "POST" and c["path"].endswith("/dns_records") for c in session.calls)
    assert not any(c["method"] == "PATCH" and "/dns_records/" in c["path"] for c in session.calls)


def test_wildcard_step_normalizes_unquoted_spf_txt():
    # 旧代码创建的无引号 SPF：判已存在但原始 content 不一致 -> 仅 PATCH 规范化，不创建新记录
    session = FakeSession(
        build_responder(
            routing_enabled=True,
            catch_all=matched_catch_all_rule(),
            wildcard_txt_records=[
                {
                    "id": "rec-manual",
                    "type": "TXT",
                    "name": "*." + DOMAIN,
                    "content": "v=spf1 include:_spf.mx.cloudflare.net ~all",
                    "ttl": 300,
                    "proxied": False,
                }
            ],
        )
    )

    result, logs = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is True
    assert item["changed"] is True
    assert result["summary"] == {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0}
    assert item["steps"]["wildcard"]["status"] == "success"
    assert not any(c["method"] == "POST" and c["path"].endswith("/dns_records") for c in session.calls)
    patch_calls = [c for c in session.calls if c["method"] == "PATCH" and c["path"].endswith("/dns_records/rec-manual")]
    assert len(patch_calls) == 1
    # body 沿用记录现有 type/name/ttl/proxied + 新的带引号 content
    assert patch_calls[0]["json"] == {
        "type": "TXT",
        "name": "*." + DOMAIN,
        "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"',
        "ttl": 300,
        "proxied": False,
    }
    assert TOKEN not in json.dumps(result, ensure_ascii=False)
    assert all(TOKEN not in line for line in logs)


def test_wildcard_step_403_reports_permission_hint():
    session = FakeSession(
        build_responder(
            routing_enabled=True,
            catch_all=matched_catch_all_rule(),
            wildcard_mx_records=[],
            wildcard_txt_records=[],
            wildcard_create_responses=[cf_error(403, 9103, "IP is blocked")],
        )
    )

    result, logs = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is False
    assert item["changed"] is False
    message = item["steps"]["wildcard"]["message"]
    assert item["steps"]["wildcard"]["status"] == "failed"
    assert "403" in message
    assert "区域→DNS→编辑" in message
    assert "手工添加" in message
    assert "*." + DOMAIN in message
    # 失败后不继续后续步骤（dns/catch_all/worker 记 skipped）
    assert item["steps"]["dns"]["status"] == "skipped"
    assert item["steps"]["catch_all"]["status"] == "skipped"
    assert item["steps"]["worker"]["status"] == "skipped"
    assert result["summary"] == {"total": 1, "succeeded": 0, "failed": 1, "skipped": 0}
    assert TOKEN not in json.dumps(result, ensure_ascii=False)
    assert all(TOKEN not in line for line in logs)


def test_wildcard_step_partial_failure_fails_domain():
    session = FakeSession(
        build_responder(
            routing_enabled=True,
            catch_all=matched_catch_all_rule(),
            wildcard_mx_records=[],
            wildcard_txt_records=[],
            # 创建顺序 MX route1/route2/route3 + TXT：第 3 条 400 失败，其余成功
            wildcard_create_responses=[
                ok({"id": "rec-1"}),
                ok({"id": "rec-2"}),
                cf_error(400, 1004, "dns.records.invalid-content"),
                ok({"id": "rec-4"}),
            ],
        )
    )

    result, _ = run_provision(session)

    item = result["results"][0]
    assert item["ok"] is False
    message = item["steps"]["wildcard"]["message"]
    assert item["steps"]["wildcard"]["status"] == "failed"
    assert "部分失败" in message
    assert "route3.mx.cloudflare.net" in message
    assert "dns.records.invalid-content" in message
    assert "已创建的记录保留" in message
    post_calls = [c for c in session.calls if c["method"] == "POST" and c["path"].endswith("/dns_records")]
    assert len(post_calls) == 4
    # 已创建记录不回滚（无 DELETE 调用）
    assert not any(c["method"] == "DELETE" for c in session.calls)
    assert item["steps"]["dns"]["status"] == "skipped"
    assert item["steps"]["catch_all"]["status"] == "skipped"
    assert item["steps"]["worker"]["status"] == "skipped"
    assert result["summary"] == {"total": 1, "succeeded": 0, "failed": 1, "skipped": 0}


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    engine = create_configured_engine(
        "sqlite:///" + str(tmp_path / "cloudflare_provision.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(config_store_module, "engine", engine)
    monkeypatch.setattr(rebind_service, "engine", engine)
    return engine


def test_provision_route_is_registered():
    paths = {getattr(route, "path", "") for route in rebind_api.router.routes}
    assert "/chatgpt-rebind/provision/cloudflare" in paths


def test_api_provision_route_delegates_and_maps_value_error_to_400(isolated_db, monkeypatch):
    rebind_service.update_mail_config(
        {
            "domains": DOMAIN,
            "cloudflare_api_token": TOKEN,
            "cloudflare_worker_name": WORKER,
        }
    )
    captured = {}

    def fake_provision(config, domains):
        captured["config"] = dict(config or {})
        captured["domains"] = domains
        return {"ok": True, "summary": {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0}, "results": []}

    monkeypatch.setattr(rebind_api, "provision_cloudflare_email_routing", fake_provision)

    response = rebind_api.provision_cloudflare(CloudflareProvisionRequest(domains=None))
    assert response["ok"] is True
    assert captured["domains"] is None
    # 路由透传独立配置原文（服务层内部使用），并透传原始 domains 输入
    assert captured["config"]["cloudflare_api_token"] == TOKEN
    assert captured["config"]["cloudflare_worker_name"] == WORKER
    assert captured["config"]["domains"] == [DOMAIN]

    rebind_api.provision_cloudflare(CloudflareProvisionRequest(domains=["One.Example.com"]))
    assert captured["domains"] == ["One.Example.com"]

    def raising(config, domains):
        raise ValueError("请先在换绑配置中填写 cloudflare_api_token")

    monkeypatch.setattr(rebind_api, "provision_cloudflare_email_routing", raising)
    with pytest.raises(HTTPException) as excinfo:
        rebind_api.provision_cloudflare(CloudflareProvisionRequest(domains=None))
    assert excinfo.value.status_code == 400
    assert "cloudflare_api_token" in excinfo.value.detail
