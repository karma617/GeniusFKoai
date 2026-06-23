"""SMS 号码池黑名单 API 与仓储测试。"""
from __future__ import annotations

import json


class _FakeResp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.text = json.dumps(data)

    def json(self):
        return self._data


class _FakeSession:
    def __init__(self, data):
        self.data = data

    def post(self, _url, **_kwargs):
        return _FakeResp(self.data)


def test_list_blacklist_empty(client):
    resp = client.get("/api/sms-pool/blacklist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_add_blacklist_creates_record(client):
    resp = client.post(
        "/api/sms-pool/blacklist",
        json={
            "phone": "+15822063090",
            "relay_url": "https://mail-api.yuecheng.shop/api/public/message?key=eca_tr_GnJcbk",
            "reason": "oas_error",
            "error_code": "OAS_ERROR",
            "task_id": "task_demo",
            "error_message": "createMemberAccount risk",
        },
    )
    assert resp.status_code == 200
    item = resp.json()
    assert item["phone_e164"] == "+15822063090"
    assert item["relay_host"] == "mail-api.yuecheng.shop"
    assert item["reason"] == "oas_error"
    assert item["fail_count"] == 1


def test_add_blacklist_increments_fail_count(client):
    payload = {"phone": "+15822063090", "reason": "oas_error"}
    client.post("/api/sms-pool/blacklist", json=payload)
    second = client.post("/api/sms-pool/blacklist", json=payload).json()
    assert second["fail_count"] == 2


def test_add_blacklist_rejects_empty_phone(client):
    resp = client.post("/api/sms-pool/blacklist", json={"phone": "   "})
    assert resp.status_code == 400


def test_remove_blacklist_ok(client):
    client.post("/api/sms-pool/blacklist", json={"phone": "+15822063090"})
    resp = client.delete("/api/sms-pool/blacklist/+15822063090")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    list_resp = client.get("/api/sms-pool/blacklist")
    assert list_resp.json()["total"] == 0


def test_remove_blacklist_missing(client):
    resp = client.delete("/api/sms-pool/blacklist/+19999999999")
    assert resp.status_code == 404


def test_clear_blacklist(client):
    for phone in ("+15822063090", "+15822063091"):
        client.post("/api/sms-pool/blacklist", json={"phone": phone})
    resp = client.delete("/api/sms-pool/blacklist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["removed"] == 2
    assert client.get("/api/sms-pool/blacklist").json()["total"] == 0


def test_release_queue_empty(client, monkeypatch, tmp_path):
    from platforms.gopay import sms_channel

    monkeypatch.setattr(sms_channel, "SMS_RELEASE_QUEUE_PATH", tmp_path / "release-queue.json")
    monkeypatch.setattr(sms_channel, "SMS_RELEASE_LOG_PATH", tmp_path / "release-log.jsonl")

    resp = client.get("/api/sms-pool/release-queue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["logs"] == []


def test_release_queue_snapshot_api_masks_secret(client, monkeypatch, tmp_path):
    from platforms.gopay import sms_channel

    queue_path = tmp_path / "release-queue.json"
    log_path = tmp_path / "release-log.jsonl"
    monkeypatch.setattr(sms_channel, "SMS_RELEASE_QUEUE_PATH", queue_path)
    monkeypatch.setattr(sms_channel, "SMS_RELEASE_LOG_PATH", log_path)
    now = int(sms_channel.time.time())
    queue_path.write_text(json.dumps([{
        "order_id": "ORDER_API",
        "api_key": "SECRET_KEY_123456",
        "base_url": "https://api.smspool.net",
        "phone": "+639272971374",
        "reason": "cooldown",
        "created_at": now - 60,
        "updated_at": now - 30,
        "attempts": 1,
        "next_attempt_at": now + 30,
        "last_response": {"success": 0, "message": "try later", "api_key": "SECRET_KEY_123456"},
    }]), encoding="utf-8")

    resp = client.get("/api/sms-pool/release-queue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["phone"] == "+639272971374"
    assert body["items"][0]["api_key_masked"] != "SECRET_KEY_123456"
    assert "SECRET_KEY_123456" not in json.dumps(body)


def test_release_queue_process_api_releases_item(client, monkeypatch, tmp_path):
    from platforms.gopay import sms_channel

    queue_path = tmp_path / "release-queue.json"
    log_path = tmp_path / "release-log.jsonl"
    monkeypatch.setattr(sms_channel, "SMS_RELEASE_QUEUE_PATH", queue_path)
    monkeypatch.setattr(sms_channel, "SMS_RELEASE_LOG_PATH", log_path)
    monkeypatch.setattr(sms_channel, "_new_session", lambda: _FakeSession({"success": 1, "message": "cancelled"}))
    now = int(sms_channel.time.time())
    queue_path.write_text(json.dumps([{
        "order_id": "ORDER_DONE",
        "api_key": "KEY",
        "base_url": "https://api.smspool.net",
        "phone": "+639272971374",
        "reason": "cooldown",
        "created_at": now - 60,
        "updated_at": now - 30,
        "attempts": 0,
        "next_attempt_at": now - 1,
        "last_response": {},
    }]), encoding="utf-8")

    resp = client.post("/api/sms-pool/release-queue/process")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempted"] == 1
    assert body["released"] == 1
    assert body["total"] == 0
    assert any(item["status"] == "success" and item["order_id"] == "ORDER_DONE" for item in body["logs"])


def test_release_queue_process_single_and_remove(client, monkeypatch, tmp_path):
    from platforms.gopay import sms_channel

    queue_path = tmp_path / "release-queue.json"
    log_path = tmp_path / "release-log.jsonl"
    monkeypatch.setattr(sms_channel, "SMS_RELEASE_QUEUE_PATH", queue_path)
    monkeypatch.setattr(sms_channel, "SMS_RELEASE_LOG_PATH", log_path)
    monkeypatch.setattr(sms_channel, "_new_session", lambda: _FakeSession({"success": 0, "message": "try later"}))
    now = int(sms_channel.time.time())
    queue_path.write_text(json.dumps([
        {
            "order_id": "ORDER_ONE",
            "api_key": "KEY",
            "base_url": "https://api.smspool.net",
            "phone": "+639272971374",
            "reason": "cooldown",
            "created_at": now - 60,
            "updated_at": now - 30,
            "attempts": 0,
            "next_attempt_at": now + 300,
            "last_response": {},
        },
        {
            "order_id": "ORDER_TWO",
            "api_key": "KEY",
            "base_url": "https://api.smspool.net",
            "phone": "+639272971375",
            "reason": "cooldown",
            "created_at": now - 60,
            "updated_at": now - 30,
            "attempts": 0,
            "next_attempt_at": now + 300,
            "last_response": {},
        },
    ]), encoding="utf-8")

    resp = client.post("/api/sms-pool/release-queue/ORDER_ONE/process")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempted"] == 1
    assert body["released"] == 0
    assert body["total"] == 2
    assert any(item["order_id"] == "ORDER_ONE" and item["attempts"] == 1 for item in body["items"])
    assert any(item["order_id"] == "ORDER_TWO" and item["attempts"] == 0 for item in body["items"])

    remove_resp = client.delete("/api/sms-pool/release-queue/ORDER_ONE")
    assert remove_resp.status_code == 200
    remove_body = remove_resp.json()
    assert remove_body["total"] == 1
    assert remove_body["items"][0]["order_id"] == "ORDER_TWO"


def test_release_logs_clear_api(client, monkeypatch, tmp_path):
    from platforms.gopay import sms_channel

    queue_path = tmp_path / "release-queue.json"
    log_path = tmp_path / "release-log.jsonl"
    monkeypatch.setattr(sms_channel, "SMS_RELEASE_QUEUE_PATH", queue_path)
    monkeypatch.setattr(sms_channel, "SMS_RELEASE_LOG_PATH", log_path)
    queue_path.write_text("[]", encoding="utf-8")
    log_path.write_text(json.dumps({"status": "failed", "order_id": "ORDER_LOG"}) + "\n", encoding="utf-8")

    resp = client.delete("/api/sms-pool/release-logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed"] == 1
    assert body["logs"] == []


# ── Repository unit tests ───────────────────────────────────────────────────


def test_repository_filter_pool_drops_blacklisted():
    from infrastructure.sms_pool_repository import SmsPoolBlacklistRepository

    repo = SmsPoolBlacklistRepository()
    repo.add(phone="+15822063090", reason="oas_error")

    pool = [
        {"phone_e164": "+15822063090", "phone": "5822063090", "relay_url": "https://ex"},
        {"phone_e164": "+15822064144", "phone": "5822064144", "relay_url": "https://ex"},
    ]
    kept, skipped = repo.filter_pool(pool)
    assert [item["phone_e164"] for item in kept] == ["+15822064144"]
    assert [item["phone_e164"] for item in skipped] == ["+15822063090"]
    assert skipped[0]["skipped_reason"] == "blacklisted"


def test_repository_filter_pool_no_blacklist_pass_through():
    from infrastructure.sms_pool_repository import SmsPoolBlacklistRepository

    repo = SmsPoolBlacklistRepository()
    pool = [{"phone_e164": "+15822063090", "relay_url": "x"}]
    kept, skipped = repo.filter_pool(pool)
    assert kept == pool
    assert skipped == []


def test_repository_is_blacklisted_normalizes_input():
    from infrastructure.sms_pool_repository import SmsPoolBlacklistRepository

    repo = SmsPoolBlacklistRepository()
    repo.add(phone="+15822063090")
    # 没有 + 前缀 / 含空格 / 含括号也能命中
    assert repo.is_blacklisted("+15822063090")
    assert repo.is_blacklisted("15822063090") is False  # 严格区分 + 前缀
    assert repo.is_blacklisted("+1 (582) 206-3090")
