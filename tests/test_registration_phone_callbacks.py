from __future__ import annotations

from types import SimpleNamespace

from core.base_platform import RegisterConfig
from core.registration import (
    BrowserRegistrationAdapter,
    BrowserRegistrationFlow,
    ProtocolMailboxAdapter,
    ProtocolMailboxFlow,
    RegistrationContext,
    RegistrationResult,
)
import core.registration.helpers as helpers_module
import core.registration.flows as flows_module


def test_browser_flow_wires_phone_callback_and_runs_cleanup(monkeypatch):
    events = []

    def fake_build_phone_callbacks(ctx, *, service=None):
        events.append(("build", service))
        return (lambda: "18885551234", lambda: events.append(("cleanup", service)))

    monkeypatch.setattr(flows_module, "build_phone_callbacks", fake_build_phone_callbacks)

    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(
            email="user@example.com",
            has_mailbox=True,
            identity_provider="mailbox",
        ),
        config=RegisterConfig(executor_type="headless", extra={}),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )

    def build_worker(ctx, artifacts):
        assert callable(artifacts.phone_callback)
        return SimpleNamespace(phone_callback=artifacts.phone_callback)

    def run_worker(worker, ctx, artifacts):
        events.append(("callback", worker.phone_callback()))
        return {"email": ctx.identity.email, "password": ctx.password}

    adapter = BrowserRegistrationAdapter(
        result_mapper=lambda ctx, raw: RegistrationResult(email=raw["email"], password=raw["password"]),
        browser_worker_builder=build_worker,
        browser_register_runner=run_worker,
    )

    result = BrowserRegistrationFlow(adapter).run(ctx)

    assert result.email == "user@example.com"
    assert ("build", "chatgpt") in events
    assert ("callback", "18885551234") in events
    assert ("cleanup", "chatgpt") in events


def test_protocol_flow_wires_phone_callback_only_for_sms_oauth(monkeypatch):
    events = []

    def fake_build_phone_callbacks(ctx, *, service=None):
        events.append(("build", getattr(ctx.identity, "identity_provider", ""), service))
        return (lambda: "18885550000", lambda: events.append(("cleanup", service)))

    monkeypatch.setattr(flows_module, "build_phone_callbacks", fake_build_phone_callbacks)

    adapter = ProtocolMailboxAdapter(
        result_mapper=lambda ctx, raw: RegistrationResult(email=raw["email"], password=raw["password"]),
        worker_builder=lambda ctx, artifacts: SimpleNamespace(phone_callback=artifacts.phone_callback),
        register_runner=lambda worker, ctx, artifacts: {
            "email": ctx.identity.email,
            "password": ctx.password,
            "phone": worker.phone_callback() if worker.phone_callback else "",
        },
        use_phone_callback=True,
    )

    sms_ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(
            email="user@example.com",
            has_mailbox=True,
            identity_provider="sms_oauth",
        ),
        config=RegisterConfig(executor_type="protocol", extra={}),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )
    mailbox_ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(
            email="mailbox@example.com",
            has_mailbox=True,
            identity_provider="mailbox",
        ),
        config=RegisterConfig(executor_type="protocol", extra={}),
        email="mailbox@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )

    ProtocolMailboxFlow(adapter).run(sms_ctx)
    ProtocolMailboxFlow(adapter).run(mailbox_ctx)

    assert events == [("build", "sms_oauth", "chatgpt"), ("cleanup", "chatgpt")]


def test_build_phone_callbacks_passes_phone_change_limit_without_overriding_sms_limits(monkeypatch):
    captured = {}

    class FakeDefinitionsRepository:
        def get_by_key(self, _category, _provider_key):
            return SimpleNamespace(get_fields=lambda: [{"key": "smsbower_api_key", "category": "auth"}])

    class FakeSettingsRepository:
        def get_default_provider_key(self, _category):
            return ""

        def resolve_runtime_settings(self, _category, provider_key, extra):
            settings = dict(extra)
            settings["resolved_provider"] = provider_key
            return settings

    def fake_create_phone_callbacks(provider_key, config, *, service, country="", log_fn=None):
        captured.update({
            "provider_key": provider_key,
            "config": dict(config),
            "service": service,
            "country": country,
        })
        return (lambda: "+15551234567", lambda: None)

    monkeypatch.setattr(
        "infrastructure.provider_definitions_repository.ProviderDefinitionsRepository",
        lambda: FakeDefinitionsRepository(),
    )
    monkeypatch.setattr(
        "infrastructure.provider_settings_repository.ProviderSettingsRepository",
        lambda: FakeSettingsRepository(),
    )
    monkeypatch.setattr(helpers_module, "create_phone_callbacks", fake_create_phone_callbacks)

    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(identity_provider="sms_oauth"),
        config=RegisterConfig(
            executor_type="headless",
            extra={
                "sms_provider": "smsbower_api",
                "smsbower_api_key": "KEY",
                "phone_change_limit": "30",
            },
        ),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )

    callback, cleanup = helpers_module.build_phone_callbacks(ctx, service="dr")

    assert callable(callback)
    assert callable(cleanup)
    assert captured["provider_key"] == "smsbower_api"
    assert captured["service"] == "dr"
    assert captured["config"]["phone_change_limit"] == 30
    assert "sms_phone_retry_limit" not in captured["config"]
    assert "sms_phone_failures_per_country" not in captured["config"]


def test_build_phone_callbacks_task_phone_change_limit_preserves_resolved_sms_limits(monkeypatch):
    captured = {}

    class FakeDefinitionsRepository:
        def get_by_key(self, _category, _provider_key):
            return SimpleNamespace(get_fields=lambda: [{"key": "smsbower_api_key", "category": "auth"}])

    class FakeSettingsRepository:
        def get_default_provider_key(self, _category):
            return ""

        def resolve_runtime_settings(self, _category, provider_key, extra):
            settings = dict(extra)
            settings["sms_phone_retry_limit"] = 2
            settings["sms_phone_failures_per_country"] = 2
            settings["sms_country_retry_limit"] = 1
            return settings

    def fake_create_phone_callbacks(provider_key, config, *, service, country="", log_fn=None):
        captured.update({
            "provider_key": provider_key,
            "config": dict(config),
            "service": service,
            "country": country,
        })
        return (lambda: "+15551234567", lambda: None)

    monkeypatch.setattr(
        "infrastructure.provider_definitions_repository.ProviderDefinitionsRepository",
        lambda: FakeDefinitionsRepository(),
    )
    monkeypatch.setattr(
        "infrastructure.provider_settings_repository.ProviderSettingsRepository",
        lambda: FakeSettingsRepository(),
    )
    monkeypatch.setattr(helpers_module, "create_phone_callbacks", fake_create_phone_callbacks)

    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(identity_provider="sms_oauth"),
        config=RegisterConfig(
            executor_type="headless",
            extra={
                "sms_provider": "smsbower_api",
                "smsbower_api_key": "KEY",
                "phone_change_limit": "30",
            },
        ),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )

    callback, cleanup = helpers_module.build_phone_callbacks(ctx, service="dr")

    assert callable(callback)
    assert callable(cleanup)
    assert captured["config"]["phone_change_limit"] == 30
    assert captured["config"]["sms_phone_retry_limit"] == 2
    assert captured["config"]["sms_phone_failures_per_country"] == 2
    assert captured["config"]["sms_country_retry_limit"] == 1
