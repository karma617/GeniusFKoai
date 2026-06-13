from __future__ import annotations


def test_config_options_include_herosms_provider(client):
    resp = client.get("/api/config/options")
    assert resp.status_code == 200
    data = resp.json()
    providers = data["sms_providers"]
    hero = next(item for item in providers if item["value"] == "herosms_api")
    assert hero["label"] == "HeroSMS"
    assert any(field["key"] == "herosms_api_key" for field in hero["fields"])


def test_herosms_balance_endpoint_accepts_inline_api_key(client, monkeypatch):
    monkeypatch.setattr("core.base_sms.HeroSmsProvider.get_balance", lambda self: 12.345)

    resp = client.post("/api/sms/herosms/balance", json={"api_key": "hero123"})

    assert resp.status_code == 200
    assert resp.json() == {"balance": 12.345}


def test_herosms_balance_endpoint_requires_api_key(client):
    resp = client.post("/api/sms/herosms/balance", json={})

    assert resp.status_code == 400
    assert "HeroSMS API Key" in resp.json()["detail"]


def test_grizzlysms_catalog_endpoint_reads_saved_api_key(client, monkeypatch):
    monkeypatch.setattr(
        "api.sms._saved_sms_config",
        lambda provider_key: {"grizzlysms_api_key": "KEY"} if provider_key == "grizzlysms_api" else {},
    )
    monkeypatch.setattr(
        "api.sms.GrizzlySmsProvider.get_countries",
        lambda self: [{"id": "52", "chn": "Thailand"}],
    )

    resp = client.get("/api/sms/grizzlysms/countries")

    assert resp.status_code == 200
    assert resp.json() == {"countries": [{"id": "52", "chn": "Thailand"}]}


def test_nexsms_catalog_endpoint_returns_empty_without_saved_api_key(client, monkeypatch):
    monkeypatch.setattr("api.sms._saved_sms_config", lambda provider_key: {})

    resp = client.get("/api/sms/nexsms/countries")

    assert resp.status_code == 200
    assert resp.json() == {"countries": []}
