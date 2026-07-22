"""Dynamic proxy provider unit tests."""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
import core.proxy_providers as proxy_providers_module
from core.proxy_providers import (
    ApiExtractProvider,
    ClashProxyProvider,
    RotatingProxyProvider,
    create_proxy_provider,
    refresh_local_proxy_node,
)


class TestApiExtractProvider:
    def test_parse_plain_text(self):
        provider = ApiExtractProvider(api_url="http://fake")
        lines = "1.2.3.4:8080\n5.6.7.8:3128\n"
        mock_resp = MagicMock()
        mock_resp.text = lines
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None

        with patch("core.proxy_providers.requests.get", return_value=mock_resp):
            proxy = provider.get_proxy()
            assert proxy == "http://1.2.3.4:8080"
            proxy2 = provider.get_proxy()
            assert proxy2 == "http://5.6.7.8:3128"
            # Cache exhausted
            proxy3 = provider.get_proxy()
            # Will re-fetch
            assert proxy3 is not None

    def test_parse_json_array(self):
        provider = ApiExtractProvider(api_url="http://fake")
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(["10.0.0.1:1080", "10.0.0.2:1080"])
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None

        with patch("core.proxy_providers.requests.get", return_value=mock_resp):
            proxy = provider.get_proxy()
            assert "10.0.0.1:1080" in proxy

    def test_parse_json_object_with_data_key(self):
        provider = ApiExtractProvider(api_url="http://fake")
        mock_resp = MagicMock()
        mock_resp.text = json.dumps({"data": ["10.0.0.1:8080"]})
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None

        with patch("core.proxy_providers.requests.get", return_value=mock_resp):
            proxy = provider.get_proxy()
            assert "10.0.0.1:8080" in proxy

    def test_with_auth(self):
        provider = ApiExtractProvider(
            api_url="http://fake",
            username="user",
            password="pass",
        )
        mock_resp = MagicMock()
        mock_resp.text = "1.2.3.4:8080"
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None

        with patch("core.proxy_providers.requests.get", return_value=mock_resp):
            proxy = provider.get_proxy()
            assert proxy == "http://user:pass@1.2.3.4:8080"

    def test_with_socks5_protocol(self):
        provider = ApiExtractProvider(
            api_url="http://fake",
            protocol="socks5",
        )
        mock_resp = MagicMock()
        mock_resp.text = "1.2.3.4:1080"
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None

        with patch("core.proxy_providers.requests.get", return_value=mock_resp):
            proxy = provider.get_proxy()
            assert proxy == "socks5://1.2.3.4:1080"

    def test_already_has_protocol(self):
        provider = ApiExtractProvider(api_url="http://fake")
        mock_resp = MagicMock()
        mock_resp.text = "socks5://1.2.3.4:1080"
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None

        with patch("core.proxy_providers.requests.get", return_value=mock_resp):
            proxy = provider.get_proxy()
            assert proxy == "socks5://1.2.3.4:1080"

    def test_api_failure_returns_none(self):
        provider = ApiExtractProvider(api_url="http://fake")
        with patch("core.proxy_providers.requests.get", side_effect=Exception("timeout")):
            proxy = provider.get_proxy()
            assert proxy is None


class TestRotatingProxyProvider:
    def test_returns_gateway(self):
        provider = RotatingProxyProvider(gateway_url="http://user:pass@gate.example.com:8080")
        assert provider.get_proxy() == "http://user:pass@gate.example.com:8080"
        # Always returns the same gateway
        assert provider.get_proxy() == "http://user:pass@gate.example.com:8080"

    def test_empty_gateway(self):
        provider = RotatingProxyProvider(gateway_url="")
        assert provider.get_proxy() is None


class TestClashProxyProvider:
    def test_switches_next_leaf_node_and_returns_local_proxy(self):
        provider = ClashProxyProvider(
            api_url="http://clash.test",
            secret="secret",
            proxy_url="http://127.0.0.1:7897",
            selector="GLOBAL",
        )
        payload = {
            "proxies": {
                "GLOBAL": {"type": "Selector", "now": "node-old", "all": ["DIRECT", "node-a", "node-b"]},
                "node-a": {"type": "Shadowsocks"},
                "node-b": {"type": "Vmess"},
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_put = MagicMock()
        mock_put.raise_for_status = lambda: None

        with patch("providers.proxy.clash.requests.get", return_value=mock_resp), patch(
            "providers.proxy.clash.requests.put",
            return_value=mock_put,
        ) as put:
            proxy = provider.get_proxy()

        assert proxy == "http://127.0.0.1:7897"
        assert provider.last_node == "node-a"
        put.assert_called_once()
        assert put.call_args.kwargs["json"] == {"name": "node-a"}
        assert put.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}

    def test_refresh_local_node_switches_to_different_node(self):
        provider = ClashProxyProvider(
            api_url="http://clash-refresh.test",
            secret="secret",
            proxy_url="http://127.0.0.1:7897",
            selector="GLOBAL",
            check_url="",
        )
        payload = {
            "proxies": {
                "GLOBAL": {"type": "Selector", "now": "node-a", "all": ["node-a", "node-b"]},
                "node-a": {"type": "Shadowsocks"},
                "node-b": {"type": "Vmess"},
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_put = MagicMock()
        mock_put.raise_for_status = lambda: None

        with patch("providers.proxy.clash.requests.get", return_value=mock_resp), patch(
            "providers.proxy.clash.requests.put",
            return_value=mock_put,
        ) as put:
            result = provider.refresh_local_node(reason="network error")

        assert result["ok"] is True
        assert result["previous_node"] == "node-a"
        assert result["selected_node"] == "node-b"
        assert result["proxy"] == "http://127.0.0.1:7897"
        put.assert_called_once()
        assert put.call_args.kwargs["json"] == {"name": "node-b"}

    def test_refresh_local_node_rolls_back_when_exit_check_fails(self):
        provider = ClashProxyProvider(
            api_url="http://clash-refresh.test",
            secret="secret",
            proxy_url="http://127.0.0.1:7897",
            selector="GLOBAL",
            check_url="https://exit-check.test",
        )
        switches: list[tuple[str, str]] = []
        provider.list_nodes = lambda: ("GLOBAL", ["node-a", "node-b"])
        provider.current_node = lambda selector=None: "node-a"
        provider._choose_node_after = lambda nodes, current: "node-b"
        provider.switch_node = lambda selector, node: switches.append((selector, node))
        provider._check_proxy_exit = MagicMock(side_effect=RuntimeError("exit check failed"))

        with pytest.raises(RuntimeError, match="exit check failed"):
            provider.refresh_local_node(reason="network error")

        assert switches == [("GLOBAL", "node-b"), ("GLOBAL", "node-a")]

    def test_excludes_nodes_by_name_filter(self):
        provider = ClashProxyProvider(
            api_url="http://clash-filter.test",
            proxy_url="127.0.0.1:7897",
            selector="GLOBAL",
            node_filter="JP",
        )
        payload = {
            "proxies": {
                "GLOBAL": {"type": "Selector", "now": "", "all": ["US-1", "JP-1"]},
                "US-1": {"type": "Trojan"},
                "JP-1": {"type": "Trojan"},
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        mock_put = MagicMock()
        mock_put.raise_for_status = lambda: None

        with patch("providers.proxy.clash.requests.get", return_value=mock_resp), patch(
            "providers.proxy.clash.requests.put",
            return_value=mock_put,
        ):
            proxy = provider.get_proxy()

        assert proxy == "http://127.0.0.1:7897"
        assert provider.last_node == "US-1"
    def test_multi_port_preparation_shuffles_nodes(self, tmp_path, monkeypatch):
        provider = ClashProxyProvider(
            api_url="http://clash-multi.test",
            proxy_url="127.0.0.1:7897",
            selector="GLOBAL",
            allocation_mode="multi_port",
            multi_port_start=7891,
            multi_port_controller_start=9191,
            multi_port_runtime_dir=str(tmp_path),
        )
        selected_nodes = []

        class FakeProcess:
            def poll(self):
                return None

            def terminate(self):
                return None

        monkeypatch.setattr(provider, "list_nodes", lambda: ("GLOBAL", ["US-1", "TH-1", "JP-1"]))
        monkeypatch.setattr("providers.proxy.clash.random.shuffle", lambda values: values.reverse())
        monkeypatch.setattr(provider, "_resolve_source_config", lambda: tmp_path / "source.yaml")
        monkeypatch.setattr(provider, "_resolve_runtime_dir", lambda: tmp_path)
        monkeypatch.setattr(provider, "_resolve_core_path", lambda: tmp_path / "mihomo.exe")
        monkeypatch.setattr(provider, "_is_port_open", lambda _port: False)
        monkeypatch.setattr(provider, "_wait_port", lambda _port: None)
        monkeypatch.setattr(provider, "_wait_port_closed", lambda _port: None)
        monkeypatch.setattr(provider, "_check_proxy_exit", lambda _proxy: "{}")
        monkeypatch.setattr(provider, "_write_multi_port_config", lambda _runtime, index, _config: tmp_path / f"{index}.yaml")

        def fake_build_config(_source, node, _port, _controller_port):
            selected_nodes.append(node)
            return {"node": node}

        monkeypatch.setattr(provider, "_build_multi_port_config", fake_build_config)
        monkeypatch.setattr(provider, "_start_multi_port_instance", lambda *_args: FakeProcess())

        proxies = provider.prepare_for_concurrency(2, refresh=True)

        assert proxies == ["http://127.0.0.1:7891", "http://127.0.0.1:7892"]
        assert selected_nodes == ["JP-1", "TH-1"]


class TestCreateProxyProvider:
    def test_api_extract(self):
        provider = create_proxy_provider("api_extract", {"proxy_api_url": "http://api.test/get"})
        assert isinstance(provider, ApiExtractProvider)

    def test_rotating_gateway(self):
        provider = create_proxy_provider("rotating_gateway", {"proxy_gateway_url": "http://gate:8080"})
        assert isinstance(provider, RotatingProxyProvider)

    def test_clash(self):
        provider = create_proxy_provider("clash", {"clash_api_url": "http://127.0.0.1:9097"})
        assert isinstance(provider, ClashProxyProvider)

    def test_api_extract_missing_url(self):
        with pytest.raises(RuntimeError, match="未配置"):
            create_proxy_provider("api_extract", {})

    def test_rotating_missing_gateway(self):
        with pytest.raises(RuntimeError, match="未配置"):
            create_proxy_provider("rotating_gateway", {})

    def test_clash_missing_api_url(self):
        with pytest.raises(RuntimeError, match="未配置"):
            create_proxy_provider("clash", {})

    def test_unknown_provider(self):
        with pytest.raises(RuntimeError, match="未知"):
            create_proxy_provider("unknown", {})


def test_refresh_local_proxy_node_uses_configured_clash_for_matching_upstream(monkeypatch):
    setting = MagicMock()
    setting.id = 10
    setting.provider_key = "clash"
    setting.get_config.return_value = {"clash_proxy_url": "http://127.0.0.1:7897"}
    setting.get_auth.return_value = {}
    provider = MagicMock()
    provider.refresh_local_node.return_value = {"ok": True, "selected_node": "node-b"}
    repo = MagicMock()
    repo.list_enabled.return_value = []
    repo.get_by_key.return_value = setting

    monkeypatch.setattr(
        "infrastructure.provider_settings_repository.ProviderSettingsRepository",
        lambda: repo,
    )
    monkeypatch.setattr(proxy_providers_module, "create_proxy_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(proxy_providers_module, "_LAST_LOCAL_NODE_REFRESH_AT", 0.0)
    monkeypatch.setattr(proxy_providers_module, "_LAST_LOCAL_NODE_REFRESH_RESULT", None)

    result = refresh_local_proxy_node(
        proxy_url="socks5h://127.0.0.1:7897",
        reason="network error",
        min_interval_seconds=0,
    )

    assert result["ok"] is True
    provider.refresh_local_node.assert_called_once_with(
        proxy_url="socks5h://127.0.0.1:7897",
        reason="network error",
    )
