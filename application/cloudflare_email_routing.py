# -*- coding: utf-8 -*-
"""Cloudflare Email Routing 自动 provision（cloud-mail 域名接入）。

按 Cloudflare 官方契约逐域名幂等执行（全部 GET-before-write）：

  1. GET  /client/v4/zones?name={domain}（可选 account.id）-> 解析 zone
  2. GET  /client/v4/zones/{zone_id}/email/routing -> 读取 enabled
  3. 未启用时 POST /client/v4/zones/{zone_id}/email/routing/dns（不带 name：该
     字段仅接受 zone 的子域名，顶级域名必须省略；官方端点一次性写入并锁定
     apex MX/SPF；apex 启用不用手工 dns_records 代替、禁止调用已废弃的 /enable）
  4. 通配子域投递（wildcard）：GET /client/v4/zones/{zone_id}/dns_records
     按 name=*.{domain} + type=MX/TXT 检测；缺失记录逐条 POST
     /client/v4/zones/{zone_id}/dns_records 创建（*.{domain} 的 MX
     route1/2/3.mx.cloudflare.net 优先级 10/20/30 与 SPF TXT——content 带字面
     双引号，ttl=1 自动、proxied=false）；SPF TXT 已存在但原始 content 无引号
     时 PATCH 规范化。官方 Email Routing API 无通配能力（见下），故经标准
     DNS Records API 自动创建；失败则域名 ok=False，部分失败不回滚、重跑幂等补齐
  5. GET  /client/v4/zones/{zone_id}/email/routing/dns -> 复核所需 DNS 记录
  6. GET+PUT /client/v4/zones/{zone_id}/email/routing/rules/catch_all
     actions=[{type: "worker", value: [worker_name]}], matchers=[{type: "all"}],
     enabled=true, name="cloud-mail catch-all"；已完全匹配则跳过 PUT
  7. GET+PATCH /client/v4/accounts/{aid}/workers/scripts/{worker_name}/settings
     -> 幂等合并 Worker 域名变量 DOMAINS / RANDOM_SUBDOMAIN_DOMAINS：json 数组
     中按小写比较追加当前域名（绑定缺失则自动创建 json 数组绑定），其余绑定
     深拷贝原样保留；两变量均已包含时跳过 PATCH。catch_all 失败仍继续执行本
     步骤；zone/email_routing/dns 失败则按 skipped 跳过。

Worker settings 官方契约核对（OpenAPI operationId worker-script-get-settings /
worker-script-patch-settings，developers.cloudflare.com Patch Settings 页）：

  - GET 返回 result.bindings（Worker 元数据与绑定配置）。
  - PATCH 官方请求体为 multipart/form-data，名为 settings 的 part 以
    application/json 承载 settings 对象（即 {"bindings": [...]}），等价 JSON
    视角为 {"settings": {"bindings": [...]}} 包装；官方 python/typescript SDK
    均按 settings 字段以 multipart 发送。所需 token 权限：Workers Scripts
    Write（GET 端点为 Workers Scripts Read）。
  - secret_text 绑定在 PATCH settings 请求模型中 text 为 Required（"The secret
    value to use."），而 GET 响应对 secret 脱敏不回传 text：GET->PATCH 回写无法
    还原 secret（缺 text 会被校验拒绝，乱填会覆盖丢失）。安全路径：Worker 现有
    bindings 含 secret_text 时绝不 PATCH，worker 步骤 failed，提示手动在控制台
    添加域名变量。
  - cloudflare_account_id 为空时 GET /client/v4/accounts 自动发现：恰好一个账号
    则采用；0 个或多个则 worker 步骤 failed 并提示填写 Account ID。

DNS Records API 官方契约核对（OpenAPI operationId
dns-records-for-a-zone-list-dns-records / dns-records-for-a-zone-create-dns-record；
注意线上 wire path 为 /zones/{zone_id}/dns_records，文档站 "/dns/records" 仅为 IA 命名）：

  - GET 列表支持 name（官方精确过滤，"convenience alias for name.exact"，大小写
    不敏感）与 type 过滤。通配检测选它而非 GET email/routing/dns：后者仅列出
    Email Routing 官方管理的 apex 记录，不含普通/手工 DNS 记录。
  - POST body：{type, name, content, priority?, ttl, proxied}；priority 对 MX
    必填（"Required for MX and URI records"）；ttl=1 表示自动（"Setting to 1
    means 'automatic'"）；MX/TXT 不可代理（proxied=false）。
  - 权限：token 组 "DNS Write"，控制台对应「区域 -> DNS -> 编辑」
    (#dns_records:edit)；403/401 时提示补权限或在控制台 DNS 页手工添加。
  - 存量规范化选 PATCH /zones/{zone_id}/dns_records/{record_id}（operationId
    dns-records-for-a-zone-patch-dns-record，"Update an existing DNS record"）：
    部分更新语义，仅修改提交字段，comment/tags 等未管理字段不受影响；PUT
    （dns-records-for-a-zone-update-dns-record）为 "Overwrite" 全量替换、未提交
    字段会被重置，故不选。权限同 DNS Write / #dns_records:edit。
  - SPF TXT content 带字面双引号（与控制台手工记录展示一致）；检测/比较前一律
    _strip_txt_quotes 剥离一层成对双引号——带引号（手工）与不带引号（旧代码
    创建）均视为已存在，仅原始 content 与带引号期望值不一致时规范化。

子域投递根因链（通配 MX ≠ 投递能力）：通配 MX 仅保证 DNS 可解析；Cloudflare
Email Routing 只处理已 onboarding 的域名，未启用子域被 CF 以 Domain not found
拒收（官方 Subdomains 文档：子域需逐一启用，每 zone ≤30 个含 apex）。因此换绑
分配子域后须由 ensure_subdomain_email_routing 逐一 onboarding（POST
email/routing/dns 带 name）；wildcard 通配记录保留（不回滚）但不承担投递能力。
enable 端点所需权限：Zone Settings Write（见上）；另需 Zone 读取（get_zone）
与 DNS Records 读取（幂等检测）。

配置来自独立 mail 配置（application.chatgpt_rebind.load_rebind_mail_config）：
cloudflare_api_token / cloudflare_worker_name 必填，cloudflare_account_id 可选。
secret 仅用于请求头，绝不进入返回值、错误消息或日志。
"""
from __future__ import annotations

import copy
import json
import re
import time
from typing import Any, Callable

import requests

from application.chatgpt_rebind import normalize_domains

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
REQUEST_TIMEOUT_SECONDS = 15.0
# 429/5xx 有界指数退避：最多额外重试 3 次（1s/2s/4s）。
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
CATCH_ALL_RULE_NAME = "cloud-mail catch-all"

# 通配子域投递记录（用户手工状态的自动化；与 Cloudflare Email Routing 基础设施对齐）
WILDCARD_MX_TARGETS = (
    ("route1.mx.cloudflare.net", 10),
    ("route2.mx.cloudflare.net", 20),
    ("route3.mx.cloudflare.net", 30),
)
WILDCARD_SPF_CONTENT = '"v=spf1 include:_spf.mx.cloudflare.net ~all"'
DNS_RECORD_TTL_AUTO = 1  # 官方：ttl=1 表示 automatic

DOMAIN_PATTERN = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

LogFn = Callable[[str], None]


class CloudflareAPIError(Exception):
    """Cloudflare HTTP 4xx/协议错误，保留解析后的 errors[] 详情（不含 secret）。"""

    def __init__(self, method: str, path: str, status_code: int, errors: Any = None):
        self.method = str(method)
        self.path = str(path)
        self.status_code = int(status_code)
        self.errors = errors if isinstance(errors, list) else []
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        parts: list[str] = []
        for item in self.errors[:5]:
            if isinstance(item, dict):
                code = item.get("code", "?")
                message = str(item.get("message") or "").strip()
                docs = str(item.get("documentation_url") or "").strip()
                text = f"code={code}" + (f" {message}" if message else "")
                if docs:
                    text += f" docs={docs}"
                parts.append(text)
            else:
                parts.append(str(item))
        detail = "; ".join(parts) if parts else "无错误详情"
        return f"Cloudflare {self.method} {self.path} HTTP {self.status_code}: {detail}"


def validate_domain(value: Any) -> str:
    """规范化单个域名并严格校验；非法输入抛 ValueError。"""
    domain = str(value or "").strip().lstrip("@").strip().rstrip(".").lower()
    if not domain:
        raise ValueError("域名为空，无法执行 Cloudflare provision")
    if len(domain) > 253 or not DOMAIN_PATTERN.match(domain):
        raise ValueError(f"域名格式不合法: {domain}")
    return domain


def _parse_error_payload(response: Any) -> list[dict[str, Any]]:
    try:
        data = response.json()
    except Exception:
        data = None
    errors = data.get("errors") if isinstance(data, dict) else None
    if not isinstance(errors, list) or not errors:
        try:
            body_text = str(response.text or "").strip()[:200]
        except Exception:
            body_text = ""
        return [{"code": -1, "message": body_text or "无响应体"}]
    parsed: list[dict[str, Any]] = []
    for item in errors[:5]:
        if isinstance(item, dict):
            parsed.append(
                {
                    "code": item.get("code"),
                    "message": str(item.get("message") or ""),
                    "documentation_url": str(item.get("documentation_url") or ""),
                }
            )
        else:
            parsed.append({"code": -1, "message": str(item)})
    return parsed


class CloudflareClient:
    """Cloudflare API v4 客户端：Bearer + JSON，10-20s 超时，429/5xx 有界退避。"""

    def __init__(
        self,
        api_token: str,
        *,
        account_id: str = "",
        base_url: str = CLOUDFLARE_API_BASE,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        retry_delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
        session: requests.Session | None = None,
    ):
        self._api_token = str(api_token or "")
        self._account_id = str(account_id or "").strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout)
        self._retry_delays = tuple(float(item) for item in retry_delays)
        self._session = session if session is not None else requests.Session()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._base_url + path
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "accept": "application/json",
        }
        request_kwargs: dict[str, Any] = {"headers": headers, "params": params, "timeout": self._timeout}
        if files:
            # multipart 契约（官方 PATCH settings）：不带 Content-Type，由 requests 生成 boundary
            request_kwargs["files"] = files
        else:
            headers["Content-Type"] = "application/json"
            request_kwargs["json"] = json_body
        response = None
        for attempt in range(len(self._retry_delays) + 1):
            if attempt:
                time.sleep(self._retry_delays[min(attempt - 1, len(self._retry_delays) - 1)])
            response = self._session.request(method, url, **request_kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                continue
            break
        if response is None:
            raise CloudflareAPIError(method, path, 0, [{"code": -1, "message": "no response"}])
        if response.status_code >= 400:
            raise CloudflareAPIError(method, path, response.status_code, _parse_error_payload(response))
        try:
            data = response.json()
        except ValueError as exc:
            raise CloudflareAPIError(
                method, path, response.status_code, [{"code": -1, "message": "响应不是有效 JSON"}]
            ) from exc
        if isinstance(data, dict) and data.get("success") is False:
            raise CloudflareAPIError(method, path, response.status_code, _parse_error_payload(response))
        return data if isinstance(data, dict) else {"result": data}

    def verify_token(self) -> dict[str, Any]:
        return self.request("GET", "/user/tokens/verify")

    def get_zone(self, domain: str) -> dict[str, Any] | None:
        params: dict[str, str] = {"name": domain}
        if self._account_id:
            params["account.id"] = self._account_id
        data = self.request("GET", "/zones", params=params)
        zones = data.get("result") if isinstance(data, dict) else None
        if isinstance(zones, list) and zones and isinstance(zones[0], dict):
            return zones[0]
        return None

    def get_email_routing(self, zone_id: str) -> dict[str, Any]:
        data = self.request("GET", f"/zones/{zone_id}/email/routing")
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def enable_email_routing_dns(self, zone_id: str, *, name: str = "") -> dict[str, Any]:
        """官方启用端点：自动写入并锁定 MX/SPF，替代手工 dns_records 与废弃 /enable。

        两种调用形态（CF 2007: name 仅接受 zone 的子域名，must be a subdomain of zone）：
          - apex 域启用：必须省略 name（body 为 {}）——顶级域名 zone 传 name 会被拒；
          - 子域启用：必须传 name=子域 FQDN（body 为 {"name": ...}），用于子域逐一
            onboarding（官方 Subdomains 文档）。
        所需权限：Zone Settings Write（x-cfPermissionsRequired
        com.cloudflare.api.account.zone.email.routing.config.update）。
        """
        body: dict[str, Any] = {"name": name} if name else {}
        data = self.request("POST", f"/zones/{zone_id}/email/routing/dns", json_body=body)
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def get_email_routing_dns(self, zone_id: str) -> dict[str, Any] | list[dict[str, Any]]:
        data = self.request("GET", f"/zones/{zone_id}/email/routing/dns")
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, (dict, list)) else {}

    def get_catch_all(self, zone_id: str) -> dict[str, Any]:
        data = self.request("GET", f"/zones/{zone_id}/email/routing/rules/catch_all")
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def put_catch_all(self, zone_id: str, *, worker_name: str) -> dict[str, Any]:
        data = self.request(
            "PUT",
            f"/zones/{zone_id}/email/routing/rules/catch_all",
            json_body=_catch_all_payload(worker_name),
        )
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def list_accounts(self) -> tuple[list[dict[str, Any]], int | None]:
        """GET /accounts（Account ID 自动发现）；返回 (账号列表, result_info.total_count 或 None)。"""
        data = self.request("GET", "/accounts", params={"per_page": 5})
        result = data.get("result") if isinstance(data, dict) else None
        accounts = [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []
        info = data.get("result_info") if isinstance(data, dict) else None
        total_count: int | None = None
        if isinstance(info, dict):
            try:
                total_count = int(info.get("total_count"))
            except (TypeError, ValueError):
                total_count = None
        return accounts, total_count

    def get_worker_settings(self, account_id: str, worker_name: str) -> dict[str, Any]:
        data = self.request("GET", f"/accounts/{account_id}/workers/scripts/{worker_name}/settings")
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def patch_worker_settings(
        self, account_id: str, worker_name: str, bindings: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """官方 PATCH Settings：multipart 的 settings part 承载 {"bindings": [...]}（见模块 docstring）。"""
        return self.request(
            "PATCH",
            f"/accounts/{account_id}/workers/scripts/{worker_name}/settings",
            files={"settings": (None, json.dumps({"bindings": bindings}, ensure_ascii=False), "application/json")},
        )

    def list_dns_records(self, zone_id: str, *, name: str, record_type: str) -> list[dict[str, Any]]:
        """GET /dns_records：name 为官方精确过滤（alias for name.exact，大小写不敏感）。"""
        data = self.request("GET", f"/zones/{zone_id}/dns_records", params={"name": name, "type": record_type})
        result = data.get("result") if isinstance(data, dict) else None
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def create_dns_record(self, zone_id: str, body: dict[str, Any]) -> dict[str, Any]:
        data = self.request("POST", f"/zones/{zone_id}/dns_records", json_body=body)
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def update_dns_record(self, zone_id: str, record_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """官方 PATCH /dns_records/{record_id}：部分更新语义（见模块 docstring，未提交字段不受影响）。"""
        data = self.request("PATCH", f"/zones/{zone_id}/dns_records/{record_id}", json_body=body)
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}


def _catch_all_payload(worker_name: str) -> dict[str, Any]:
    return {
        "actions": [{"type": "worker", "value": [worker_name]}],
        "matchers": [{"type": "all"}],
        "enabled": True,
        "name": CATCH_ALL_RULE_NAME,
    }


def _catch_all_matches(rule: Any, worker_name: str) -> bool:
    """与目标规则完全一致（name/actions/matchers/enabled）才视为已配置。"""
    if not isinstance(rule, dict) or not rule:
        return False
    if not rule.get("enabled"):
        return False
    if str(rule.get("name") or "") != CATCH_ALL_RULE_NAME:
        return False
    actions = rule.get("actions")
    if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
        return False
    action = actions[0]
    if str(action.get("type") or "") != "worker":
        return False
    value = action.get("value")
    values = value if isinstance(value, list) else [value]
    if [str(item or "") for item in values] != [worker_name]:
        return False
    matchers = rule.get("matchers")
    if not isinstance(matchers, list) or len(matchers) != 1 or not isinstance(matchers[0], dict):
        return False
    return str(matchers[0].get("type") or "") == "all"


def _summarize_dns_records(dns_result: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(dns_result, list):
        items = dns_result
    elif isinstance(dns_result, dict):
        # 当前官方查询响应使用 result.record；兼容早期 record_set 形态。
        items = dns_result.get("record")
        if not isinstance(items, list):
            items = dns_result.get("record_set")
    else:
        items = []
    if not isinstance(items, list):
        return records
    for item in items:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "type": str(item.get("type") or ""),
                "name": str(item.get("name") or ""),
                "content": str(item.get("content") or ""),
                "priority": item.get("priority"),
                "locked": bool(item.get("locked")),
            }
        )
    return records


def _error_text(exc: Exception) -> str:
    if isinstance(exc, CloudflareAPIError):
        return str(exc)
    return f"Cloudflare 网络错误: {exc}"


def _fail_step(steps: dict[str, Any], key: str, exc: Exception) -> str:
    message = _error_text(exc)
    steps[key] = {"status": "failed", "message": message}
    return message


def _skip_steps(steps: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        steps[key] = {"status": "skipped", "message": "前置步骤失败，跳过"}


WORKER_DOMAIN_VARIABLES = ("DOMAINS", "RANDOM_SUBDOMAIN_DOMAINS")
ACCOUNT_ID_HINT = "请在换绑配置中填写 Cloudflare Account ID（控制台域名概览页右下角）"


def _discover_account_id(cf_client: CloudflareClient, log: LogFn) -> tuple[str, str]:
    """cloudflare_account_id 为空时 GET /accounts 自动发现；返回 (account_id, error)。"""
    try:
        accounts, total_count = cf_client.list_accounts()
    except (CloudflareAPIError, requests.RequestException) as exc:
        return "", f"自动发现 Cloudflare Account ID 失败: {_error_text(exc)}。{ACCOUNT_ID_HINT}"
    effective_total = total_count if total_count is not None else len(accounts)
    if effective_total == 1 and accounts:
        account_id = str(accounts[0].get("id") or "").strip()
        if account_id:
            log(f"[cloudflare-provision] 自动发现 Cloudflare Account ID: {account_id}")
            return account_id, ""
        return "", f"自动发现 Cloudflare Account ID 失败: 账号缺少 id 字段。{ACCOUNT_ID_HINT}"
    if effective_total <= 1:
        return "", f"自动发现 Cloudflare Account ID 失败: 未发现可用账号（0 个）。{ACCOUNT_ID_HINT}"
    return "", f"自动发现 Cloudflare Account ID 失败: 发现 {effective_total} 个账号，无法自动选择。{ACCOUNT_ID_HINT}"


def _wildcard_expected_records(domain: str) -> list[dict[str, Any]]:
    """通配记录目标状态：*.{domain} 的 MX route1/2/3（10/20/30）与 SPF TXT。"""
    name = f"*.{domain}"
    records = [
        {
            "type": "MX",
            "name": name,
            "content": content,
            "priority": priority,
            "ttl": DNS_RECORD_TTL_AUTO,
            "proxied": False,
        }
        for content, priority in WILDCARD_MX_TARGETS
    ]
    records.append(
        {
            "type": "TXT",
            "name": name,
            "content": WILDCARD_SPF_CONTENT,
            "ttl": DNS_RECORD_TTL_AUTO,
            "proxied": False,
        }
    )
    return records


def _strip_txt_quotes(value: Any) -> str:
    """剥离一层成对双引号（TXT content 在控制台展示/录入可能带引号，API 内容形态不一）。"""
    text = str(value or "").strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    return text


def _dns_permission_hint(domain: str) -> str:
    return (
        f"请给 API Token 增加“区域→DNS→编辑”权限后重试，或在 Cloudflare 控制台 DNS 页手工添加"
        f"（*.{domain} 的 MX route1/2/3 优先级 10/20/30 与 SPF TXT）"
    )


def _provision_wildcard_step(
    cf_client: CloudflareClient, domain: str, zone_id: str, log: LogFn
) -> tuple[str, str, bool]:
    """通配子域 MX/SPF：官方 DNS Records API 检测 + 逐条创建/规范化；返回 (status, message, changed)。

    MX 按内容匹配（优先级不参与判定，避免因既有记录优先级差异重复创建）；SPF TXT
    一律先剥离一层成对双引号再比较——带引号（手工记录）与不带引号（旧代码创建）均视为
    已存在；存在但原始 content 与带引号期望值不一致时 PATCH 规范化（body 沿用记录现有
    type/name/ttl/proxied）。部分失败不回滚已创建记录（重跑幂等补齐）；401/403 提示补
    权限或手工添加。
    """
    name = f"*.{domain}"
    try:
        mx_records = cf_client.list_dns_records(zone_id, name=name, record_type="MX")
        txt_records = cf_client.list_dns_records(zone_id, name=name, record_type="TXT")
    except (CloudflareAPIError, requests.RequestException) as exc:
        return "failed", _error_text(exc), False
    existing_mx = {str(item.get("content") or "").strip().rstrip(".").lower() for item in mx_records}
    existing_txt = {_strip_txt_quotes(item.get("content")).lower() for item in txt_records}
    spf_stripped = _strip_txt_quotes(WILDCARD_SPF_CONTENT).lower()
    missing = []
    for record in _wildcard_expected_records(domain):
        if record["type"] == "MX":
            present = record["content"].strip().rstrip(".").lower() in existing_mx
        else:
            # TXT 期望值同样先剥引号再比较（期望 content 本身带字面双引号）
            present = _strip_txt_quotes(record["content"]).lower() in existing_txt
        if not present:
            missing.append(record)
    # 存量规范化：SPF TXT 已存在但原始 content 与带引号期望值不一致（如旧代码写入的无引号形态）
    normalize_record: dict[str, Any] | None = None
    for item in txt_records:
        raw = str(item.get("content") or "")
        if _strip_txt_quotes(raw).lower() == spf_stripped and raw.strip() != WILDCARD_SPF_CONTENT:
            normalize_record = item
            break
    if not missing and normalize_record is None:
        return "skipped", f"通配记录已配置（*.{domain} 的 MX route1/2/3 与 SPF TXT），跳过创建", False
    created: list[str] = []
    failures: list[str] = []
    for record in missing:
        label = f"{record['type']} {record['name']} ({record['content']})"
        try:
            cf_client.create_dns_record(zone_id, record)
        except CloudflareAPIError as exc:
            if exc.status_code in (401, 403):
                return "failed", f"创建通配记录 {label} 失败: {_error_text(exc)}。{_dns_permission_hint(domain)}", False
            failures.append(f"{label}: {_error_text(exc)}")
        except requests.RequestException as exc:
            failures.append(f"{label}: {_error_text(exc)}")
        else:
            created.append(label)
            log(f"[cloudflare-provision] {domain} 已创建通配记录 {label}")
    normalized = False
    if normalize_record is not None:
        record_id = str(normalize_record.get("id") or "").strip()
        if not record_id:
            failures.append(f"规范化 TXT {name}: 记录缺少 id，无法更新")
        else:
            label = f"TXT {name} ({WILDCARD_SPF_CONTENT})"
            body = {
                "type": "TXT",
                "name": str(normalize_record.get("name") or name),
                "content": WILDCARD_SPF_CONTENT,
                "ttl": normalize_record.get("ttl", DNS_RECORD_TTL_AUTO),
                "proxied": bool(normalize_record.get("proxied")),
            }
            try:
                cf_client.update_dns_record(zone_id, record_id, body)
            except CloudflareAPIError as exc:
                if exc.status_code in (401, 403):
                    return "failed", f"规范化通配记录 {label} 失败: {_error_text(exc)}。{_dns_permission_hint(domain)}", False
                failures.append(f"规范化 {label}: {_error_text(exc)}")
            except requests.RequestException as exc:
                failures.append(f"规范化 {label}: {_error_text(exc)}")
            else:
                normalized = True
                log(f"[cloudflare-provision] {domain} 已规范化通配 SPF TXT 引号 {label}")
    if failures:
        return "failed", "通配记录写入部分失败: " + "; ".join(failures) + "（已创建的记录保留，重跑将幂等补齐）", False
    if created:
        message = f"已创建通配记录 {', '.join(created)}"
        if normalized:
            message += "，并规范化 SPF TXT 引号"
        return "success", message, True
    return "success", f"已规范化通配 SPF TXT 引号（*.{domain}）", True


def _merge_worker_domain_bindings(
    bindings: list[dict[str, Any]], domain: str
) -> tuple[list[dict[str, Any]], bool, str]:
    """把 domain 合并进 DOMAINS/RANDOM_SUBDOMAIN_DOMAINS；返回 (新 bindings, changed, error)。

    其余绑定深拷贝、顺序不变；json 值非数组（或同名绑定类型非 json）时报错且不做修改。
    """
    merged = copy.deepcopy(bindings)
    changed = False
    for name in WORKER_DOMAIN_VARIABLES:
        target: dict[str, Any] | None = None
        for item in merged:
            if isinstance(item, dict) and str(item.get("name") or "") == name:
                target = item
                break
        if target is None:
            merged.append({"type": "json", "name": name, "json": [domain]})
            changed = True
            continue
        binding_type = str(target.get("type") or "")
        if binding_type != "json":
            return merged, False, f"Worker 绑定 {name} 的类型为 {binding_type or '未知'}（期望 json），不做修改"
        value = target.get("json")
        if not isinstance(value, list):
            return merged, False, f"Worker 绑定 {name} 的 json 值不是数组，不做修改"
        existing = {str(item or "").strip().lower() for item in value}
        if domain.lower() not in existing:
            target["json"] = [*value, domain]
            changed = True
    return merged, changed, ""


def _provision_worker_step(
    cf_client: CloudflareClient, domain: str, worker_name: str, account_id: str, log: LogFn
) -> tuple[str, str, bool]:
    """同步 Worker 域名变量；返回 (status: success|skipped|failed, message, changed)。"""
    effective_account_id = str(account_id or "").strip()
    if not effective_account_id:
        effective_account_id, discover_error = _discover_account_id(cf_client, log)
        if discover_error:
            return "failed", discover_error, False
    try:
        settings = cf_client.get_worker_settings(effective_account_id, worker_name)
    except (CloudflareAPIError, requests.RequestException) as exc:
        return "failed", _error_text(exc), False
    bindings = settings.get("bindings")
    if not isinstance(bindings, list):
        return "failed", "Worker settings 响应缺少 bindings 列表", False
    # 安全路径：secret_text 绑定无法经 GET->PATCH 还原（text 为 Required 且 GET 脱敏），绝不回写
    for item in bindings:
        if isinstance(item, dict) and str(item.get("type") or "") == "secret_text":
            return "failed", "该 Worker 含 secret 绑定，请手动在控制台添加域名变量", False
    merged, changed, merge_error = _merge_worker_domain_bindings(bindings, domain)
    if merge_error:
        return "failed", merge_error, False
    if not changed:
        return "skipped", f"Worker 的 DOMAINS/RANDOM_SUBDOMAIN_DOMAINS 均已包含域名 {domain}，跳过 PATCH", False
    try:
        cf_client.patch_worker_settings(effective_account_id, worker_name, merged)
    except (CloudflareAPIError, requests.RequestException) as exc:
        return "failed", _error_text(exc), False
    return "success", f"已将域名 {domain} 追加到 Worker 的 DOMAINS/RANDOM_SUBDOMAIN_DOMAINS", True


def _provision_domain(
    cf_client: CloudflareClient, domain: str, worker_name: str, account_id: str, log: LogFn
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "domain": domain,
        "ok": False,
        "changed": False,
        "zone_id": "",
        "steps": {},
        "error": "",
    }
    steps: dict[str, Any] = result["steps"]
    log(f"[cloudflare-provision] {domain} 开始")

    # 1) zone 解析（GET-before-write）
    try:
        zone = cf_client.get_zone(domain)
    except (CloudflareAPIError, requests.RequestException) as exc:
        result["error"] = _fail_step(steps, "zone", exc)
        _skip_steps(steps, ("email_routing", "wildcard", "dns", "catch_all", "worker"))
        log(f"[cloudflare-provision] {domain} 失败: {result['error']}")
        return result
    if not isinstance(zone, dict) or not str(zone.get("id") or ""):
        message = f"Cloudflare 未找到域名 {domain} 对应的 zone"
        steps["zone"] = {"status": "failed", "message": message}
        result["error"] = message
        _skip_steps(steps, ("email_routing", "wildcard", "dns", "catch_all", "worker"))
        log(f"[cloudflare-provision] {domain} 失败: {message}")
        return result
    zone_id = str(zone.get("id"))
    result["zone_id"] = zone_id
    steps["zone"] = {"status": "success", "message": f"zone 已找到（status={str(zone.get('status') or 'unknown')}）"}

    # 2) Email Routing：已启用跳过；未启用走官方 /email/routing/dns（自动 MX/SPF）
    try:
        routing = cf_client.get_email_routing(zone_id)
        if routing.get("enabled"):
            steps["email_routing"] = {"status": "skipped", "message": "Email Routing 已启用，跳过 DNS provisioning"}
        else:
            cf_client.enable_email_routing_dns(zone_id)
            result["changed"] = True
            steps["email_routing"] = {
                "status": "success",
                "message": "已通过官方 /email/routing/dns 启用（自动写入并锁定 MX/SPF 记录）",
            }
    except (CloudflareAPIError, requests.RequestException) as exc:
        result["error"] = _fail_step(steps, "email_routing", exc)
        _skip_steps(steps, ("wildcard", "dns", "catch_all", "worker"))
        log(f"[cloudflare-provision] {domain} 失败: {result['error']}")
        return result

    # 3) 通配子域投递：DNS Records API 幂等补齐 *.{domain} 的 MX/SPF（官方 Email Routing API 无通配）
    try:
        wildcard_status, wildcard_message, wildcard_changed = _provision_wildcard_step(cf_client, domain, zone_id, log)
    except (CloudflareAPIError, requests.RequestException) as exc:
        wildcard_status, wildcard_message, wildcard_changed = "failed", _error_text(exc), False
    steps["wildcard"] = {"status": wildcard_status, "message": wildcard_message}
    if wildcard_status == "failed":
        result["error"] = wildcard_message
        _skip_steps(steps, ("dns", "catch_all", "worker"))
        log(f"[cloudflare-provision] {domain} 失败: {wildcard_message}")
        return result
    if wildcard_changed:
        result["changed"] = True

    # 4) DNS 复核（GET /email/routing/dns）
    try:
        dns_result = cf_client.get_email_routing_dns(zone_id)
    except (CloudflareAPIError, requests.RequestException) as exc:
        result["error"] = _fail_step(steps, "dns", exc)
        _skip_steps(steps, ("catch_all", "worker"))
        log(f"[cloudflare-provision] {domain} 失败: {result['error']}")
        return result
    records = _summarize_dns_records(dns_result)
    mx_records = [item for item in records if str(item.get("type") or "").upper() == "MX"]
    if not mx_records:
        message = "复核未发现 MX 记录，Email Routing DNS 可能未生效"
        steps["dns"] = {"status": "failed", "message": message, "records": records}
        result["error"] = message
        _skip_steps(steps, ("catch_all", "worker"))
        log(f"[cloudflare-provision] {domain} 失败: {message}")
        return result
    steps["dns"] = {
        "status": "success",
        "message": f"复核到 {len(mx_records)} 条 MX（共 {len(records)} 条官方记录）",
        "records": records,
    }

    # 5) catch-all -> worker：完全匹配则 skipped，否则 PUT（幂等）；失败仍继续执行 worker 步骤
    try:
        rule = cf_client.get_catch_all(zone_id)
        if _catch_all_matches(rule, worker_name):
            steps["catch_all"] = {"status": "skipped", "message": "catch-all 已完全匹配目标 worker 规则，跳过 PUT"}
        else:
            cf_client.put_catch_all(zone_id, worker_name=worker_name)
            result["changed"] = True
            steps["catch_all"] = {"status": "success", "message": f"catch-all 已更新为 worker={worker_name}"}
    except (CloudflareAPIError, requests.RequestException) as exc:
        result["error"] = _fail_step(steps, "catch_all", exc)
        log(f"[cloudflare-provision] {domain} catch_all 失败: {result['error']}")

    # 6) worker 域名变量：DOMAINS/RANDOM_SUBDOMAIN_DOMAINS 追加当前域名（幂等 GET-before-write）
    try:
        worker_status, worker_message, worker_changed = _provision_worker_step(
            cf_client, domain, worker_name, account_id, log
        )
    except (CloudflareAPIError, requests.RequestException) as exc:
        worker_status, worker_message, worker_changed = "failed", _error_text(exc), False
    steps["worker"] = {"status": worker_status, "message": worker_message}
    if worker_status == "failed":
        result["error"] = worker_message
        result["ok"] = False
        log(f"[cloudflare-provision] {domain} 失败: {worker_message}")
        return result
    if worker_changed:
        result["changed"] = True
    # catch_all 失败但 worker 成功时域名仍计 failed（error 非空），保持 summary 准确
    if not result["error"]:
        result["ok"] = True
    log(f"[cloudflare-provision] {domain} 完成")
    return result


def provision_cloudflare_email_routing(
    mail_config: dict[str, Any],
    domains: Any = None,
    *,
    client: CloudflareClient | None = None,
    log_fn: LogFn | None = None,
) -> dict[str, Any]:
    """逐域名幂等启用 Cloudflare Email Routing 并指向 cloud-mail worker。

    - mail_config：load_rebind_mail_config() 的原始配置（含原文 secret，仅内部使用）。
    - domains：请求传入的原始域名输入；空值/None 时使用配置中的全部域名。
    - 配置缺失或域名校验失败抛 ValueError（API 层转 HTTP 400）。
    - 全局 token 校验失败 -> ok=False 结构化返回；单域名失败不中断后续域名。
    - 返回值与日志均不含 secret。
    """
    config = mail_config if isinstance(mail_config, dict) else {}
    token = str(config.get("cloudflare_api_token") or "").strip()
    worker_name = str(config.get("cloudflare_worker_name") or "").strip()
    account_id = str(config.get("cloudflare_account_id") or "").strip()
    if not token:
        raise ValueError("请先在换绑配置中填写 cloudflare_api_token")
    if not worker_name:
        raise ValueError("请先在换绑配置中填写 cloudflare_worker_name")

    configured_domains = config.get("domains")
    if (
        domains is None
        or (isinstance(domains, str) and not str(domains).strip())
        or (isinstance(domains, (list, tuple, set)) and not any(str(item or "").strip() for item in domains))
    ):
        raw_domains = configured_domains
    else:
        raw_domains = domains

    validated: list[str] = []
    seen: set[str] = set()
    for item in normalize_domains(raw_domains):
        domain = validate_domain(item)
        if domain in seen:
            continue
        seen.add(domain)
        validated.append(domain)
    if not validated:
        raise ValueError("没有可 provision 的域名：请在配置 domains 或请求中传入 domains")

    log: LogFn = log_fn if callable(log_fn) else (lambda _message: None)
    cf_client = client if client is not None else CloudflareClient(token, account_id=account_id)

    try:
        verify = cf_client.verify_token()
        token_status = ""
        if isinstance(verify, dict) and isinstance(verify.get("result"), dict):
            token_status = str(verify["result"].get("status") or "").strip().lower()
        if token_status != "active":
            raise CloudflareAPIError(
                "GET",
                "/user/tokens/verify",
                200,
                [{"code": -1, "message": "token status is " + (token_status or "unknown")}],
            )
        log(f"[cloudflare-provision] token 校验通过 status={token_status}")
    except (CloudflareAPIError, requests.RequestException) as exc:
        message = f"Cloudflare API token 校验失败: {_error_text(exc)}"
        log(f"[cloudflare-provision] {message}")
        return {
            "ok": False,
            "error": message,
            "summary": {"total": len(validated), "succeeded": 0, "failed": len(validated), "skipped": 0},
            "results": [
                {"domain": domain, "ok": False, "zone_id": "", "steps": {}, "error": "Cloudflare API token 校验失败"}
                for domain in validated
            ],
        }

    results = [_provision_domain(cf_client, domain, worker_name, account_id, log) for domain in validated]
    failed = sum(1 for item in results if not item.get("ok"))
    skipped = sum(1 for item in results if item.get("ok") and not item.get("changed"))
    succeeded = len(results) - failed - skipped
    return {
        "ok": failed == 0,
        "summary": {"total": len(results), "succeeded": succeeded, "failed": failed, "skipped": skipped},
        "results": results,
    }


def ensure_subdomain_email_routing(
    cf_client: CloudflareClient, zone_id: str, subdomain: str, *, log_fn: LogFn | None = None
) -> dict[str, Any]:
    """为已分配子域幂等启用 Email Routing（子域必须逐一 onboarding 才有投递能力）。

    根因链：通配 MX（*.{domain}）只保证 DNS 可解析；Cloudflare Email Routing 仅
    处理已 onboarding 的域名，未启用子域被 CF 以 Domain not found 拒收（官方
    Subdomains 文档：子域需逐一启用，每 zone ≤30 个含 apex）。wildcard 通配记录
    保留（不回滚）但不承担投递能力。enable 端点所需权限：Zone Settings Write。

    幂等流程（GET-before-write）：
      1. GET /zones/{zone_id}/dns_records?name={subdomain}&type=MX —— onboarding
         会为子域创建托管 MX；已有 MX 视为已启用，返回 {"ok": True, "changed": False}；
      2. 缺失时 POST /zones/{zone_id}/email/routing/dns，body {"name": subdomain}
         （子域必须传 name；apex 启用必须省略 name，见
         CloudflareClient.enable_email_routing_dns 的两种调用形态）；
      3. 成功后复核 dns_records 出现 MX，返回 {"ok": True, "changed": True}。
    失败（API 错误/网络错误）抛 CloudflareAPIError 或 requests.RequestException；
    启用后复核缺失抛携带说明的 CloudflareAPIError，由调用方结构化处理。
    """
    log: LogFn = log_fn if callable(log_fn) else (lambda _message: None)
    mx_records = cf_client.list_dns_records(zone_id, name=subdomain, record_type="MX")
    if mx_records:
        log(f"[cloudflare-provision] 子域 {subdomain} Email Routing 已启用（已有 MX），跳过")
        return {"ok": True, "changed": False}
    log(f"[cloudflare-provision] 为子域 {subdomain} 启用 Email Routing（onboarding）")
    cf_client.enable_email_routing_dns(zone_id, name=subdomain)
    verified = cf_client.list_dns_records(zone_id, name=subdomain, record_type="MX")
    if not verified:
        raise CloudflareAPIError(
            "POST",
            f"/zones/{zone_id}/email/routing/dns",
            200,
            [{"code": -1, "message": f"子域 {subdomain} Email Routing 启用后未复核到 MX 记录"}],
        )
    return {"ok": True, "changed": True}
