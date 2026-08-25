import sys
import threading
import types

import pytest

from platforms.gopay import sms_channel


def _install_fake_worker(monkeypatch):
    opai = types.ModuleType("opai")
    core = types.ModuleType("opai.core")
    worker = types.ModuleType("opai.core.gopay_protocol_worker")
    core.gopay_protocol_worker = worker
    opai.core = core
    monkeypatch.setitem(sys.modules, "opai", opai)
    monkeypatch.setitem(sys.modules, "opai.core", core)
    monkeypatch.setitem(sys.modules, "opai.core.gopay_protocol_worker", worker)
    return worker


def test_worker_sms_dispatcher_is_thread_local(monkeypatch):
    worker = _install_fake_worker(monkeypatch)
    barrier = threading.Barrier(2)
    results = {}
    failures = []

    def run(provider):
        try:
            sms_channel.bind_worker_sms_callbacks(
                get_number=lambda _key: (f"phone-{provider}", f"id-{provider}"),
                wait_code=lambda _key, aid, timeout=0: f"code-{provider}-{aid}",
                request_another=lambda _key, aid: f"retry-{provider}-{aid}",
                cancel=lambda _key, aid: results.setdefault(f"cancel-{provider}", aid),
                done=lambda _key, aid: results.setdefault(f"done-{provider}", aid),
            )
            barrier.wait(timeout=5)
            phone, aid = worker.sms_get_number("ignored")
            results[provider] = (
                phone,
                aid,
                worker.sms_wait_code("ignored", aid, timeout=1),
                worker.sms_request_another("ignored", aid),
            )
            worker.sms_cancel("ignored", aid)
            worker.sms_done("ignored", aid)
        except Exception as exc:  # pragma: no cover - assertion reports details
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert results["alpha"] == (
        "phone-alpha",
        "id-alpha",
        "code-alpha-id-alpha",
        "retry-alpha-id-alpha",
    )
    assert results["beta"] == (
        "phone-beta",
        "id-beta",
        "code-beta-id-beta",
        "retry-beta-id-beta",
    )
    assert results["cancel-alpha"] == "id-alpha"
    assert results["cancel-beta"] == "id-beta"
    assert results["done-alpha"] == "id-alpha"
    assert results["done-beta"] == "id-beta"
    assert worker.sms_get_number is sms_channel._dispatch_sms_get_number


def test_worker_sms_dispatcher_fails_closed_without_binding(monkeypatch):
    worker = _install_fake_worker(monkeypatch)
    sms_channel.bind_worker_sms_callbacks(
        get_number=lambda _key: ("phone", "id"),
        wait_code=lambda *_args, **_kwargs: "code",
        request_another=lambda *_args, **_kwargs: True,
        cancel=lambda *_args, **_kwargs: None,
        done=lambda *_args, **_kwargs: None,
    )

    errors = []

    def run_unbound():
        with pytest.raises(RuntimeError, match="未绑定接码通道"):
            worker.sms_get_number("ignored")
        errors.append("checked")

    thread = threading.Thread(target=run_unbound)
    thread.start()
    thread.join(timeout=5)
    assert errors == ["checked"]


def test_smsapi_accepts_new_code_with_same_timestamp(monkeypatch):
    channel = sms_channel.SmsApiChannel(
        url="https://sms.example.test/latest",
        phone="+447700900001",
    )
    timestamp = "2026-06-04 11:00:00"
    channel._last_seen_fingerprint = channel._message_fingerprint(
        timestamp, "Your old code is 1111"
    )
    monkeypatch.setattr(
        channel,
        "_fetch",
        lambda: {"code_time": timestamp, "code": "Your new code is 2222"},
    )

    assert channel.wait_code(channel.phone, timeout=1) == "2222"


def test_smsapi_accepts_new_code_without_timestamp(monkeypatch):
    channel = sms_channel.SmsApiChannel(
        url="https://sms.example.test/latest",
        phone="+447700900002",
    )
    monkeypatch.setattr(
        channel,
        "_fetch",
        lambda: {"code_time": "", "code": "Use 3456 as your GoPay OTP"},
    )

    assert channel.wait_code(channel.phone, timeout=1) == "3456"


def test_smsapi_accepts_message_data_when_api_code_is_nonstandard(monkeypatch):
    class Response:
        def json(self):
            return {
                "code": 200,
                "data": {
                    "code": "Use 4567 as your GoPay OTP",
                    "code_time": "2026-06-04 11:01:00",
                },
            }

    class Client:
        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(sms_channel, "_new_session", lambda: Client())
    channel = sms_channel.SmsApiChannel(
        url="https://sms.example.test/latest",
        phone="+447700900003",
    )

    assert channel._fetch()["code"].endswith("GoPay OTP")
