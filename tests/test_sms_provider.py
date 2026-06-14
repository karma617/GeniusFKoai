"""SMS provider unit tests."""
from __future__ import annotations

import json

import pytest
from core.base_sms import (
    FiveSimProvider,
    GrizzlySmsProvider,
    HeroSmsProvider,
    NexSmsProvider,
    SmsActivation,
    SmsActivateProvider,
    SmsPoolProvider,
    SmsVerificationNumberProvider,
    create_sms_provider,
    create_phone_callbacks,
    SMS_ACTIVATE_SERVICES,
    SMS_ACTIVATE_COUNTRIES,
)
import core.base_sms as sms_module


class TestSmsActivateServiceMapping:
    def test_cursor_maps_to_ot(self):
        assert SMS_ACTIVATE_SERVICES["cursor"] == "ot"

    def test_chatgpt_maps_to_dr(self):
        assert SMS_ACTIVATE_SERVICES["chatgpt"] == "dr"

    def test_default_exists(self):
        assert "default" in SMS_ACTIVATE_SERVICES


class TestSmsActivateCountryMapping:
    def test_us_maps_to_187(self):
        assert SMS_ACTIVATE_COUNTRIES["us"] == "187"

    def test_ru_maps_to_0(self):
        assert SMS_ACTIVATE_COUNTRIES["ru"] == "0"

    def test_th_maps_to_52(self):
        assert SMS_ACTIVATE_COUNTRIES["th"] == "52"

    def test_default_exists(self):
        assert "default" in SMS_ACTIVATE_COUNTRIES


class TestCreateSmsProvider:
    def test_sms_activate(self):
        provider = create_sms_provider("sms_activate", {"sms_activate_api_key": "test123"})
        assert isinstance(provider, SmsActivateProvider)
        assert provider.api_key == "test123"

    def test_sms_activate_missing_key(self):
        with pytest.raises(RuntimeError, match="未配置"):
            create_sms_provider("sms_activate", {})

    def test_herosms(self):
        provider = create_sms_provider("herosms", {"herosms_api_key": "hero123"})
        assert isinstance(provider, HeroSmsProvider)
        assert provider.api_key == "hero123"
        assert provider.default_service == "dr"
        assert provider.default_country == "187"

    def test_herosms_reuse_flag_parses_string_false(self):
        provider = create_sms_provider(
            "herosms",
            {
                "herosms_api_key": "hero123",
                "register_reuse_phone_to_max": "false",
            },
        )
        assert isinstance(provider, HeroSmsProvider)
        assert provider.reuse_phone_to_max is False

    def test_herosms_missing_key(self):
        with pytest.raises(RuntimeError, match="HeroSMS 未配置"):
            create_sms_provider("herosms", {})

    def test_gujumpgate_sms_providers_are_available(self):
        grizzly = create_sms_provider("grizzlysms_api", {"grizzlysms_api_key": "key"})
        assert isinstance(grizzly, GrizzlySmsProvider)
        assert grizzly.default_country == "52"

        svn = create_sms_provider("sms_verification_number_api", {"sms_verification_number_api_key": "key"})
        assert isinstance(svn, SmsVerificationNumberProvider)
        assert svn.default_country == "33"

        smspool = create_sms_provider(
            "smspool_api",
            {"smspool_api_key": "key", "sms_service": "dr"},
        )
        assert isinstance(smspool, SmsPoolProvider)
        assert smspool.default_service == "671"

        five_sim = create_sms_provider(
            "five_sim_api",
            {"five_sim_api_key": "key", "sms_service": "dr"},
        )
        assert isinstance(five_sim, FiveSimProvider)
        assert five_sim.product == "openai"

        nexsms = create_sms_provider(
            "nexsms_api",
            {"nexsms_api_key": "key", "nexsms_country_order": "1,2"},
        )
        assert isinstance(nexsms, NexSmsProvider)
        assert nexsms.country_order == [1, 2]

        nexsms_default = create_sms_provider(
            "nexsms_api",
            {"nexsms_api_key": "key", "nexsms_default_country": "7"},
        )
        assert isinstance(nexsms_default, NexSmsProvider)
        assert nexsms_default.country_order == [7]

    def test_unknown_provider(self):
        with pytest.raises(RuntimeError, match="未知"):
            create_sms_provider("unknown", {})


class TestCreatePhoneCallbacks:
    def test_returns_tuple(self):
        # This will fail on actual API call, but we can test the structure
        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="cursor",
        )
        assert callable(callback)
        assert callable(cleanup)

    def test_provider_is_created_lazily_and_cleanup_cancels_pending_activation(self, monkeypatch):
        events = []
        logs = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(activation_id="act_1", phone_number="+15551234567")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return ""

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            country="us",
            log_fn=logs.append,
        )

        assert events == []
        assert callback() == "+15551234567"
        cleanup()
        assert ("get_number", "chatgpt", "us") in events
        assert ("cancel", "act_1") in events
        assert any("准备租用手机号" in item for item in logs)
        assert any("已成功租到号码" in item for item in logs)
        assert any("已释放未使用号码" in item for item in logs)

    def test_number_fetch_logs_balance_and_current_price(self, monkeypatch):
        logs = []

        class FakeProvider:
            def get_balance(self) -> float:
                return 12.345

            def get_current_price_info(self, *, service: str, country: str = ""):
                return {"price": 0.075, "count": 80, "currency": "USD"}

            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(
                    activation_id="act_price",
                    phone_number="+15551234567",
                    country=country,
                    metadata={
                        "activation_cost": "0.08",
                        "price_info": {"price": 0.075, "count": 80, "currency": "USD"},
                        "max_price": 0.225,
                    },
                )

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                return ""

            def cancel(self, activation_id: str) -> bool:
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {"smsbower_api_key": "test"},
            service="chatgpt",
            country="52",
            log_fn=logs.append,
        )

        assert callback() == "+15551234567"
        cleanup()

        joined = "\n".join(logs)
        assert "余额=12.345 USD" in joined
        assert "当前价=0.08 USD" in joined
        assert "stock=80" in joined
        assert "maxPrice=0.225" in joined

    def test_cleanup_does_not_cancel_after_success(self, monkeypatch):
        events = []
        logs = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(activation_id="act_2", phone_number="+15557654321")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "123456"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            log_fn=logs.append,
        )

        assert callback() == "+15557654321"
        assert callback() == "123456"
        cleanup()
        assert ("report_success", "act_2") in events
        assert ("cancel", "act_2") not in events
        assert any("等待短信验证码" in item for item in logs)
        assert any("短信验证成功" in item for item in logs)

    def test_code_timeout_can_be_lowered_for_get_rt_add_phone(self, monkeypatch):
        events = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_timeout", phone_number="+15557654321")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return ""

            def cancel(self, activation_id: str) -> bool:
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
        )
        callback.set_code_timeout(60)

        assert callback() == "+15557654321"
        assert callback() == ""
        cleanup()
        assert ("get_code", "act_timeout", 60) in events

    def test_deferred_success_provider_reports_on_cleanup_for_legacy_callers(self, monkeypatch):
        events = []

        class FakeProvider:
            auto_report_success_on_code = False

            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(activation_id="act_deferred", phone_number="+15550001111")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "111222"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="cursor",
        )

        assert callback() == "+15550001111"
        assert callback() == "111222"
        cleanup()
        assert ("report_success", "act_deferred") in events
        assert ("cancel", "act_deferred") not in events

    def test_first_number_fetch_failure_does_not_poison_future_retries(self, monkeypatch):
        events = []

        class FakeProvider:
            def __init__(self):
                self.calls = 0

            def get_number(self, *, service: str, country: str = ""):
                self.calls += 1
                events.append(("get_number", self.calls, service, country))
                if self.calls == 1:
                    raise RuntimeError("temporary failure")
                return SmsActivation(activation_id="act_retry", phone_number="+66123456789")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "654321"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            country="th",
        )

        with pytest.raises(RuntimeError, match="temporary failure"):
            callback()

        assert callback() == "+66123456789"
        assert callback() == "654321"
        cleanup()
        assert ("report_success", "act_retry") in events

    def test_herosms_number_fetch_failure_releases_verify_lock(self, monkeypatch):
        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                raise RuntimeError("temporary failure")

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
        )

        with pytest.raises(RuntimeError, match="temporary failure"):
            callback()

        assert callback._verify_lock_acquired is False
        cleanup()

    def test_mark_send_succeeded_delegates_to_provider(self, monkeypatch):
        events = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_sent", phone_number="+15551234567")

            def mark_send_succeeded(self, activation_id: str) -> None:
                events.append(("mark_send_succeeded", activation_id))

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
        )

        assert callback() == "+15551234567"
        callback.mark_send_succeeded()
        cleanup()
        assert ("mark_send_succeeded", "act_sent") in events

    def test_phone_callback_switches_country_after_ten_rejected_numbers(self, monkeypatch):
        events = []
        logs = []

        class FakeProvider(HeroSmsProvider):
            def __init__(self):
                pass

            def get_top_countries(self, service: str | None = None):
                events.append(("get_top_countries", service))
                return [
                    {"country": "6", "name": "Indonesia", "price": 0.045, "count": 100},
                    {"country": "52", "name": "Thailand", "price": 0.075, "count": 80},
                    {"country": "187", "name": "United States", "price": 0.12, "count": 50},
                ]

            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                index = len([item for item in events if item[0] == "get_number"])
                return SmsActivation(activation_id=f"act_{index}", phone_number=f"+1555000{index:04d}", country=country)

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                return ""

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
                events.append(("mark_send_failed", activation_id, reason))

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "KEY",
                "smsbower_default_country": "6",
            },
            service="chatgpt",
            log_fn=logs.append,
        )

        assert callback.get_add_phone_attempt_limit(20) == 20

        explicit_callback, _ = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "KEY",
                "smsbower_default_country": "6",
                "sms_country_retry_limit": 1,
                "phone_change_limit": 20,
            },
            service="chatgpt",
            log_fn=logs.append,
        )

        assert explicit_callback.get_add_phone_attempt_limit(10) == 20

        for _ in range(10):
            callback()
            callback.mark_send_failed("We couldn't send a text message to this phone number.")
            cleanup()
            callback.phase = "need_number"
            callback.activation = None
            callback.completed = False

        callback()

        countries = [item[2] for item in events if item[0] == "get_number"]
        assert countries[:10] == ["6"] * 10
        assert countries[10] == "52"
        assert any("接码国家尝试计划" in item for item in logs)
        assert any("切换下一国家" in item for item in logs)


class TestSmsActivateProviderCountryResolution:
    def test_get_number_accepts_numeric_country_id(self, monkeypatch):
        captured = {}

        def fake_request(self, action: str, **params):
            captured["action"] = action
            captured["params"] = params
            return "NO_NUMBERS"

        monkeypatch.setattr(SmsActivateProvider, "_request", fake_request)
        provider = SmsActivateProvider("test123", default_country="ru")

        with pytest.raises(RuntimeError, match="NO_NUMBERS|无可用号码"):
            provider.get_number(service="chatgpt", country="52")

        assert captured["action"] == "getNumber"
        assert captured["params"]["country"] == "52"


class TestSmsPoolProviderReleaseQueue:
    def test_mark_send_failed_cancels_and_queues_when_platform_blocks_cancel(self, monkeypatch, tmp_path):
        from platforms.gopay import sms_channel

        queue_path = tmp_path / "release-queue.json"
        monkeypatch.setattr(sms_channel, "SMSPOOL_RELEASE_QUEUE_PATH", queue_path)
        monkeypatch.setattr(sms_channel, "_ensure_release_worker", lambda: None)

        calls = []

        class FakeResp:
            ok = True

            def __init__(self, data):
                self.text = json.dumps(data)

        def fake_post(url, data=None, **_kwargs):
            calls.append((url, dict(data or {})))
            if url.endswith("/purchase/sms"):
                return FakeResp({"success": 1, "number": "6288899", "order_id": "ORDER123"})
            if url.endswith("/sms/cancel"):
                return FakeResp({
                    "success": 0,
                    "message": "Your order cannot be cancelled yet, please try again later.",
                })
            return FakeResp({"success": 0, "message": "unexpected"})

        monkeypatch.setattr(sms_module.requests, "post", fake_post)

        logs = []
        callback, cleanup = create_phone_callbacks(
            "smspool_api",
            {
                "smspool_api_key": "KEY",
                "smspool_default_country": "9",
                "smspool_default_service": "671",
            },
            service="chatgpt",
            log_fn=logs.append,
        )

        assert callback() == "+6288899"
        callback.mark_send_failed("phone rejected by target")

        assert callback.phase == "need_number"
        assert callback.activation is None
        assert any(call[0].endswith("/sms/cancel") for call in calls)
        queued = json.loads(queue_path.read_text(encoding="utf-8"))
        assert queued[0]["order_id"] == "ORDER123"
        assert queued[0]["phone"] == "+6288899"
        cleanup()


class TestHeroSmsProvider:
    def test_get_number_uses_v2_json(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)
        calls = []

        class FakeResp:
            text = '{"activationId":"act_1","phoneNumber":"5551234","countryPhoneCode":"1","activationCost":"0.6"}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"activationId": "act_1", "phoneNumber": "5551234", "countryPhoneCode": "1", "activationCost": "0.6"}

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params)
            return FakeResp()

        monkeypatch.setattr("core.base_sms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")
        activation = provider.get_number(service="chatgpt", country="187")

        assert activation.activation_id == "act_1"
        assert activation.phone_number == "+15551234"
        assert any(item["action"] == "getNumberV2" for item in calls)

    def test_get_number_falls_back_to_v1_text(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)
        calls = []

        class FakeResp:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

            def json(self):
                raise ValueError("not json")

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params["action"])
            if params["action"] == "getNumberV2":
                return FakeResp("BAD")
            return FakeResp("ACCESS_NUMBER:act_2:15557654321")

        monkeypatch.setattr("core.base_sms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")
        activation = provider.get_number(service="chatgpt", country="187")

        assert activation.activation_id == "act_2"
        assert activation.phone_number == "+15557654321"
        assert calls == ["getPrices", "getNumberV2", "getNumber"]

    def test_get_code_skips_attempted_sms_event(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_3",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        provider = HeroSmsProvider("hero123")
        first = {"status": "ok", "code": "111111", "sms_key": "sms_1", "allow_same_code": True}
        second = {"status": "ok", "code": "222222", "sms_key": "sms_2", "allow_same_code": True}
        results = [first, second]

        monkeypatch.setattr(provider, "get_status_v2", lambda activation_id: results.pop(0))
        monkeypatch.setattr(provider, "get_status", lambda activation_id: {"status": "wait_code"})
        monkeypatch.setattr(provider, "get_active_activations", lambda: [])
        monkeypatch.setattr(provider, "request_resend_sms", lambda activation_id: True)

        assert provider.get_code("act_3", timeout=1) == "111111"
        provider.mark_code_failed("act_3", "invalid otp")
        assert provider.get_code("act_3", timeout=1) == "222222"

    def test_get_code_respects_short_timeout_for_get_rt(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_short",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        provider = HeroSmsProvider("hero123")
        seen = {}

        def fake_wait_for_code(activation_id, *, timeout=180, poll_interval=3):
            seen["activation_id"] = activation_id
            seen["timeout"] = timeout
            return None

        monkeypatch.setattr(provider, "wait_for_code", fake_wait_for_code)

        assert provider.get_code("act_short", timeout=60) == ""
        assert seen == {"activation_id": "act_short", "timeout": 60}

    def test_mark_send_succeeded_sets_sms_sent_status(self, monkeypatch):
        calls = []
        provider = HeroSmsProvider("hero123")
        monkeypatch.setattr(provider, "set_status", lambda activation_id, status: calls.append((activation_id, status)) or "ACCESS_READY")

        provider.mark_send_succeeded("act_4")

        assert calls == [("act_4", 1)]

    def test_mark_code_failed_triggers_openai_and_herosms_resend(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_5",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        events = []
        provider = HeroSmsProvider("hero123")
        provider.last_code_result = {"code": "333333", "sms_key": "sms_3"}
        provider.set_resend_callback(lambda: events.append(("openai_resend",)))
        monkeypatch.setattr(provider, "request_resend_sms", lambda activation_id: events.append(("hero_resend", activation_id)) or True)

        provider.mark_code_failed("act_5", "invalid otp")

        assert ("openai_resend",) in events
        assert ("hero_resend", "act_5") in events
        assert "333333" in sms_module._HERO_SMS_CACHE["used_codes"]
        assert "sms_3" in sms_module._HERO_SMS_CACHE["attempted_sms_keys"]

    def test_report_success_finishes_activation_when_reuse_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_6",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        events = []
        provider = HeroSmsProvider("hero123", reuse_phone_to_max=False)
        provider.last_code_result = {"code": "444444", "sms_key": "sms_4"}
        monkeypatch.setattr(provider, "finish_activation", lambda activation_id: events.append(("finish", activation_id)) or True)

        assert provider.report_success("act_6") is True

        assert events == [("finish", "act_6")]
        assert sms_module._HERO_SMS_CACHE is None


class _RemoteCatalogResp:
    def __init__(self, data, *, ok=True):
        self._data = data
        self.ok = ok
        self.status_code = 200 if ok else 502
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("HTTP error")


class TestRemoteSmsProviderCatalogs:
    def test_sms_verification_number_gets_countries_from_provider_api(self, monkeypatch):
        calls = []

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params)
            return _RemoteCatalogResp([
                {"id": 33, "name": "United States", "operators": {"any": "Any"}},
            ])

        monkeypatch.setattr("core.base_sms.requests.get", fake_get)

        provider = SmsVerificationNumberProvider("KEY")
        countries = provider.get_countries()

        assert calls[0]["action"] == "getCountryAndOperators"
        assert calls[0]["api_key"] == "KEY"
        assert countries[0]["id"] == "33"
        assert countries[0]["chn"] == "United States"

    def test_smspool_catalogs_parse_native_api(self, monkeypatch):
        def fake_post(url, data=None, headers=None, timeout=20, proxies=None):
            if url.endswith("/country/retrieve_all"):
                return _RemoteCatalogResp([
                    {"ID": 1, "name": "United States", "short_name": "US", "cc": "1"},
                ])
            if url.endswith("/service/retrieve_all"):
                return _RemoteCatalogResp([
                    {"ID": 671, "name": "OpenAI / ChatGPT"},
                ])
            return _RemoteCatalogResp({}, ok=False)

        monkeypatch.setattr("core.base_sms.requests.post", fake_post)

        provider = SmsPoolProvider("KEY")

        assert provider.default_country == "1"
        assert provider.get_countries()[0]["id"] == "1"
        assert provider.get_services()[0] == {
            "code": "671",
            "name": "OpenAI / ChatGPT",
            "raw": {"ID": 671, "name": "OpenAI / ChatGPT"},
        }

    def test_five_sim_catalogs_use_guest_endpoints(self, monkeypatch):
        def fake_get(url, headers=None, timeout=20, proxies=None):
            if "/v1/guest/countries" in url:
                return _RemoteCatalogResp({"vietnam": {"text_en": "Vietnam"}})
            if "/v1/guest/products/vietnam/any" in url:
                return _RemoteCatalogResp({"openai": {"Name": "OpenAI", "Qty": 5}})
            return _RemoteCatalogResp({}, ok=False)

        monkeypatch.setattr("core.base_sms.requests.get", fake_get)

        provider = FiveSimProvider("")

        assert provider.get_countries()[0]["id"] == "vietnam"
        assert provider.get_services()[0]["code"] == "openai"

    def test_nexsms_catalogs_parse_api(self, monkeypatch):
        def fake_request(method, url, json=None, headers=None, timeout=20, proxies=None):
            if "/api/countries" in url:
                return _RemoteCatalogResp({"code": 0, "data": [{"id": 1, "name": "United States"}]})
            if "/api/services" in url:
                return _RemoteCatalogResp({"code": 0, "data": [{"code": "ot", "name": "OpenAI"}]})
            return _RemoteCatalogResp({}, ok=False)

        monkeypatch.setattr("core.base_sms.requests.request", fake_request)

        provider = NexSmsProvider("KEY")

        assert provider.get_countries()[0]["id"] == "1"
        assert provider.get_services()[0]["code"] == "ot"


class TestSmsProviderDefinitions:
    def test_gujumpgate_provider_country_and_service_fields_are_async_selects(self):
        from infrastructure.provider_definitions_repository import _BUILTIN_DEFINITIONS

        expected = {
            "grizzlysms_api": (
                ("grizzlysms_default_country", "/sms/grizzlysms/countries"),
                ("grizzlysms_default_service", "/sms/grizzlysms/services"),
            ),
            "sms_verification_number_api": (
                ("sms_verification_number_default_country", "/sms/sms-verification-number/countries"),
                ("sms_verification_number_default_service", "/sms/sms-verification-number/services"),
            ),
            "smspool_api": (
                ("smspool_default_country", "/sms/smspool/countries"),
                ("smspool_default_service", "/sms/smspool/services"),
            ),
            "five_sim_api": (
                ("five_sim_country", "/sms/five-sim/countries"),
                ("five_sim_product", "/sms/five-sim/services"),
            ),
            "nexsms_api": (
                ("nexsms_default_country", "/sms/nexsms/countries"),
                ("nexsms_default_service", "/sms/nexsms/services"),
            ),
        }
        definitions = {item["provider_key"]: item for item in _BUILTIN_DEFINITIONS}

        for provider_key, field_expectations in expected.items():
            fields = {field["key"]: field for field in definitions[provider_key]["fields"]}
            for field_key, async_url in field_expectations:
                assert fields[field_key]["type"] == "async-select"
                assert fields[field_key]["asyncUrl"] == async_url


class TestSmsActivation:
    def test_dataclass(self):
        a = SmsActivation(activation_id="123", phone_number="+79001234567")
        assert a.activation_id == "123"
        assert a.phone_number == "+79001234567"
        assert a.country == ""

    def test_with_country(self):
        a = SmsActivation(activation_id="1", phone_number="+1555", country="us")
        assert a.country == "us"
