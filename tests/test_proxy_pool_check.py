from __future__ import annotations

from sqlmodel import Session, select

import core.config_store  # noqa: F401  ensure ConfigItem is registered before test DB create_all
from core.db import ProxyModel, engine
from core.proxy_pool import PROXY_CHECK_URL, normalize_proxy_url, proxy_pool


class _Response:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def test_proxy_pool_check_all_uses_registration_http_client_and_checks_every_proxy(monkeypatch):
    with Session(engine) as session:
        session.add(ProxyModel(url="http://good.proxy:3128", region="BR"))
        session.add(ProxyModel(url="http://bad.proxy:3128", region="JP"))
        session.commit()

    calls = []
    closed = []

    class FakeOpenAIHTTPClient:
        def __init__(self, proxy_url=None, config=None):
            self.proxy_url = proxy_url
            self.config = config

        def get(self, url, *, timeout=None):
            calls.append((url, self.proxy_url, self.config, timeout))
            if self.proxy_url == "http://good.proxy:3128":
                return _Response(200, "ip=1.2.3.4\nloc=US\n")
            return _Response(403, "blocked")

        def close(self):
            closed.append(self.proxy_url)

    monkeypatch.setattr("platforms.chatgpt.http_client.OpenAIHTTPClient", FakeOpenAIHTTPClient)

    result = proxy_pool.check_all(concurrency=2, timeout=5)

    assert result["total"] == 2
    assert result["ok"] == 1
    assert result["fail"] == 1
    assert len(calls) == 2
    assert {call[0] for call in calls} == {PROXY_CHECK_URL}
    assert {call[1] for call in calls} == {
        "http://good.proxy:3128",
        "http://bad.proxy:3128",
    }
    assert {call[2].timeout for call in calls} == {5}
    assert {call[2].max_retries for call in calls} == {1}
    assert {call[2].impersonate for call in calls} == {"chrome136"}
    assert {call[3] for call in calls} == {5}
    assert set(closed) == {"http://good.proxy:3128", "http://bad.proxy:3128"}

    with Session(engine) as session:
        rows = {
            item.url: item
            for item in session.exec(select(ProxyModel)).all()
        }
    assert rows["http://good.proxy:3128"].success_count == 1
    assert rows["http://good.proxy:3128"].fail_count == 0
    assert rows["http://good.proxy:3128"].region == "US"
    assert rows["http://bad.proxy:3128"].success_count == 0
    assert rows["http://bad.proxy:3128"].fail_count == 1


def test_proxy_pool_check_one_uses_same_registration_http_client(monkeypatch):
    with Session(engine) as session:
        session.add(ProxyModel(url="http://one.proxy:3128", region="JP"))
        session.commit()

    calls = []

    class FakeOpenAIHTTPClient:
        def __init__(self, proxy_url=None, config=None):
            self.proxy_url = proxy_url
            self.config = config

        def get(self, url, *, timeout=None):
            calls.append((url, self.proxy_url, self.config, timeout))
            return _Response(200, "ip=1.2.3.4\nloc=US\n")

        def close(self):
            pass

    monkeypatch.setattr("platforms.chatgpt.http_client.OpenAIHTTPClient", FakeOpenAIHTTPClient)

    result = proxy_pool.check_one("http://one.proxy:3128", timeout=5)

    assert result["total"] == 1
    assert result["ok"] == 1
    assert result["fail"] == 0
    assert result["result"]["url"] == "http://one.proxy:3128"
    assert calls[0][0] == PROXY_CHECK_URL
    assert calls[0][1] == "http://one.proxy:3128"
    assert calls[0][2].timeout == 5
    assert calls[0][2].max_retries == 1
    assert calls[0][2].impersonate == "chrome136"
    assert calls[0][3] == 5

    with Session(engine) as session:
        row = session.exec(select(ProxyModel).where(ProxyModel.url == "http://one.proxy:3128")).first()
    assert row.success_count == 1
    assert row.fail_count == 0
    assert row.region == "US"


def test_proxy_pool_lease_next_keeps_concurrent_leases_unique():
    with Session(engine) as session:
        session.add(ProxyModel(url="http://one.proxy:3128", region="JP"))
        session.add(ProxyModel(url="http://two.proxy:3128", region="JP"))
        session.commit()

    proxy_pool.clear_leases()
    first = proxy_pool.lease_next(region="JP")
    second = proxy_pool.lease_next(region="JP")
    third = proxy_pool.lease_next(region="JP")

    assert {first, second} == {"http://one.proxy:3128", "http://two.proxy:3128"}
    assert third is None

    proxy_pool.release_lease(first)
    reused = proxy_pool.lease_next(region="JP")
    assert reused == first
    proxy_pool.clear_leases()


def test_normalize_proxy_url_matches_registration_runtime_formats():
    assert normalize_proxy_url("us.1024proxy.io:3000:user:pass") == (
        "http://user:pass@us.1024proxy.io:3000"
    )
    assert normalize_proxy_url("https://us.1024proxy.io:3000:user:pass") == (
        "https://user:pass@us.1024proxy.io:3000"
    )
    assert normalize_proxy_url("http://user:pass@us.1024proxy.io:3000") == (
        "http://user:pass@us.1024proxy.io:3000"
    )
    assert normalize_proxy_url("127.0.0.1:7897") == "http://127.0.0.1:7897"


def test_normalize_proxy_url_uses_selected_default_scheme():
    assert normalize_proxy_url("127.0.0.1:7897", default_scheme="https") == (
        "https://127.0.0.1:7897"
    )
    assert normalize_proxy_url("127.0.0.1:7897", default_scheme="socks5") == (
        "socks5://127.0.0.1:7897"
    )
    assert normalize_proxy_url(
        "us.1024proxy.io:3000:user:pass",
        default_scheme="socks5",
    ) == "socks5://user:pass@us.1024proxy.io:3000"
    assert normalize_proxy_url("https://127.0.0.1:7897", default_scheme="socks5") == (
        "https://127.0.0.1:7897"
    )
