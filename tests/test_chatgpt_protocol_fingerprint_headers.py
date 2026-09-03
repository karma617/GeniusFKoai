"""Platform OAuth UA/Client-Hints 一致性 focused tests（纯无网络）。

- 默认 Firefox 指纹（RegistrationEngine 现配置：Firefox UA + 空 sec_ch_ua

  + macOS 指纹）在 nav/json 请求头中完全不发送 sec-ch-ua* client hints。
- Chromium UA 且 sec_ch_ua 非空才发送 hints；UA 明确 Windows/Macintosh/
  Linux/Android 时 platform/platform-version 以 UA 为准（归一不一致值），
  其余 arch/bitness/full-version 保持 fingerprint 现值。
- nav/json 两处请求头经同一 helper，client hints 一致；其余普通头不丢。
"""
from types import SimpleNamespace

from platforms.chatgpt.register import (
    LATEST_CHATGPT_FIREFOX_USER_AGENT,
    PLATFORM_REFERENCE_SEC_CH_UA,
    PLATFORM_REFERENCE_SEC_CH_UA_FULL,
    PLATFORM_REFERENCE_USER_AGENT,
    ProtocolFingerprint,
    RegistrationEngine,
    _platform_client_hint_headers,
)

HINT_KEY_PREFIX = "sec-ch-ua"

WINDOWS_CHROME_UA = PLATFORM_REFERENCE_USER_AGENT
MAC_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
ANDROID_CHROME_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36"
)
LINUX_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


def _firefox_fingerprint() -> ProtocolFingerprint:
    """复刻 RegistrationEngine 默认配置：Firefox UA + 空 sec_ch_ua + macOS 指纹。"""
    return ProtocolFingerprint(
        device_id="did-firefox",
        user_agent=LATEST_CHATGPT_FIREFOX_USER_AGENT,
        sec_ch_ua="",
        sec_ch_ua_full="",
        sec_ch_ua_platform='"macOS"',
        sec_ch_ua_platform_version='"10.15.0"',
        sec_ch_ua_arch='"x86"',
        sec_ch_ua_bitness='"64"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_model='""',
    )


def _chrome_fingerprint(*, user_agent, platform, platform_version, arch='"x86_64"'):
    return ProtocolFingerprint(
        device_id="did-chrome",
        user_agent=user_agent,
        sec_ch_ua=PLATFORM_REFERENCE_SEC_CH_UA,
        sec_ch_ua_full=PLATFORM_REFERENCE_SEC_CH_UA_FULL,
        sec_ch_ua_platform=platform,
        sec_ch_ua_platform_version=platform_version,
        sec_ch_ua_arch=arch,
        sec_ch_ua_bitness='"64"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_model='""',
    )


def _stub_engine(fingerprint):
    """仅提供 _platform_*_headers 依赖的属性，绕过 RegistrationEngine 重初始化。"""
    return SimpleNamespace(
        protocol_fingerprint=fingerprint,
        http_client=SimpleNamespace(
            default_headers={"User-Agent": fingerprint.user_agent}
        ),
    )


def _nav_headers(engine_stub):
    return RegistrationEngine._platform_nav_headers(
        engine_stub, referer="https://auth.openai.com/create-account"
    )


def _json_headers(engine_stub):
    return RegistrationEngine._platform_json_headers(
        engine_stub, device_id="did-1", referer="https://auth.openai.com/create-account"
    )


def _hints(headers):
    return {key: value for key, value in headers.items() if key.startswith(HINT_KEY_PREFIX)}


def test_default_firefox_fingerprint_sends_no_client_hints():
    fingerprint = _firefox_fingerprint()
    assert _platform_client_hint_headers(fingerprint, fingerprint.user_agent) == {}

    engine_stub = _stub_engine(fingerprint)
    nav = _nav_headers(engine_stub)
    json_headers = _json_headers(engine_stub)
    assert _hints(nav) == {}
    assert _hints(json_headers) == {}
    assert nav["user-agent"] == LATEST_CHATGPT_FIREFOX_USER_AGENT
    assert json_headers["user-agent"] == LATEST_CHATGPT_FIREFOX_USER_AGENT
    assert nav["sec-fetch-dest"] == "document"
    assert json_headers["sec-fetch-dest"] == "empty"


def test_windows_chrome_ua_normalizes_mismatched_macos_platform():
    fingerprint = _chrome_fingerprint(
        user_agent=WINDOWS_CHROME_UA,
        platform='"macOS"',
        platform_version='"10.15.0"',
        arch='"x86"',
    )
    hints = _platform_client_hint_headers(fingerprint, fingerprint.user_agent)
    assert hints, "Windows Chrome + 非空 sec_ch_ua 必须发送 client hints"
    assert hints["sec-ch-ua-platform"] == '"Windows"'
    assert hints["sec-ch-ua-platform-version"] == '"10.0.0"'
    assert hints["sec-ch-ua"] == PLATFORM_REFERENCE_SEC_CH_UA
    assert hints["sec-ch-ua-full-version-list"] == PLATFORM_REFERENCE_SEC_CH_UA_FULL
    assert hints["sec-ch-ua-arch"] == '"x86"'
    assert hints["sec-ch-ua-bitness"] == '"64"'
    assert hints["sec-ch-ua-mobile"] == "?0"
    assert hints["sec-ch-ua-model"] == '""'

    engine_stub = _stub_engine(fingerprint)
    assert _hints(_nav_headers(engine_stub)) == hints
    assert _hints(_json_headers(engine_stub)) == hints


def test_mac_chrome_ua_keeps_macos_platform():
    fingerprint = _chrome_fingerprint(
        user_agent=MAC_CHROME_UA,
        platform='"macOS"',
        platform_version='"14.2.0"',
    )
    hints = _platform_client_hint_headers(fingerprint, fingerprint.user_agent)
    assert hints["sec-ch-ua-platform"] == '"macOS"'
    assert hints["sec-ch-ua-platform-version"] == '"14.2.0"'


def test_android_linux_chrome_ua_normalizes_mismatched_platform():
    android = _chrome_fingerprint(
        user_agent=ANDROID_CHROME_UA,
        platform='"Windows"',
        platform_version='"10.0.0"',
    )
    android_hints = _platform_client_hint_headers(android, android.user_agent)
    assert android_hints["sec-ch-ua-platform"] == '"Android"'
    assert android_hints["sec-ch-ua-platform-version"] == '"13"'

    linux = _chrome_fingerprint(
        user_agent=LINUX_CHROME_UA,
        platform='"macOS"',
        platform_version='"10.15.0"',
    )
    linux_hints = _platform_client_hint_headers(linux, linux.user_agent)
    assert linux_hints["sec-ch-ua-platform"] == '"Linux"'
    assert linux_hints["sec-ch-ua-platform-version"] == '"6.2.0"'


def test_empty_sec_ch_ua_drops_hints_for_chrome_ua():
    fingerprint = ProtocolFingerprint(
        device_id="did-empty",
        user_agent=WINDOWS_CHROME_UA,
        sec_ch_ua="",
        sec_ch_ua_full="",
        sec_ch_ua_platform='"macOS"',
    )
    assert _platform_client_hint_headers(fingerprint, fingerprint.user_agent) == {}


def test_firefox_ua_ignores_populated_sec_ch_ua():
    fingerprint = ProtocolFingerprint(
        device_id="did-firefox-hints",
        user_agent=LATEST_CHATGPT_FIREFOX_USER_AGENT,
        sec_ch_ua=PLATFORM_REFERENCE_SEC_CH_UA,
        sec_ch_ua_full=PLATFORM_REFERENCE_SEC_CH_UA_FULL,
        sec_ch_ua_platform='"macOS"',
    )
    assert _platform_client_hint_headers(fingerprint, fingerprint.user_agent) == {}


def test_created_chromium_fingerprint_stays_consistent():
    fingerprint = ProtocolFingerprint.create()
    hints = _platform_client_hint_headers(fingerprint, fingerprint.user_agent)
    assert hints["sec-ch-ua"] == fingerprint.sec_ch_ua
    assert hints["sec-ch-ua-platform"] == '"Windows"'
    assert hints["sec-ch-ua-mobile"] == "?0"


def test_nav_and_json_keep_non_hint_headers():
    fingerprint = _chrome_fingerprint(
        user_agent=WINDOWS_CHROME_UA,
        platform='"Windows"',
        platform_version='"10.0.0"',
    )
    engine_stub = _stub_engine(fingerprint)
    referer = "https://auth.openai.com/create-account"

    nav = _nav_headers(engine_stub)
    assert nav["user-agent"] == WINDOWS_CHROME_UA
    assert nav["accept"].startswith("text/html")
    assert nav["sec-fetch-mode"] == "navigate"
    assert nav["sec-fetch-site"] == "same-origin"
    assert nav["upgrade-insecure-requests"] == "1"
    assert nav["referer"] == referer

    json_headers = _json_headers(engine_stub)
    assert json_headers["user-agent"] == WINDOWS_CHROME_UA
    assert json_headers["accept"] == "application/json"
    assert json_headers["content-type"] == "application/json"
    assert json_headers["sec-fetch-mode"] == "cors"
    assert json_headers["oai-device-id"] == "did-1"
    assert json_headers["referer"] == referer
    assert "traceparent" in json_headers
    assert "x-datadog-trace-id" in json_headers

    nav_hints = _hints(nav)
    json_hints = _hints(json_headers)
    assert nav_hints == json_hints
    assert nav_hints["sec-ch-ua-platform"] == '"Windows"'
