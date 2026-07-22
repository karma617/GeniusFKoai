from __future__ import annotations

from core.proxy_pool import proxy_pool
from domain.proxies import ProxyBulkCreateCommand, ProxyCheckSummary, ProxyCreateCommand, ProxyRecord
from application.free_proxy_checker import (
    check_free_proxies,
    fetch_free_proxies,
    get_free_proxy_sources,
)
from infrastructure.proxies_repository import ProxiesRepository


class ProxiesService:
    def __init__(self, repository: ProxiesRepository | None = None):
        self.repository = repository or ProxiesRepository()

    def list_proxies(self) -> list[dict]:
        return [self._serialize(item) for item in self.repository.list()]

    def create_proxy(self, command: ProxyCreateCommand) -> dict | None:
        item = self.repository.create(command)
        return self._serialize(item) if item else None

    def bulk_create_proxies(self, command: ProxyBulkCreateCommand) -> dict:
        added = self.repository.bulk_create(command.proxies, command.region, command.import_scheme)
        return {"added": added}

    def delete_proxy(self, proxy_id: int) -> dict:
        return {"ok": self.repository.delete(proxy_id)}

    def delete_all_proxies(self) -> dict:
        deleted = self.repository.delete_all()
        return {"ok": True, "deleted": deleted}

    def toggle_proxy(self, proxy_id: int) -> dict | None:
        value = self.repository.toggle(proxy_id)
        if value is None:
            return None
        return {"is_active": value}

    def trigger_check(self) -> dict:
        result = proxy_pool.check_all()
        return {"message": "检测完成", **result}

    def check_proxy(self, proxy_id: int) -> dict | None:
        item = self.repository.get(proxy_id)
        if not item:
            return None
        result = proxy_pool.check_one(item.url)
        return {"message": "检测完成", **result}

    def free_proxy_capabilities(self) -> dict:
        return get_free_proxy_sources()

    def fetch_free_proxies(self, *, source: str, limit: int) -> dict:
        return fetch_free_proxies(source, limit=limit)

    def check_free_proxies(
        self,
        *,
        proxies: list[str],
        rounds: int = 1,
        timeout: int = 10,
        concurrency: int = 20,
        limit: int = 120,
    ) -> dict:
        return check_free_proxies(
            proxies,
            rounds=rounds,
            timeout=timeout,
            concurrency=concurrency,
            limit=limit,
        )

    def import_free_proxies(self, *, proxies: list[str], region: str = "") -> dict:
        added = self.repository.bulk_create(proxies, region or "FREE")
        return {"added": added, "total": len(proxies)}

    @staticmethod
    def _serialize(item: ProxyRecord) -> dict:
        return {
            "id": item.id,
            "url": item.url,
            "region": item.region,
            "success_count": item.success_count,
            "fail_count": item.fail_count,
            "is_active": item.is_active,
            "last_checked": item.last_checked,
        }
