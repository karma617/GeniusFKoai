"""Clash external-controller proxy provider."""
from __future__ import annotations

import logging
import os
import random
import socket
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
import yaml

from core.proxy_providers import BaseProxyProvider
from providers.registry import register_provider

logger = logging.getLogger(__name__)

_POLICY_GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
_DIRECT_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}


@dataclass
class _ClashRotationState:
    lock: threading.RLock
    index: int = 0
    multi_port_proxies: list[str] | None = None
    multi_port_nodes: list[str] | None = None
    multi_port_processes: list[subprocess.Popen] | None = None


_STATE_LOCK = threading.Lock()
_ROTATION_STATES: dict[str, _ClashRotationState] = {}


def _state_for(key: str) -> _ClashRotationState:
    with _STATE_LOCK:
        state = _ROTATION_STATES.get(key)
        if state is None:
            state = _ClashRotationState(lock=threading.RLock())
            _ROTATION_STATES[key] = state
        return state


@register_provider("proxy", "clash")
class ClashProxyProvider(BaseProxyProvider):
    """Use a local Clash controller to rotate the selected node.

    The provider returns the same local proxy entry after switching the Clash
    selector. A single Clash inbound port is shared process-wide, so this
    rotates the global selector before each allocation rather than creating
    per-thread isolated proxy endpoints.
    """

    def __init__(
        self,
        *,
        api_url: str,
        secret: str = "",
        proxy_url: str = "http://127.0.0.1:7897",
        selector: str = "GLOBAL",
        node_filter: str = "",
        check_url: str = "https://api.ipify.org?format=json",
        allocation_mode: str = "rotate_selector",
        multi_port_start: int = 7891,
        multi_port_controller_start: int = 9191,
        multi_port_core_path: str = "",
        multi_port_source_config: str = "",
        multi_port_runtime_dir: str = "",
        timeout: int = 8,
    ):
        self.api_url = self._normalize_api_url(api_url)
        self.secret = secret or ""
        self.proxy_url = self._normalize_proxy_url(proxy_url)
        self.selector = (selector or "GLOBAL").strip()
        self.node_filter = node_filter or ""
        self.check_url = check_url or ""
        self.allocation_mode = (allocation_mode or "rotate_selector").strip()
        self.multi_port_start = int(multi_port_start or 7891)
        self.multi_port_controller_start = int(multi_port_controller_start or 9191)
        self.multi_port_core_path = str(multi_port_core_path or "").strip()
        self.multi_port_source_config = str(multi_port_source_config or "").strip()
        self.multi_port_runtime_dir = str(multi_port_runtime_dir or "").strip()
        self.timeout = timeout
        self._state = _state_for(
            f"{self.api_url}|{self.selector}|{self.node_filter}|"
            f"{self.allocation_mode}|{self.multi_port_start}"
        )
        self.last_node = ""

    @classmethod
    def from_config(cls, config: dict) -> "ClashProxyProvider":
        api_url = config.get("clash_api_url", "")
        if not api_url:
            raise RuntimeError("Clash 未配置外部控制接口地址")
        return cls(
            api_url=api_url,
            secret=config.get("clash_secret", ""),
            proxy_url=config.get("clash_proxy_url", "http://127.0.0.1:7897"),
            selector=config.get("clash_selector", "GLOBAL"),
            node_filter=config.get("clash_node_filter", ""),
            check_url=config.get("clash_check_url", "https://api.ipify.org?format=json"),
            allocation_mode=config.get("clash_allocation_mode", "rotate_selector"),
            multi_port_start=int(config.get("clash_multi_port_start") or 7891),
            multi_port_controller_start=int(config.get("clash_multi_port_controller_start") or 9191),
            multi_port_core_path=config.get("clash_multi_port_core_path", ""),
            multi_port_source_config=config.get("clash_multi_port_source_config", ""),
            multi_port_runtime_dir=config.get("clash_multi_port_runtime_dir", ""),
        )

    @staticmethod
    def _normalize_api_url(value: str) -> str:
        url = str(value or "").strip().rstrip("/")
        if not url:
            return ""
        if "://" not in url:
            url = f"http://{url}"
        return url

    @staticmethod
    def _normalize_proxy_url(value: str) -> str:
        proxy = str(value or "").strip()
        if proxy and "://" not in proxy:
            proxy = f"http://{proxy}"
        return proxy

    def _headers(self) -> dict[str, str]:
        if not self.secret:
            return {}
        return {"Authorization": f"Bearer {self.secret}"}

    def _controller_url(self, path: str) -> str:
        return f"{self.api_url}{path}"

    def _get_proxies(self) -> dict:
        resp = requests.get(
            self._controller_url("/proxies"),
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        proxies = payload.get("proxies") if isinstance(payload, dict) else None
        if not isinstance(proxies, dict):
            raise RuntimeError("Clash /proxies 响应格式异常")
        return proxies

    def _resolve_selector(self, proxies: dict) -> str:
        if self.selector in proxies and isinstance(proxies[self.selector].get("all"), list):
            return self.selector
        for candidate in ("GLOBAL", "Proxy", "🚀 节点选择", "节点选择"):
            if candidate in proxies and isinstance(proxies[candidate].get("all"), list):
                return candidate
        for name, info in proxies.items():
            if isinstance(info, dict) and isinstance(info.get("all"), list):
                return str(name)
        raise RuntimeError("Clash 未找到可切换的策略组")

    def list_selectors(self) -> list[str]:
        proxies = self._get_proxies()
        return [
            str(name)
            for name, info in proxies.items()
            if isinstance(info, dict) and isinstance(info.get("all"), list)
        ]

    def _filter_nodes(self, names: list[str]) -> list[str]:
        filters = [
            item.strip().lower()
            for item in self.node_filter.replace("\n", ",").split(",")
            if item.strip()
        ]
        if not filters:
            return names
        return [name for name in names if not any(token in name.lower() for token in filters)]

    @staticmethod
    def _latest_delay(info: dict) -> int:
        history = info.get("history")
        if not isinstance(history, list):
            return 999999
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            try:
                delay = int(item.get("delay") or 0)
            except Exception:
                continue
            if 0 < delay < 5000:
                return delay
        return 999999

    def list_nodes(self) -> tuple[str, list[str]]:
        proxies = self._get_proxies()
        selector = self._resolve_selector(proxies)
        selector_info = proxies.get(selector) or {}
        candidates = [
            str(name)
            for name in selector_info.get("all", [])
            if str(name).strip() and str(name).upper() not in _DIRECT_NAMES and str(name) != selector
        ]
        leaf_nodes = []
        for name in candidates:
            info = proxies.get(name)
            if not isinstance(info, dict):
                continue
            if info.get("alive") is False:
                continue
            node_type = str(info.get("type") or "")
            if node_type and node_type in _POLICY_GROUP_TYPES:
                continue
            leaf_nodes.append(name)

        nodes = self._filter_nodes(leaf_nodes or candidates)
        nodes.sort(key=lambda item: self._latest_delay(proxies.get(item) or {}))
        if not nodes:
            raise RuntimeError("Clash 策略组内没有可用节点")
        return selector, nodes

    def _choose_node(self, nodes: list[str]) -> str:
        with self._state.lock:
            node = nodes[self._state.index % len(nodes)]
            self._state.index += 1
            return node

    def _choose_proxy(self, proxies: list[str]) -> str:
        with self._state.lock:
            proxy = proxies[self._state.index % len(proxies)]
            self._state.index += 1
            return proxy

    def current_node(self, selector: str | None = None) -> str:
        proxies = self._get_proxies()
        selector_name = selector or self._resolve_selector(proxies)
        info = proxies.get(selector_name) or {}
        return str(info.get("now") or "")

    def switch_node(self, selector: str, node: str) -> None:
        encoded_selector = urllib.parse.quote(selector, safe="")
        resp = requests.put(
            self._controller_url(f"/proxies/{encoded_selector}"),
            headers=self._headers(),
            json={"name": node},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        self.last_node = node
        logger.info("[ProxyProvider] Clash 已切换节点: %s -> %s", selector, node)

    def _resolve_clash_verge_dir(self) -> Path:
        appdata = os.environ.get("APPDATA") or ""
        candidates = [
            Path(appdata) / "io.github.clash-verge-rev.clash-verge-rev",
            Path(appdata) / "clash_win",
        ]
        for candidate in candidates:
            if (candidate / "clash-verge.yaml").exists():
                return candidate
        raise RuntimeError("未找到 Clash Verge 配置目录")

    def _resolve_source_config(self) -> Path:
        if self.multi_port_source_config:
            path = Path(self.multi_port_source_config)
            if path.exists():
                return path
            raise RuntimeError(f"Clash 源配置不存在: {path}")
        return self._resolve_clash_verge_dir() / "clash-verge.yaml"

    def _resolve_runtime_dir(self) -> Path:
        if self.multi_port_runtime_dir:
            path = Path(self.multi_port_runtime_dir)
        else:
            path = self._resolve_clash_verge_dir() / "codex-register"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _terminate_stale_multi_port_processes(self, runtime_dir: Path) -> None:
        if os.name != "nt":
            return
        runtime_text = str(runtime_dir).replace("'", "''")
        command = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { ($_.Name -match 'mihomo|clash-meta') -and "
            f"($_.CommandLine -like '*{runtime_text}*') }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except Exception:
            logger.debug("[ProxyProvider] 清理旧 Clash 多入口进程失败", exc_info=True)

    def _resolve_core_path(self) -> Path:
        if self.multi_port_core_path:
            path = Path(self.multi_port_core_path)
            if path.exists():
                return path
            raise RuntimeError(f"Mihomo 内核不存在: {path}")
        candidates = [
            Path(r"C:\Program Files\Clash Verge\verge-mihomo.exe"),
            Path(r"C:\Program Files\Clash Verge\clash-meta.exe"),
            Path(r"C:\Program Files\Clash Verge\verge-mihomo-alpha.exe"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError("未找到 Clash Verge Mihomo 内核")

    @staticmethod
    def _is_port_open(port: int, host: str = "127.0.0.1") -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return True
        except OSError:
            return False

    def _wait_port(self, port: int, timeout: float = 25.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._is_port_open(port):
                return
            time.sleep(0.2)
        raise RuntimeError(f"Clash 多入口端口未启动: 127.0.0.1:{port}")

    def _wait_port_closed(self, port: int, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._is_port_open(port):
                return
            time.sleep(0.2)

    def _build_multi_port_config(self, source_config: Path, node: str, port: int, controller_port: int) -> dict:
        with source_config.open("r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
        if not isinstance(config, dict):
            raise RuntimeError("Clash 源配置格式异常")

        config["mixed-port"] = int(port)
        config["allow-lan"] = False
        config["bind-address"] = "127.0.0.1"
        config["external-controller"] = f"127.0.0.1:{int(controller_port)}"
        config["secret"] = self.secret
        config["mode"] = "rule"
        config["rules"] = [f"MATCH,{node}"]
        config.pop("external-controller-pipe", None)
        config.pop("listeners", None)
        tun = config.get("tun")
        if isinstance(tun, dict):
            tun["enable"] = False
        return config

    def _write_multi_port_config(self, runtime_dir: Path, index: int, config: dict) -> Path:
        instance_dir = runtime_dir / f"instance-{index + 1}"
        instance_dir.mkdir(parents=True, exist_ok=True)
        config_path = instance_dir / "config.yaml"
        with config_path.open("w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)
        return config_path

    def _start_multi_port_instance(self, core_path: Path, runtime_dir: Path, index: int, config_path: Path) -> subprocess.Popen:
        instance_dir = runtime_dir / f"instance-{index + 1}"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(
            [str(core_path), "-d", str(instance_dir), "-f", str(config_path)],
            cwd=str(instance_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def _check_proxy_exit(self, proxy: str) -> str:
        if not self.check_url:
            return ""
        resp = requests.get(
            self.check_url,
            proxies={"http": proxy, "https": proxy},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.text[:300]

    def _multi_port_index_from_proxy(self, proxy: str) -> int:
        parsed = urllib.parse.urlsplit(str(proxy or "").strip())
        port = int(parsed.port or 0)
        index = port - self.multi_port_start
        if index < 0:
            raise RuntimeError(f"代理不属于 Clash 多入口端口范围: {proxy}")
        return index

    def prepare_for_concurrency(self, concurrency: int, *, refresh: bool = False) -> list[str]:
        count = max(int(concurrency or 1), 1)
        if self.allocation_mode != "multi_port":
            return []

        with self._state.lock:
            current = self._state.multi_port_proxies or []
            if not refresh and len(current) >= count and all(
                self._is_port_open(self.multi_port_start + idx)
                for idx in range(count)
            ):
                return current[:count]

            selector, nodes = self.list_nodes()
            if len(nodes) < count:
                raise RuntimeError(
                    f"Clash 可用节点不足：并发数 {count}，策略组 {selector} 仅 {len(nodes)} 个节点"
                )
            source_config = self._resolve_source_config()
            runtime_dir = self._resolve_runtime_dir()
            core_path = self._resolve_core_path()

            old_processes = self._state.multi_port_processes or []
            for process in old_processes:
                if process.poll() is None:
                    process.terminate()
            self._state.multi_port_processes = []
            self._terminate_stale_multi_port_processes(runtime_dir)

            proxies: list[str] = []
            processes: list[subprocess.Popen] = []
            try:
                selected_nodes: list[str] = []
                node_pool = list(nodes)
                random.shuffle(node_pool)
                node_iter = iter(node_pool)
                for idx in range(count):
                    port = self.multi_port_start + idx
                    controller_port = self.multi_port_controller_start + idx
                    if self._is_port_open(port):
                        raise RuntimeError(f"Clash 多入口端口已被占用: 127.0.0.1:{port}")
                    while True:
                        try:
                            node = next(node_iter)
                        except StopIteration as exc:
                            raise RuntimeError(
                                f"Clash 可通过出口检测的节点不足：并发数 {count}，已准备 {len(proxies)} 个"
                            ) from exc
                        config = self._build_multi_port_config(source_config, node, port, controller_port)
                        config_path = self._write_multi_port_config(runtime_dir, idx, config)
                        process = self._start_multi_port_instance(core_path, runtime_dir, idx, config_path)
                        try:
                            self._wait_port(port)
                            proxy = f"http://127.0.0.1:{port}"
                            self._check_proxy_exit(proxy)
                        except Exception:
                            if process.poll() is None:
                                process.terminate()
                            self._wait_port_closed(port)
                            continue
                        processes.append(process)
                        proxies.append(proxy)
                        selected_nodes.append(node)
                        break
            except Exception:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                raise

            self._state.multi_port_proxies = proxies
            self._state.multi_port_nodes = selected_nodes
            self._state.multi_port_processes = processes
            logger.info("[ProxyProvider] Clash 多入口已准备: %s", ", ".join(proxies))
            return proxies

    def refresh_prepared_proxy(self, proxy: str) -> str:
        if self.allocation_mode != "multi_port":
            return str(proxy or "").strip()

        with self._state.lock:
            index = self._multi_port_index_from_proxy(proxy)
            proxies = self._state.multi_port_proxies or []
            if index >= len(proxies):
                return str(proxy or "").strip()

            port = self.multi_port_start + index
            controller_port = self.multi_port_controller_start + index
            selector, nodes = self.list_nodes()
            current_nodes = list(self._state.multi_port_nodes or [])
            excluded = {node for idx, node in enumerate(current_nodes) if idx != index}
            node_pool = [node for node in nodes if node not in excluded] or list(nodes)
            random.shuffle(node_pool)

            source_config = self._resolve_source_config()
            runtime_dir = self._resolve_runtime_dir()
            core_path = self._resolve_core_path()
            old_processes = self._state.multi_port_processes or []
            old_process = old_processes[index] if index < len(old_processes) else None
            if old_process is not None and old_process.poll() is None:
                old_process.terminate()
                self._wait_port_closed(port)

            last_error = ""
            for node in node_pool:
                process = None
                try:
                    config = self._build_multi_port_config(source_config, node, port, controller_port)
                    config_path = self._write_multi_port_config(runtime_dir, index, config)
                    process = self._start_multi_port_instance(core_path, runtime_dir, index, config_path)
                    self._wait_port(port)
                    refreshed_proxy = f"http://127.0.0.1:{port}"
                    self._check_proxy_exit(refreshed_proxy)

                    while len(old_processes) <= index:
                        old_processes.append(process)
                    old_processes[index] = process
                    while len(current_nodes) <= index:
                        current_nodes.append("")
                    current_nodes[index] = node
                    self._state.multi_port_processes = old_processes
                    self._state.multi_port_nodes = current_nodes
                    proxies[index] = refreshed_proxy
                    self._state.multi_port_proxies = proxies
                    logger.info("[ProxyProvider] Clash 多入口已刷新: %s -> %s", refreshed_proxy, node)
                    return refreshed_proxy
                except Exception as exc:
                    last_error = str(exc)
                    if process is not None and process.poll() is None:
                        process.terminate()
                    self._wait_port_closed(port)
                    continue

            raise RuntimeError(
                f"Clash 多入口端口刷新失败: selector={selector}, port={port}, error={last_error}"
            )

    def get_proxy(self) -> Optional[str]:
        if self.allocation_mode == "multi_port":
            proxies = self.prepare_for_concurrency(1)
            if not proxies:
                return None
            return self._choose_proxy(proxies)

        if not self.proxy_url:
            raise RuntimeError("Clash 未配置本地代理地址")
        with self._state.lock:
            selector, nodes = self.list_nodes()
            node = self._choose_node(nodes)
            self.switch_node(selector, node)
        return self.proxy_url

    def test_connection(self, *, check_exit: bool = True) -> dict:
        if self.allocation_mode == "multi_port":
            proxies = self.prepare_for_concurrency(1)
            proxy = proxies[0]
            result = {
                "ok": True,
                "mode": "multi_port",
                "proxy": proxy,
                "node_count": len(self._state.multi_port_nodes or []),
                "selected_node": (self._state.multi_port_nodes or [""])[0],
            }
            if check_exit and self.check_url:
                resp = requests.get(
                    self.check_url,
                    proxies={"http": proxy, "https": proxy},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                result["exit_check"] = resp.text[:300]
            return result

        with self._state.lock:
            selector, nodes = self.list_nodes()
            before = self.current_node(selector)
            node = self._choose_node(nodes)
            self.switch_node(selector, node)
        result = {
            "ok": True,
            "selector": selector,
            "previous_node": before,
            "selected_node": node,
            "node_count": len(nodes),
            "proxy": self.proxy_url,
        }
        if check_exit and self.check_url:
            resp = requests.get(
                self.check_url,
                proxies={"http": self.proxy_url, "https": self.proxy_url},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            result["exit_check"] = resp.text[:300]
        return result
