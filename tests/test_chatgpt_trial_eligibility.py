from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from application import tasks


class _Logger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, object]] = []

    def log(self, message: str, level: str = "info", detail=None) -> None:
        self.entries.append((message, level, detail))


def test_trial_probe_after_register_requires_explicit_opt_in(monkeypatch) -> None:
    logger = _Logger()
    account = SimpleNamespace(platform="chatgpt", email="trial@example.com", extra={})

    assert tasks._chatgpt_trial_probe_after_register_enabled({}) is False
    assert tasks._chatgpt_trial_probe_after_register_enabled(
        {"trial_eligibility_proxies": {"JP": "http://jp-proxy:8000"}}
    ) is False
    assert tasks._chatgpt_trial_probe_after_register_enabled(
        {"trial_probe_after_register": True}
    ) is True

    monkeypatch.setattr(
        tasks._CHATGPT_TRIAL_CHECK_EXECUTOR,
        "submit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled trial probe must not submit background work")
        ),
    )
    tasks._schedule_chatgpt_trial_post_register_check(
        account=account,
        saved_account_id=42,
        logger=logger,
        proxy="http://registration-proxy:8000",
        region_proxies={"JP": "http://jp-proxy:8000"},
    )


def test_validate_trial_region_proxies_checks_expected_exit(monkeypatch) -> None:
    logger = _Logger()

    def _check_one(proxy: str, timeout: int = 0):
        assert timeout == 15
        region = "JP" if "jp-proxy" in proxy else "PH"
        return {"result": {"ok": True, "region": region}}

    from core import proxy_pool as proxy_pool_module

    monkeypatch.setattr(proxy_pool_module.proxy_pool, "check_one", _check_one)

    validated = tasks._validate_chatgpt_trial_region_proxies(
        {
            "JP": "http://user:pass@jp-proxy:8000",
            "PH": "http://user:pass@ph-proxy:8000",
            "US": "http://ignored:8000",
        },
        logger,
        required=True,
    )

    assert list(validated) == ["JP", "PH"]
    assert validated["JP"] == "http://user:pass@jp-proxy:8000"
    assert validated["PH"] == "http://user:pass@ph-proxy:8000"
    assert any("日本代理出口验证通过" in message for message, _level, _detail in logger.entries)
    assert any("菲律宾代理出口验证通过" in message for message, _level, _detail in logger.entries)


def test_validate_trial_region_proxies_rejects_region_mismatch(monkeypatch) -> None:
    logger = _Logger()

    from core import proxy_pool as proxy_pool_module

    monkeypatch.setattr(
        proxy_pool_module.proxy_pool,
        "check_one",
        lambda _proxy, timeout=0: {"result": {"ok": True, "region": "US"}},
    )

    with pytest.raises(ValueError, match="日本代理实际出口"):
        tasks._validate_chatgpt_trial_region_proxies(
            {"JP": "http://jp-proxy:8000"},
            logger,
            required=True,
        )


def test_trial_probe_uses_system_proxy_when_all_regions_are_empty(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "_get_chatgpt_trial_system_proxy", lambda: "http://127.0.0.1:7897")

    assert tasks._build_chatgpt_trial_probe_targets({}) == [
        ("SYSTEM", "本机系统代理", "http://127.0.0.1:7897"),
    ]
    assert tasks._chatgpt_trial_probe_marking("SYSTEM", "本机系统代理") == ("试用", "", "")


def test_trial_probe_includes_vietnam_region() -> None:
    assert tasks.TRIAL_ELIGIBILITY_REGION_LABELS["VN"] == "越南"
    assert tasks._normalize_chatgpt_trial_region_proxies({
        "VN": "socks5://vn-proxy:1080",
    }) == {
        "VN": "socks5://vn-proxy:1080",
    }


def test_create_trial_probe_allows_empty_region_proxies(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "create_task", lambda **kwargs: kwargs)

    result = tasks.create_trial_eligibility_probe_task({
        "ids": [42],
        "proxies": {},
    })

    assert result["payload"]["proxies"] == {}


def test_post_register_trial_check_runs_configured_regions_in_parallel(monkeypatch) -> None:
    logger = _Logger()
    account = SimpleNamespace(platform="chatgpt", email="trial@example.com", extra={})
    barrier = threading.Barrier(2)
    calls: list[tuple[str, str]] = []
    marks: list[tuple[int, str, str, str]] = []

    def _inspect(_account, *, proxy=None, region_code="", **_kwargs):
        calls.append((str(region_code), str(proxy)))
        barrier.wait(timeout=2)
        return {
            "ok": True,
            "eligible": True,
            "campaign_id": "plus-1-month-free",
        }

    def _mark(saved_account_id: int, _trial_info, *, trial_label: str, region_code: str, region_label: str) -> None:
        marks.append((saved_account_id, trial_label, region_code, region_label))

    monkeypatch.setattr(tasks, "_inspect_chatgpt_free_plus_trial", _inspect)
    monkeypatch.setattr(tasks, "_mark_chatgpt_trial_account", _mark)

    eligible = tasks._run_chatgpt_trial_post_register_check(
        account=account,
        saved_account_id=42,
        logger=logger,
        proxy="http://registration-proxy:8000",
        region_proxies={
            "JP": "http://jp-proxy:8000",
            "PH": "http://ph-proxy:8000",
        },
    )

    assert eligible is True
    assert set(calls) == {
        ("JP", "http://jp-proxy:8000"),
        ("PH", "http://ph-proxy:8000"),
    }
    assert marks == [
        (42, "日本试用", "JP", "日本"),
        (42, "菲律宾试用", "PH", "菲律宾"),
    ]
    assert any("开始并行检测 2 个已配置地区" in message for message, _level, _detail in logger.entries)


def test_post_register_trial_check_uses_system_proxy_when_regions_are_empty(monkeypatch) -> None:
    logger = _Logger()
    account = SimpleNamespace(platform="chatgpt", email="trial@example.com", extra={})
    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(tasks, "_get_chatgpt_trial_system_proxy", lambda: "http://127.0.0.1:7897")

    def _inspect(_account, *, proxy=None, region_code="", **_kwargs):
        calls.append((str(region_code), proxy))
        return {"ok": True, "eligible": False}

    monkeypatch.setattr(tasks, "_inspect_chatgpt_free_plus_trial", _inspect)

    eligible = tasks._run_chatgpt_trial_post_register_check(
        account=account,
        saved_account_id=42,
        logger=logger,
        proxy="http://registration-proxy:8000",
        region_proxies={},
    )

    assert eligible is False
    assert calls == [("", "http://127.0.0.1:7897")]
    assert any("改用本机系统代理" in message for message, _level, _detail in logger.entries)


def test_trial_check_client_uses_registration_proxy_chain(monkeypatch) -> None:
    from platforms.chatgpt import http_client as chatgpt_http_client

    monkeypatch.setattr(
        chatgpt_http_client,
        "get_proxy_runtime_config",
        lambda: {"upstream_url": "http://127.0.0.1:7897"},
    )

    client = tasks._create_chatgpt_trial_check_client(
        "socks5://vn-proxy:1080",
        20,
    )
    try:
        assert client.config.impersonate == "firefox135"
        assert client.config.proxy_upstream_url == "http://127.0.0.1:7897"
        assert client.proxies == {
            "http": "socks5h://vn-proxy:1080",
            "https": "socks5h://vn-proxy:1080",
        }
    finally:
        client.close()
