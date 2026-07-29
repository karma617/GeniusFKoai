from __future__ import annotations

from core.base_platform import RegisterConfig
from platforms.chatgpt.authflow_experimental.sentinel_quickjs import _runtime_profile
from platforms.chatgpt.constants import extract_sentinel_sdk_url
from platforms.chatgpt.plugin import ChatGPTPlatform


def test_extract_sentinel_sdk_url_from_entry_script():
    script = "script.src = 'https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js';"

    assert (
        extract_sentinel_sdk_url(script)
        == "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"
    )


def test_chatgpt_browser_registration_mail_otp_timeout_is_30_seconds():
    platform = ChatGPTPlatform(RegisterConfig(executor_type="headless"))

    adapter = platform.build_browser_registration_adapter()

    assert adapter.otp_spec is not None
    assert adapter.otp_spec.timeout == 30


def test_sentinel_quickjs_runtime_profile_matches_headed_mac_firefox():
    profile = _runtime_profile(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0",
        "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
    )

    assert profile["platform"] == "MacIntel"
    assert profile["hardware_concurrency"] == 10
    assert profile["color_depth"] == 30
    assert profile["viewport_width"] == 1800
    assert profile["timezone"] == "Asia/Shanghai"
    assert profile["timezone_offset_min"] == -480
    assert profile["frame_url"] == "https://chatgpt.com/backend-api/sentinel/frame.html?sv=20260219f9f6"
