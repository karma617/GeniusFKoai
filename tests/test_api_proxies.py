"""Proxy management endpoint tests."""
from __future__ import annotations


def test_list_proxies_empty(client):
    resp = client.get("/api/proxies")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 0


def test_add_proxy(client):
    resp = client.post("/api/proxies", json={"url": "http://127.0.0.1:7890", "region": "US"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "http://127.0.0.1:7890"
    assert data["region"] == "US"


def test_add_proxy_defaults_missing_scheme_to_http(client):
    resp = client.post("/api/proxies", json={"url": "127.0.0.1:7890", "region": "US"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "http://127.0.0.1:7890"
    assert data["region"] == "US"


def test_add_proxy_uses_selected_import_scheme_for_missing_scheme(client):
    resp = client.post("/api/proxies", json={
        "url": "127.0.0.1:7890",
        "region": "US",
        "import_scheme": "https",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "https://127.0.0.1:7890"
    assert data["region"] == "US"


def test_add_and_list_proxy(client):
    client.post("/api/proxies", json={"url": "http://127.0.0.1:7890"})
    resp = client.get("/api/proxies")
    data = resp.json()
    assert len(data) == 1


def test_delete_proxy(client):
    create_resp = client.post("/api/proxies", json={"url": "http://127.0.0.1:7890"})
    proxy_id = create_resp.json()["id"]
    del_resp = client.delete(f"/api/proxies/{proxy_id}")
    assert del_resp.status_code == 200
    # Verify deleted
    list_resp = client.get("/api/proxies")
    assert len(list_resp.json()) == 0


def test_delete_all_proxies(client):
    client.post("/api/proxies/bulk", json={"proxies": ["http://1.1.1.1:8080", "http://2.2.2.2:8080"]})

    resp = client.delete("/api/proxies")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": 2}
    assert client.get("/api/proxies").json() == []


def test_bulk_add_proxies(client):
    resp = client.post("/api/proxies/bulk", json={
        "proxies": ["http://1.1.1.1:8080", "http://2.2.2.2:8080"],
        "region": "SG",
    })
    assert resp.status_code == 200
    list_resp = client.get("/api/proxies")
    assert len(list_resp.json()) == 2


def test_bulk_add_proxies_normalizes_missing_scheme_and_deduplicates(client):
    resp = client.post("/api/proxies/bulk", json={
        "proxies": ["1.1.1.1:8080", "http://1.1.1.1:8080", "socks5://2.2.2.2:1080"],
        "region": "BR",
    })
    assert resp.status_code == 200
    assert resp.json()["added"] == 2
    list_resp = client.get("/api/proxies")
    data = sorted(list_resp.json(), key=lambda item: item["url"])
    assert [item["url"] for item in data] == ["http://1.1.1.1:8080", "socks5://2.2.2.2:1080"]
    assert {item["region"] for item in data} == {"BR"}


def test_bulk_add_proxies_uses_selected_scheme_without_overriding_existing_scheme(client):
    resp = client.post("/api/proxies/bulk", json={
        "proxies": ["1.1.1.1:8080", "http://2.2.2.2:8080"],
        "region": "BR",
        "import_scheme": "socks5",
    })
    assert resp.status_code == 200
    assert resp.json()["added"] == 2
    list_resp = client.get("/api/proxies")
    data = sorted(list_resp.json(), key=lambda item: item["url"])
    assert [item["url"] for item in data] == ["http://2.2.2.2:8080", "socks5://1.1.1.1:8080"]


def test_import_free_proxy_candidates(client):
    resp = client.post("/api/proxies/free/import-valid", json={
        "proxies": ["http://3.3.3.3:8080"],
        "region": "FREE",
    })
    assert resp.status_code == 200
    assert resp.json()["added"] == 1
    list_resp = client.get("/api/proxies")
    data = list_resp.json()
    assert data[0]["url"] == "http://3.3.3.3:8080"
    assert data[0]["region"] == "FREE"
