from application import tasks


def test_proxy_rotation_uses_initial_plus_five_replacements():
    pool = [f"http://proxy-{index}.example.test:8080" for index in range(20)]

    candidates = tasks._gopay_proxy_rotation_candidates(
        pool,
        pool[0],
        resource_index=0,
        account_count=1,
        max_switches=5,
    )

    assert candidates == pool[:6]


def test_concurrent_accounts_receive_separate_proxy_lanes():
    pool = [f"http://proxy-{index}.example.test:8080" for index in range(30)]

    first = tasks._gopay_proxy_rotation_candidates(
        pool,
        pool[0],
        resource_index=0,
        account_count=2,
        max_switches=5,
    )
    second = tasks._gopay_proxy_rotation_candidates(
        pool,
        pool[1],
        resource_index=1,
        account_count=2,
        max_switches=5,
    )

    assert first == [pool[index] for index in (0, 2, 4, 6, 8, 10)]
    assert second == [pool[index] for index in (1, 3, 5, 7, 9, 11)]
    assert set(first).isdisjoint(second)


def test_only_retryable_proxy_and_checkout_risk_errors_rotate():
    assert tasks._is_gopay_precharge_proxy_retryable(
        "curl: (56) Proxy CONNECT aborted"
    )
    assert tasks._is_gopay_precharge_proxy_retryable(
        "OpenAI checkout detected unusual activity"
    )
    assert not tasks._is_gopay_precharge_proxy_retryable(
        "ChatGPT access token is invalid"
    )
    assert not tasks._is_gopay_precharge_proxy_retryable(
        "GoPay payment declined"
    )


def test_proxy_switch_is_blocked_after_checkout_or_uncertain_charge():
    assert tasks._can_switch_gopay_precharge_proxy(
        {
            "status": "failed_precharge",
            "midtrans_url": "",
            "snap_id": "",
            "uncertain": False,
        }
    )
    assert not tasks._can_switch_gopay_precharge_proxy(
        {
            "status": "checkout_ready",
            "midtrans_url": "https://app.midtrans.example/snap",
            "snap_id": "snap-id",
            "uncertain": False,
        }
    )
    assert not tasks._can_switch_gopay_precharge_proxy(
        {
            "status": "uncertain",
            "midtrans_url": "",
            "snap_id": "",
            "uncertain": True,
        }
    )


def test_long_link_defaults_to_browser_and_short_link_defaults_to_protocol():
    assert tasks._resolve_gopay_link_mode(
        {}, use_stripe_init=True, use_short_link=False
    ) == "browser"
    # 短链默认改走纯协议提链（不开浏览器）；显式 link_mode 仍可覆盖。
    assert tasks._resolve_gopay_link_mode(
        {}, use_stripe_init=False, use_short_link=True
    ) == "protocol"


def test_default_and_explicit_gopay_link_mode_are_preserved():
    assert tasks._resolve_gopay_link_mode(
        {}, use_stripe_init=False, use_short_link=False
    ) == "protocol"
    assert tasks._resolve_gopay_link_mode(
        {"link_mode": "protocol"}, use_stripe_init=True, use_short_link=True
    ) == "protocol"
    assert tasks._resolve_gopay_link_mode(
        {"link_mode": "browser"}, use_stripe_init=False, use_short_link=False
    ) == "browser"
