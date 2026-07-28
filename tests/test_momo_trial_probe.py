import application.momo_trial_probe as momo_trial_probe
from application.momo_trial_probe import choose_decision, probe_momo_trial
from application.tasks import _is_momo_trial_result_taggable, _momo_trial_probe_result_data


def test_trial_without_momo_is_not_taggable():
    result = {
        "decision": "momo_not_enabled",
        "supported": False,
        "has_momo": False,
        "trial": {"has_real_trial": True},
        "payment_method_types": ["card", "link", "apple_pay", "google_pay"],
    }

    assert _is_momo_trial_result_taggable(result) is False


def test_ready_with_momo_is_taggable():
    result = {
        "decision": "ready",
        "supported": True,
        "has_momo": True,
        "trial": {"has_real_trial": True},
        "payment_method_types": ["card", "momo"],
    }

    assert _is_momo_trial_result_taggable(result) is True


def test_choose_decision_keeps_trial_without_momo_separate():
    decision = choose_decision(
        checkout_ok=True,
        checkout_error="",
        trial_flags={"has_real_trial": True, "one_click_trial_eligible": False},
        payment_methods=["card", "link", "apple_pay", "google_pay"],
        mode="subscription",
        stripe_ok=True,
    )

    assert decision["decision"] == "momo_not_enabled"
    assert decision["supported"] is False


def test_momo_trial_probe_result_data_keeps_network_failures_out_of_ineligible():
    assert _momo_trial_probe_result_data(
        total=50,
        ready=0,
        ineligible=45,
        failed=5,
        completed=50,
    ) == {
        "total": 50,
        "ready": 0,
        "ineligible": 45,
        "failed": 5,
        "completed": 50,
        "remaining": 0,
    }


def test_momo_trial_probe_retries_checkout_network_error_five_times(monkeypatch):
    calls = []
    logs = []

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "checkout_session_id": "cs_test",
                "publishable_key": "pk_test",
                "one_click_trial_eligible": True,
            }

    class Session:
        def post(self, *_args, **_kwargs):
            calls.append(1)
            if len(calls) <= 5:
                raise RuntimeError("ProxyError")
            return Response()

    monkeypatch.setattr(momo_trial_probe, "build_protocol_session", lambda **_kwargs: Session())
    monkeypatch.setattr(momo_trial_probe, "stripe_init", lambda *_args, **_kwargs: {
        "payment_method_types": ["card", "momo"],
        "mode": "subscription",
        "invoice": {"amount_due": 0},
    })
    monkeypatch.setattr(momo_trial_probe.time, "sleep", lambda _seconds: None)

    result = probe_momo_trial(access_token="token", log_fn=logs.append)

    assert len(calls) == 6
    assert result["decision"] == "ready"
    assert sum("checkout 网络异常" in line for line in logs) == 5
