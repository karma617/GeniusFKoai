from application.momo_trial_probe import choose_decision
from application.tasks import _is_momo_trial_result_taggable


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
