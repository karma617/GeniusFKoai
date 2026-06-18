"""ChatGPT 浏览器注册流程（Camoufox）。"""
import base64
import json
import random
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from camoufox.sync_api import Camoufox

from .._browser_backend import BrowserBackendConfig, open_browser_backend
from .._register_browser_window import (
    REGISTER_BROWSER_WINDOW_ARG,
    apply_camoufox_register_window_size,
    set_register_page_viewport,
)
from .constants import (
    OPENAI_AUTH,
    CHATGPT_APP,
    PLATFORM_LOGIN_ENTRY,
    SENTINEL_SDK_URL,
    SENTINEL_REQ_URL,
    SENTINEL_FRAME_URL,
    SENTINEL_BASE,
    OAUTH_CONSENT_FORM_SELECTOR,
)


def _is_transient_nav_error(exc: BaseException) -> bool:
    """page.goto / page.reload 抛错是否属于可重试的瞬时网络断连。

    覆盖 Chromium/Firefox 常见的瞬时网络错误码。业务/页面错误（4xx、选择器
    超时等）不在此列，不会被误判重试。
    """
    msg = str(exc or "").lower()
    return any(
        token in msg
        for token in (
            "err_connection_closed",
            "err_connection_reset",
            "err_connection_refused",
            "err_connection_aborted",
            "err_connection_failed",
            "err_timed_out",
            "err_network_changed",
            "err_empty_response",
            "err_socks_connection_failed",
            "err_proxy_connection_failed",
            "err_tunnel_connection_failed",
            "err_name_not_resolved",
            "err_address_unreachable",
            "ns_error_net",            # Firefox/Camoufox 网络错误前缀
            "neterror",
            "navigating to",           # Playwright 包装的导航失败常带这句
        )
    )


def _goto_with_retry(
    page,
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    attempts: int = 3,
    log: Optional[Callable[[str], None]] = None,
):
    """``page.goto`` 带瞬时网络错误重试（默认 3 次，指数退避）。

    全局统一：注册流程里所有打开页面都该走这个，避免一次网络波动
    （ERR_CONNECTION_CLOSED / RESET / TIMED_OUT 等）就直接判失败。
    瞬时错误重试；业务错误（页面 4xx、选择器问题）原样抛出不重试。
    """
    _log = log or (lambda *_a, **_k: None)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 按错误内容判定是否重试
            last_exc = exc
            if attempt >= attempts or not _is_transient_nav_error(exc):
                raise
            backoff = 1.5 * attempt
            _log(
                f"打开页面瞬时网络失败（第 {attempt}/{attempts} 次，{backoff:.1f}s 后重试）："
                f"{str(exc)}"
            )
            time.sleep(backoff)
    if last_exc is not None:
        raise last_exc


def _reload_with_retry(
    page,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    attempts: int = 3,
    log: Optional[Callable[[str], None]] = None,
):
    """``page.reload`` 带瞬时网络错误重试。"""
    _log = log or (lambda *_a, **_k: None)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return page.reload(wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= attempts or not _is_transient_nav_error(exc):
                raise
            time.sleep(1.5 * attempt)
    if last_exc is not None:
        raise last_exc

EMAIL_INPUT_SELECTORS = [
    'input#login-email',
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[autocomplete*="username"]',
    'input[inputmode="email"]',
    'input[id*="email"]',
]

PASSWORD_INPUT_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="new-password"]',
]

EMAIL_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Next")',
    'button:has-text("next")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
]

PASSWORD_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Sign up")',
    'button:has-text("sign up")',
    'button:has-text("Create account")',
    'button:has-text("create account")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("登録")',
    'button:has-text("新規登録")',
    'button:has-text("アカウントを作成")',
    'button:has-text("サインアップ")',
]

OTP_INPUT_SELECTORS = [
    "input[inputmode='numeric']",
    "input[autocomplete='one-time-code']",
    "input[type='tel']",
    "input[type='number']",
    "input[name*='code' i]",
    "input[id*='code' i]",
]

SIGNUP_RECOVERY_SELECTORS = [
    'a:has-text("Sign up")',
    'button:has-text("Sign up")',
    'a:has-text("sign up")',
    'button:has-text("sign up")',
    'a:has-text("Register")',
    'button:has-text("Register")',
    'a:has-text("Create account")',
    'button:has-text("Create account")',
    'a:has-text("创建账号")',
    'button:has-text("创建账号")',
    'a:has-text("注册")',
    'button:has-text("注册")',
    'a:has-text("登録")',
    'button:has-text("登録")',
    'a:has-text("新規登録")',
    'button:has-text("新規登録")',
    'a:has-text("アカウントを作成")',
    'button:has-text("アカウントを作成")',
    'a:has-text("サインアップ")',
    'button:has-text("サインアップ")',
]

PASSWORDLESS_LOGIN_SELECTORS = [
    'button[name="intent"][value="passwordless_login_send_otp"]',
    'button[value="passwordless_login_send_otp"]',
    'button:has-text("one-time code")',
    'button:has-text("one time code")',
    'button:has-text("passwordless")',
    'button:has-text("一次性验证码")',
    'button:has-text("驗證碼")',
    'button:has-text("验证码")',
    'button:has-text("código único")',
    'button:has-text("code unique")',
    'button:has-text("Einmalcode")',
    'button:has-text("código de uso único")',
    'button:has-text("ワンタイムコード")',
    'button:has-text("一回限りのコード")',
    'button:has-text("認証コード")',
]

# add-phone 页面国际拨号码 -> 国家名映射（用于 UI 下拉选择）
PHONE_COUNTRY_CODE_MAP = {
    "1": "United States", "7": "Russia", "20": "Egypt", "27": "South Africa",
    "30": "Greece", "31": "Netherlands", "32": "Belgium", "33": "France",
    "34": "Spain", "36": "Hungary", "39": "Italy", "40": "Romania",
    "44": "United Kingdom", "45": "Denmark", "46": "Sweden", "47": "Norway",
    "48": "Poland", "49": "Germany", "51": "Peru", "52": "Mexico",
    "53": "Cuba", "54": "Argentina", "55": "Brazil", "56": "Chile",
    "57": "Colombia", "58": "Venezuela", "60": "Malaysia", "61": "Australia",
    "62": "Indonesia", "63": "Philippines", "64": "New Zealand",
    "65": "Singapore", "66": "Thailand", "81": "Japan", "82": "South Korea",
    "84": "Vietnam", "86": "China", "90": "Turkey", "91": "India",
    "92": "Pakistan", "93": "Afghanistan", "94": "Sri Lanka", "95": "Myanmar",
    "98": "Iran", "212": "Morocco", "213": "Algeria", "216": "Tunisia",
    "218": "Libya", "220": "Gambia", "221": "Senegal", "234": "Nigeria",
    "254": "Kenya", "255": "Tanzania", "256": "Uganda", "260": "Zambia",
    "263": "Zimbabwe", "351": "Portugal", "353": "Ireland", "354": "Iceland",
    "358": "Finland", "370": "Lithuania", "371": "Latvia", "372": "Estonia",
    "374": "Armenia", "375": "Belarus", "380": "Ukraine", "381": "Serbia",
    "385": "Croatia", "420": "Czech Republic", "421": "Slovakia",
    "855": "Cambodia", "856": "Laos", "880": "Bangladesh", "886": "Taiwan",
    "960": "Maldives", "966": "Saudi Arabia", "971": "United Arab Emirates",
    "972": "Israel", "977": "Nepal", "992": "Tajikistan",
    "993": "Turkmenistan", "994": "Azerbaijan", "995": "Georgia",
    "996": "Kyrgyzstan", "998": "Uzbekistan",
}

# 拨号码 -> ISO 3166-1 alpha-2 国家代码（用于 React Aria <select> 的 value 匹配）
PHONE_DIAL_TO_ISO = {
    "1": "US", "7": "RU", "20": "EG", "27": "ZA",
    "30": "GR", "31": "NL", "32": "BE", "33": "FR",
    "34": "ES", "36": "HU", "39": "IT", "40": "RO",
    "44": "GB", "45": "DK", "46": "SE", "47": "NO",
    "48": "PL", "49": "DE", "51": "PE", "52": "MX",
    "53": "CU", "54": "AR", "55": "BR", "56": "CL",
    "57": "CO", "58": "VE", "60": "MY", "61": "AU",
    "62": "ID", "63": "PH", "64": "NZ",
    "65": "SG", "66": "TH", "81": "JP", "82": "KR",
    "84": "VN", "86": "CN", "90": "TR", "91": "IN",
    "92": "PK", "93": "AF", "94": "LK", "95": "MM",
    "98": "IR", "212": "MA", "213": "DZ", "216": "TN",
    "218": "LY", "220": "GM", "221": "SN", "234": "NG",
    "254": "KE", "255": "TZ", "256": "UG", "260": "ZM",
    "263": "ZW", "351": "PT", "353": "IE", "354": "IS",
    "358": "FI", "370": "LT", "371": "LV", "372": "EE",
    "374": "AM", "375": "BY", "380": "UA", "381": "RS",
    "385": "HR", "420": "CZ", "421": "SK",
    "855": "KH", "856": "LA", "880": "BD", "886": "TW",
    "960": "MV", "966": "SA", "971": "AE",
    "972": "IL", "977": "NP", "992": "TJ",
    "993": "TM", "994": "AZ", "995": "GE",
    "996": "KG", "998": "UZ",
}

PHONE_INPUT_SELECTORS = [
    'input[type="tel"]',
    'input[name="phone"]',
    'input[name="phone_number"]',
    'input[name="phoneNumber"]',
    'input[id*="phone" i]',
    'input[placeholder*="phone" i]',
    'input[autocomplete="tel"]',
    'input[autocomplete="tel-national"]',
]

PHONE_SEND_SELECTORS = [
    'button[data-testid="continue-button"]',
    'button[data-testid*="send" i]',
    'button:has-text("Send code via SMS")',
    'button:has-text("Send code")',
    'button:has-text("Send via SMS")',
    'button:has-text("Send link via SMS")',
    'button:has-text("Send")',
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("发送")',
    'button:has-text("コードを送信")',
    'button:has-text("SMSで送信")',
    'button:has-text("送信")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
]

PHONE_VERIFY_SELECTORS = [
    'button:has-text("Verify")',
    'button:has-text("verify")',
    'button:has-text("Check")',
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("验证")',
    'button:has-text("确认")',
    'button:has-text("確認")',
    'button:has-text("認証")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
]

PHONE_CODE_TIMEOUT_SECONDS = 180
PHONE_CODE_TIMEOUT_SENTINEL = "SMS_CODE_TIMEOUT_180S"
PHONE_REJECTED_SENTINEL = "PHONE_REJECTED_RETRYABLE"
PHONE_ATTEMPTS_PER_COUNTRY = 10
PHONE_MAX_COUNTRIES = 2

# add-phone 页面会把虚拟号、VOIP 号或不可用号码提示成多语言短句。
# 这些错误不应终止整批任务，应立即换下一个号码继续尝试。
PHONE_RETRYABLE_REJECTION_RE = re.compile(
    r"virtual|voip|unsupported|not\s+support(?:ed)?|phone\s+number\s+is\s+not\s+supported|"
    r"cannot\s+use|can't\s+use|unable\s+to\s+use|try\s+another|use\s+another|"
    r"not\s+a\s+valid|invalid\s+phone|we\s+cannot\s+send|could(?:\s+not|n't)\s+send\s+a\s+text|"
    r"can't\s+send\s+a\s+text|unable\s+to\s+send\s+a\s+text|switched\s+to\s+whats\s*app|"
    r"failed\s+to\s+create\s+account|account_creation_failed|"
    r"sorry,\s*we\s+cannot\s+create\s+your\s+account|error\s+creating\s+your\s+account|"
    r"unable\s+to\s+create\s+(?:your\s+)?account|could(?:\s+not|n't)\s+create\s+(?:your\s+)?account|"
    "\uacc4\uc815\uc744\\s*\uc0dd\uc131\ud558\uc9c0\\s*\ubabb\ud588\uc2b5\ub2c8\ub2e4|"
    "\ub2e4\uc2dc\\s*\uc2dc\ub3c4\ud574\\s*\uc8fc\uc138\uc694|"
    r"too\s+many\s+phone\s+verification\s+requests|too\s+many\s+verification\s+requests|"
    r"continue\s+to\s+send[\s\S]{0,80}whats\s*app|"
    r"smspool\s+购号失败|purchase\s+failed|failed\s+to\s+get\s+phone|"
    r"no\s+numbers|no_numbers|no\s+number|"
    r"虚拟|不支持|无法使用|不能使用|换一个|更换|无效手机号|手机号无效|号码无效|"
    r"无法发送短信|发送短信失败|购号失败|获取手机号失败|无可用号码|切换到\s*whatsapp",
    re.I,
)

_PLAYWRIGHT_PAGEERROR_PATCH_REPLACEMENTS = (
    ('url: pageError.location.url,', 'url: pageError.location?.url || "",'),
    ('line: pageError.location.lineNumber,', 'line: pageError.location?.lineNumber || 0,'),
    ('column: pageError.location.columnNumber', 'column: pageError.location?.columnNumber || 0'),
)


def _playwright_core_bundle_path() -> Path:
    import playwright

    return Path(playwright.__file__).resolve().parent / "driver" / "package" / "lib" / "coreBundle.js"


def _patch_playwright_firefox_pageerror_location_bug(
    *,
    bundle_path: str | Path | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """修复 Camoufox/Firefox pageerror 无 location 时导致 Playwright driver 崩溃。"""
    log = log_fn or (lambda _message: None)
    path = Path(bundle_path) if bundle_path is not None else _playwright_core_bundle_path()
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        log(f"Playwright pageerror 热补丁读取失败: {exc}")
        return False

    patched = text
    for old, new in _PLAYWRIGHT_PAGEERROR_PATCH_REPLACEMENTS:
        patched = patched.replace(old, new)
    if patched == text:
        return False
    try:
        path.write_text(patched, encoding="utf-8")
        log("已应用 Playwright Firefox pageerror 热补丁")
        return True
    except Exception as exc:
        log(f"Playwright pageerror 热补丁写入失败: {exc}")
        return False


AUTH_TIMEOUT_TITLE_RE = re.compile(r"oops,\s*an\s*error\s*occurred|出错|發生錯誤|エラーが発生|問題が発生", re.I)
AUTH_TIMEOUT_DETAIL_RE = re.compile(
    r"operation\s+timed\s+out|route\s+error|405\s+method\s+not\s+allowed|failed\s+to\s+fetch|network\s+error|fetch\s+failed|タイムアウト|ネットワークエラー|取得に失敗",
    re.I,
)
AUTH_RETRY_TEXT_RE = re.compile(r"try\s+again|重试|重試|再試行|もう一度|やり直す", re.I)


def _is_auth_timeout_retry_text(text: str) -> bool:
    value = str(text or "")
    return bool(
        AUTH_RETRY_TEXT_RE.search(value)
        and (AUTH_TIMEOUT_TITLE_RE.search(value) or AUTH_TIMEOUT_DETAIL_RE.search(value))
    )


def _parse_phone_country_and_local(phone_number: str) -> tuple[str, str, str]:
    """从完整手机号解析出 (拨号码, 本地号码, 国家名)。

    例: +66959075673 -> ("66", "959075673", "Thailand")
    """
    num = re.sub(r"\D", "", str(phone_number or ""))
    for length in (3, 2, 1):
        if length > len(num):
            continue
        prefix = num[:length]
        if prefix in PHONE_COUNTRY_CODE_MAP:
            return prefix, num[length:], PHONE_COUNTRY_CODE_MAP[prefix]
    return "", num, ""


def _is_retryable_phone_rejection_text(text: str) -> bool:
    """识别 add-phone 页可通过换号恢复的拒号提示。"""
    return bool(PHONE_RETRYABLE_REJECTION_RE.search(str(text or "")))


def _is_retryable_initial_phone_fetch_error(text: str) -> bool:
    value = str(text or "").lower()
    if _is_retryable_phone_rejection_text(value):
        return True
    markers = (
        "smsbower",
        "herosms",
        "grizzlysms",
        "sms-verification-number",
        "sms verification number",
        "sms-activate",
        "get number",
        "get_number",
        "\u83b7\u53d6\u53f7\u7801",
        "\u83b7\u53d6\u624b\u673a\u53f7",
        "no_numbers",
        "no numbers",
        "connectionreseterror",
        "connection reset",
        "connection aborted",
        "remote disconnected",
        "read timed out",
        "read timeout",
        "connect timeout",
        "connection timeout",
        "request timeout",
        "timed out",
        "10054",
        "sms country plan exhausted",
    )
    return any(marker in value for marker in markers)


def _get_phone_country_select_state(page, dial_code: str, country_name: str) -> dict:
    try:
        iso_code = PHONE_DIAL_TO_ISO.get(str(dial_code or ""), "")
        result = page.evaluate(
            """
            ({ dialCode, countryName, isoCode }) => {
              const normalize = (value) => String(value || '')
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .replace(/\\s+/g, ' ')
                .trim()
                .toLowerCase();
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const trigger = document.querySelector('button[aria-haspopup="listbox"], .react-aria-Select button');
              const triggerText = trigger ? String(trigger.innerText || trigger.textContent || '').trim() : '';
              const triggerNormalized = normalize(triggerText);
              const select = Array.from(document.querySelectorAll('select')).find((sel) => sel.options && sel.options.length > 10);
              let selectValue = '';
              let selectText = '';
              if (select) {
                selectValue = String(select.value || '');
                const option = Array.from(select.options || []).find((opt) => String(opt.value || '') === selectValue) || null;
                selectText = String(option ? (option.text || option.label || option.textContent || '') : '').trim();
              }
              const dialPattern = String(dialCode || '').trim();
              const countryPattern = normalize(countryName);
              return {
                hasTrigger: Boolean(trigger && visible(trigger)),
                triggerText,
                selectValue,
                selectText,
                matchesDial: Boolean(dialPattern && (triggerText.includes(`(+${dialPattern})`) || selectText.includes(`(+${dialPattern})`) || selectValue === dialPattern)),
                matchesCountry: Boolean(countryPattern && (triggerNormalized.includes(countryPattern) || normalize(selectText).includes(countryPattern) || selectValue === isoCode)),
                matchesIso: Boolean(isoCode && selectValue === isoCode),
              };
            }
            """,
            {"dialCode": dial_code, "countryName": country_name, "isoCode": iso_code},
        )
    except Exception:
        result = {}
    return result if isinstance(result, dict) else {}


def _select_phone_country_ui(page, dial_code: str, country_name: str, log) -> bool:
    """在 add-phone 页面的国家下拉框中选择对应国家。

    OpenAI add-phone 页面使用 React Aria Select 组件，底层有一个隐藏的原生 <select>
    和一个可视的 button trigger + listbox 弹出层。
    """
    if not dial_code and not country_name:
        log("  无法识别国家码，跳过国家选择")
        return False

    iso_code = PHONE_DIAL_TO_ISO.get(dial_code, "")
    log(f"  目标国家: {country_name} (+{dial_code}) ISO={iso_code}")

    # 先检查当前下拉框是否已经是目标国家
    dial_pattern = f"(+{dial_code})"
    already = page.evaluate(
        """
        (dialPattern) => {
          const visible = (el) => {
            if (!el) return false;
            const s = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return s && s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
          };
          const all = Array.from(document.querySelectorAll('button, div, span, a, [role="button"], [role="combobox"], select'));
          for (const el of all) {
            if (!visible(el)) continue;
            const text = (el.innerText || el.textContent || '').trim();
            if (text.includes(dialPattern) && text.length < 80) return true;
          }
          return false;
        }
        """,
        dial_pattern,
    )
    if already:
        log(f"  国家已是目标值: (+{dial_code})")
        return True

    # ═══════════════════════════════════════════════════════════════════
    # 策略 1: 通过底层原生 <select> 直接设置值（最可靠）
    # React Aria Select 底层会有一个隐藏的 <select> 用于表单提交和无障碍。
    # 直接修改它的值并触发 change 事件可以同步 React 状态。
    # ═══════════════════════════════════════════════════════════════════
    native_selected = page.evaluate(
        """
        ({ isoCode, dialCode, countryName }) => {
          const selects = document.querySelectorAll('select');
          for (const sel of selects) {
            if (sel.options.length < 10) continue;  // 排除非国家的 select

            // 尝试多种匹配策略找到目标 option
            let targetValue = null;
            for (const opt of sel.options) {
              const v = (opt.value || '').trim();
              const t = (opt.text || opt.label || '').trim();
              // 匹配 ISO 代码 (如 "TH")
              if (isoCode && v === isoCode) { targetValue = v; break; }
              // 匹配拨号码 (如 value 包含 "66" 或 text 包含 "+66")
              if (t.includes('(+' + dialCode + ')')) { targetValue = v; break; }
              if (t.includes(countryName)) { targetValue = v; break; }
            }

            if (targetValue !== null) {
              // 使用 React 兼容的方式设置值
              const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value'
              )?.set;
              if (nativeInputValueSetter) {
                nativeInputValueSetter.call(sel, targetValue);
              } else {
                sel.value = targetValue;
              }
              sel.dispatchEvent(new Event('change', { bubbles: true }));
              sel.dispatchEvent(new Event('input', { bubbles: true }));
              return { ok: true, value: targetValue, method: 'native_setter' };
            }
          }
          return { ok: false };
        }
        """,
        {"isoCode": iso_code, "dialCode": dial_code, "countryName": country_name},
    )
    if native_selected and native_selected.get("ok"):
        log(f"  [OK] 通过原生 <select> 选择成功: value={native_selected.get('value')}")
        time.sleep(0.5)
        # 验证 UI 是否同步更新
        verify = page.evaluate(
            "(dp) => { const b = document.querySelector('button[aria-haspopup=\"listbox\"]'); return b ? (b.innerText || '').trim() : ''; }",
            dial_pattern,
        )
        if f"+{dial_code}" in (verify or ""):
            log(f"  [OK] UI 已同步: {verify}")
            return True
        log(f"  原生 select 已设置但 UI 未同步 ({verify})，尝试 UI 交互...")

    # ═══════════════════════════════════════════════════════════════════
    # 策略 2: 通过 React Aria 的 key 属性直接操作
    # ═══════════════════════════════════════════════════════════════════
    key_selected = page.evaluate(
        """
        ({ isoCode, dialCode, countryName }) => {
          // 找到 React Aria Select 的隐藏 <select> 并通过 selectOption 模拟
          const selects = document.querySelectorAll('select');
          for (const sel of selects) {
            if (sel.options.length < 10) continue;
            for (const opt of sel.options) {
              const v = (opt.value || '').trim();
              const t = (opt.text || opt.label || '').trim();
              if ((isoCode && v === isoCode) || t.includes('(+' + dialCode + ')') || t.includes(countryName)) {
                sel.value = v;
                // 触发 React 合成事件
                const ev = new Event('change', { bubbles: true });
                Object.defineProperty(ev, 'target', { writable: false, value: sel });
                sel.dispatchEvent(ev);
                return { ok: true, value: v, text: t };
              }
            }
          }
          return { ok: false };
        }
        """,
        {"isoCode": iso_code, "dialCode": dial_code, "countryName": country_name},
    )

    # ═══════════════════════════════════════════════════════════════════
    # 策略 3: 使用 Playwright 的 selectOption API（对原生 select 最可靠）
    # ═══════════════════════════════════════════════════════════════════
    try:
        select_el = page.query_selector("select")
        if select_el:
            # 尝试用 ISO 代码选择
            if iso_code:
                try:
                    select_el.select_option(value=iso_code)
                    log(f"  [OK] Playwright selectOption(value={iso_code}) 成功")
                    time.sleep(0.5)
                    return True
                except Exception:
                    pass
            # 尝试用 label 匹配（包含国家名或拨号码）
            try:
                # 获取所有 option 的 value 和 text，找到匹配的
                match_value = page.evaluate(
                    """
                    ({ dialCode, countryName }) => {
                      const sel = document.querySelector('select');
                      if (!sel) return '';
                      for (const opt of sel.options) {
                        const t = (opt.text || opt.label || '').trim();
                        const v = (opt.value || '').trim();
                        if (t.includes('(+' + dialCode + ')') || t.includes(countryName)) return v;
                      }
                      return '';
                    }
                    """,
                    {"dialCode": dial_code, "countryName": country_name},
                )
                if match_value:
                    select_el.select_option(value=match_value)
                    log(f"  [OK] Playwright selectOption(value={match_value}) 成功")
                    time.sleep(0.5)
                    return True
            except Exception as e:
                log(f"  selectOption label 匹配失败: {e}")
    except Exception as e:
        log(f"  Playwright selectOption 策略失败: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # 策略 4: 点击 trigger 按钮打开 listbox，然后在 listbox 中选择
    # ═══════════════════════════════════════════════════════════════════
    trigger = None
    for sel in [
        'button[aria-haspopup="listbox"]',
        '.react-aria-Select button',
        'button[class*="select" i]',
        'button[class*="country" i]',
    ]:
        trigger = page.query_selector(sel)
        if trigger:
            break

    if not trigger:
        trigger = page.evaluate(
            r"""
            () => {
              const pattern = /\(\+\d{1,4}\)/;
              const all = document.querySelectorAll('button, [role="button"], [role="combobox"]');
              for (const el of all) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                const text = (el.innerText || '').trim();
                if (pattern.test(text)) {
                  el.scrollIntoView({ block: 'center' });
                  el.click();
                  return true;
                }
              }
              return false;
            }
            """,
        )
        if not trigger:
            log("  ⚠️ 未找到国家选择器触发按钮")
            return False
        log("  已通过 JS 点击触发按钮")
    else:
        trigger.scroll_into_view_if_needed()
        trigger.click()
        log("  已点击国家选择器下拉框")

    time.sleep(0.8)

    # 等待 listbox 出现
    listbox = None
    for _ in range(10):
        listbox = page.query_selector('[role="listbox"]')
        if listbox:
            break
        time.sleep(0.3)

    if not listbox:
        log("  ⚠️ 下拉框 listbox 未出现")
        return False

    log("  listbox 已出现")

    # 在 listbox 中查找并点击目标 option
    option = None
    if iso_code:
        for attr in ["data-key", "data-value", "value", "id"]:
            # 尝试精确匹配和包含匹配
            option = page.query_selector(f'[role="option"][{attr}="{iso_code}"]')
            if not option:
                option = page.query_selector(f'[role="option"][{attr}*="{iso_code}"]')
            if option:
                log(f"  找到 option: [{attr} 含 {iso_code}]")
                break

    if not option:
        option_idx = page.evaluate(
            """
            ({ countryName, dialCode }) => {
              const options = document.querySelectorAll('[role="option"]');
              for (let i = 0; i < options.length; i++) {
                const text = (options[i].innerText || options[i].textContent || '').trim();
                if (text.includes(countryName) || text.includes('(+' + dialCode + ')') || text.includes('+' + dialCode)) {
                  return i;
                }
              }
              // 宽松匹配：只匹配拨号码数字
              for (let i = 0; i < options.length; i++) {
                const text = (options[i].innerText || options[i].textContent || '').trim();
                if (text.includes(dialCode)) {
                  return i;
                }
              }
              return -1;
            }
            """,
            {"countryName": country_name, "dialCode": dial_code},
        )
        if option_idx >= 0:
            options = page.query_selector_all('[role="option"]')
            if option_idx < len(options):
                option = options[option_idx]
                log(f"  找到 option: 文本匹配 index={option_idx}")

    if option:
        option.scroll_into_view_if_needed()
        option.click()
        time.sleep(0.5)
        new_text = page.evaluate(
            """() => {
              const btn = document.querySelector('button[aria-haspopup="listbox"]') ||
                          document.querySelector('.react-aria-Select button');
              return btn ? (btn.innerText || '').trim() : '';
            }""",
        )
        log(f"  选择后下拉框显示: {new_text}")
        if f"+{dial_code}" in (new_text or ""):
            log(f"  [OK] 国家选择成功: {new_text}")
            return True

    # 键盘 type-ahead 搜索
    log(f"  尝试键盘 type-ahead: {country_name}")
    page.keyboard.type(country_name, delay=80)
    time.sleep(0.8)

    # 按 Enter 确认选择
    page.keyboard.press("Enter")
    time.sleep(0.5)

    # 验证
    final_text = page.evaluate(
        """() => {
          const btn = document.querySelector('button[aria-haspopup="listbox"]') ||
                      document.querySelector('.react-aria-Select button');
          return btn ? (btn.innerText || '').trim() : '';
        }""",
    )
    if f"+{dial_code}" in (final_text or ""):
        log(f"  [OK] type-ahead 选择成功: {final_text}")
        return True

    log(f"  ⚠️ 下拉框已展开但未找到匹配国家: {country_name} (+{dial_code})")
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def _build_proxy_config(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config



def _is_local_proxy(proxy):
    """Return True if the proxy URL points to a local address (geoip will fail)."""
    if not proxy:
        return False
    from urllib.parse import urlparse as _up
    host = str(_up(proxy).hostname or "").strip().lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")

def _wait_for_url(page, substring: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if substring in page.url:
            return True
        time.sleep(1)
    return False


def _find_first_selector(page, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            node = page.query_selector(sel)
        except Exception:
            node = None
        if node:
            return sel
    return None


def _is_visible_password_registration_page(page) -> bool:
    try:
        result = page.evaluate(
            """
                () => {
                  const norm = (value) => String(value || '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toLowerCase();
                  const text = norm(document.body?.innerText || '');
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style
                      && style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && rect.width > 0
                      && rect.height > 0
                      && !el.disabled
                      && el.getAttribute('aria-disabled') !== 'true';
                  };
                  const inputs = Array.from(document.querySelectorAll('input, textarea'));
                  const hasPasswordInput = inputs.some((el) => {
                    if (!visible(el)) return false;
                    const hint = norm([
                      el.type,
                      el.name,
                      el.id,
                      el.placeholder,
                      el.getAttribute('aria-label'),
                      el.getAttribute('autocomplete'),
                    ].filter(Boolean).join(' '));
                    return hint.includes('password');
                  });
                  if (hasPasswordInput) return true;
                  return text.includes('create a password')
                    || text.includes('you\\u2019ll use this password')
                    || text.includes("you'll use this password");
                }
            """
        )
        return result is True
    except Exception:
        return False


def _is_visible_phone_first_sms_otp_page(page) -> bool:
    try:
        result = page.evaluate(
            """
                () => {
                  const norm = (value) => String(value || '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toLowerCase();
                  const text = norm(document.body?.innerText || '');
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style
                      && style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && rect.width > 0
                      && rect.height > 0
                      && !el.disabled
                      && el.getAttribute('aria-disabled') !== 'true';
                  };
                  const hasOtpText = [
                    'verification code',
                    'enter code',
                    'one-time code',
                    'security code',
                    'we sent',
                    'text message',
                    'sms',
                    'whatsapp',
                    'code'
                  ].some((token) => text.includes(token));
                  const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea'));
                  return inputs.some((el) => {
                    if (!visible(el)) return false;
                    const hint = norm([
                      el.type,
                      el.name,
                      el.id,
                      el.placeholder,
                      el.getAttribute('aria-label'),
                      el.getAttribute('autocomplete'),
                      el.getAttribute('inputmode'),
                    ].filter(Boolean).join(' '));
                    if (hint.includes('phone') || hint.includes('tel')) {
                      return false;
                    }
                    if (hint.includes('one-time-code') || hint.includes('otp')) {
                      return true;
                    }
                    if (hint.includes('code') || hint.includes('verification')) {
                      return true;
                    }
                    const maxLength = Number(el.getAttribute('maxlength') || 0);
                    const numeric = hint.includes('numeric') || hint.includes('number');
                    return hasOtpText && numeric && (!maxLength || maxLength <= 8);
                  });
                }
            """
        )
        return result is True
    except Exception:
        return False


def _wait_for_any_selector(page, selectors: list[str], timeout: int = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _find_first_selector(page, selectors)
        if found:
            return found
        time.sleep(0.5)
    return None


def _click_first(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    found = _wait_for_any_selector(page, selectors, timeout=timeout)
    if not found:
        return None
    try:
        page.click(found)
        return found
    except Exception:
        return None


def _click_first_no_wait(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    """Click a visible element without waiting for navigation.

    OpenAI's add-phone page sometimes leaves the submit XHR pending long enough
    that Playwright reports "Operation timed out" even though the click was
    delivered. This helper treats that as a click problem only after a
    no-wait click and a DOM fallback both fail.
    """
    found = _wait_for_any_selector(page, selectors, timeout=timeout)
    if not found:
        return None
    for kwargs in (
        {"timeout": 3000, "no_wait_after": True},
        {"timeout": 3000, "force": True, "no_wait_after": True},
    ):
        try:
            page.click(found, **kwargs)
            return found
        except Exception:
            pass
    try:
        clicked = bool(
            page.evaluate(
                """
                (selector) => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  let target = null;
                  try {
                    target = document.querySelector(selector);
                  } catch (_) {
                    const textMatch = selector.match(/:has-text\\(["'](.+?)["']\\)/);
                    const tag = String(selector.split(':')[0] || 'button').trim() || 'button';
                    const needle = textMatch ? textMatch[1].toLowerCase() : '';
                    target = Array.from(document.querySelectorAll(tag)).find((el) => {
                      const text = String(el.innerText || el.textContent || '').trim().toLowerCase();
                      return visible(el) && (!needle || text.includes(needle));
                    });
                  }
                  if (!target || !visible(target) || target.disabled) return false;
                  target.click();
                  return true;
                }
                """,
                found,
            )
        )
        return found if clicked else None
    except Exception:
        return None


def _phone_page_status(page) -> dict:
    try:
        result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
              const addPhoneForm = document.querySelector('form[action*="/add-phone" i]');
              const verificationForm = document.querySelector('form[action*="/phone-verification" i]');
              const codeInput = verificationForm?.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]');
              const addPhoneError = (() => {
                if (!addPhoneForm) return '';
                const selectors = [
                  '.react-aria-FieldError',
                  '[slot="errorMessage"]',
                  '[id$="-error"]',
                  '[data-invalid="true"] + *',
                  '[aria-invalid="true"] + *',
                  '[class*="error" i]'
                ];
                for (const selector of selectors) {
                  for (const el of Array.from(addPhoneForm.querySelectorAll(selector))) {
                    const msg = String(el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (msg) return msg;
                  }
                }
                return '';
              })();
              const verifyError = (() => {
                if (!verificationForm) return '';
                const selectors = [
                  '.react-aria-FieldError',
                  '[slot="errorMessage"]',
                  '[id$="-error"]',
                  '[data-invalid="true"] + *',
                  '[aria-invalid="true"] + *',
                  '[class*="error" i]',
                  '[role="alert"]'
                ];
                for (const selector of selectors) {
                  for (const el of Array.from(verificationForm.querySelectorAll(selector))) {
                    const msg = String(el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (msg) return msg;
                  }
                }
                return '';
              })();
              return {
                url: location.href,
                addPhoneReady: Boolean(addPhoneForm && visible(addPhoneForm.querySelector('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phoneNumber"]'))),
                phoneVerificationReady: Boolean(verificationForm && codeInput && visible(codeInput)),
                addPhoneError,
                verifyError,
                text,
              };
            }
            """
        )
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _auth_timeout_retry_page_state(page, *, path_patterns: list[str] | None = None) -> dict:
    try:
        result = page.evaluate(
            """
            (pathPatterns) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const pathname = String(location.pathname || '');
              if (Array.isArray(pathPatterns) && pathPatterns.length) {
                const matched = pathPatterns.some((raw) => {
                  try { return new RegExp(raw, 'i').test(pathname); } catch (_) { return false; }
                });
                if (!matched) return { retryPage: false, url: location.href, text: '' };
              }
              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
              const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'));
              const retryButton = document.querySelector('button[data-dd-action-name="Try again"]')
                || buttons.find((button) => {
                  const label = String([button.value, button.textContent, button.getAttribute?.('aria-label'), button.getAttribute?.('title')].filter(Boolean).join(' '));
                  return visible(button) && /try\\s+again|重试|重試|再試行|もう一度|やり直す/i.test(label);
                });
              return {
                retryPage: Boolean(retryButton && /try\\s+again|重试|重試/i.test(text) && (/oops,?\\s*an\\s*error\\s*occurred|operation\\s+timed\\s+out|route\\s+error|405\\s+method\\s+not\\s+allowed|failed\\s+to\\s+fetch|network\\s+error/i.test(text))),
                retryEnabled: Boolean(retryButton && visible(retryButton) && !retryButton.disabled && retryButton.getAttribute('aria-disabled') !== 'true'),
                url: location.href,
                text,
              };
            }
            """,
            path_patterns or [],
        )
        if isinstance(result, dict):
            result["retryPage"] = bool(result.get("retryPage") or _is_auth_timeout_retry_text(str(result.get("text") or "")))
            return result
    except Exception:
        pass
    return {"retryPage": False, "retryEnabled": False, "url": str(page.url or ""), "text": ""}


def _recover_auth_timeout_retry_page(
    page,
    log,
    *,
    path_patterns: list[str] | None = None,
    max_clicks: int = 3,
    wait_after_click: float = 3.0,
) -> dict:
    last_state = {}
    for attempt in range(1, max_clicks + 1):
        state = _auth_timeout_retry_page_state(page, path_patterns=path_patterns)
        last_state = state
        if not state.get("retryPage"):
            return {"recovered": attempt > 1, "clicks": attempt - 1, "url": str(state.get("url") or page.url)}
        if not state.get("retryEnabled"):
            time.sleep(0.5)
            continue
        log(f"  检测到 OpenAI auth 超时重试页，点击 Try again ({attempt}/{max_clicks})")
        clicked = _click_first_no_wait(
            page,
            [
                'button[data-dd-action-name="Try again"]',
                'button:has-text("Try again")',
                'button:has-text("try again")',
                'button:has-text("重试")',
                'button:has-text("重試")',
                'button:has-text("再試行")',
                'button:has-text("もう一度")',
                'button:has-text("やり直す")',
            ],
            timeout=2,
        )
        if not clicked:
            try:
                clicked = "dom" if page.evaluate(
                    """
                    () => {
                      const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                      };
                      const direct = document.querySelector('button[data-dd-action-name="Try again"]');
                      const target = direct || Array.from(document.querySelectorAll('button, [role="button"]')).find((el) => {
                        const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        return visible(el) && /try\\s+again|重试|重試/i.test(text);
                      });
                      if (!target) return false;
                      target.click();
                      return true;
                    }
                    """
                ) else ""
            except Exception:
                clicked = ""
        if not clicked:
            break
        time.sleep(wait_after_click)
        state = _auth_timeout_retry_page_state(page, path_patterns=path_patterns)
        last_state = state
        if not state.get("retryPage"):
            return {"recovered": True, "clicks": attempt, "url": str(state.get("url") or page.url)}
    return {
        "recovered": False,
        "clicks": max_clicks,
        "url": str(last_state.get("url") or page.url),
        "text": str(last_state.get("text") or ""),
    }


def _wait_for_phone_verification_ready(page, *, timeout: int = 25) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = _phone_page_status(page)
        if last.get("phoneVerificationReady"):
            return last
        if last.get("addPhoneError"):
            return last
        time.sleep(0.25)
    return last


def _submit_add_phone_dom(
    page,
    *,
    phone_number: str,
    dial_code: str,
    local_number: str,
    country_name: str,
    log,
) -> dict:
    """Submit OpenAI add-phone with GuJumpgate-style DOM state sync."""
    e164 = "+" + str(phone_number or "").lstrip("+").strip()
    national = str(local_number or "").strip() or e164
    iso_code = PHONE_DIAL_TO_ISO.get(str(dial_code or ""), "")
    payload = {
        "phoneNumber": e164,
        "nationalPhoneNumber": national,
        "dialCode": str(dial_code or ""),
        "countryLabel": str(country_name or ""),
        "isoCode": iso_code,
    }
    try:
        result = page.evaluate(
            """
            async (payload) => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const dispatchInputEvents = (el) => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              };
              const setNativeValue = (el, value) => {
                const proto = el instanceof HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                dispatchInputEvents(el);
              };
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const form = document.querySelector('form[action*="/add-phone" i]');
              if (!form) return { ok: false, reason: 'missing_add_phone_form', url: location.href };

              const channelInput = form.querySelector('input[name="channel"]');
              const radioEntries = Array.from(form.querySelectorAll('input[type="radio"]')).map((input) => {
                const label = input.closest('label');
                const root = label || input.closest('[role="radio"], [data-state], [class*="option"]') || input;
                const text = normalize([input.value, label?.textContent, root?.textContent, root?.getAttribute?.('aria-label')].filter(Boolean).join(' '));
                const channel = /^(sms)$/i.test(input.value || '') || /\\b(sms|text message)\\b/i.test(text)
                  ? 'sms'
                  : (/whats\\s*app/i.test(text) || /^(whatsapp)$/i.test(input.value || '') ? 'whatsapp' : '');
                return { input, label, root, channel, text };
              }).filter((entry) => entry.channel || entry.text);
              const sms = radioEntries.find((entry) => entry.channel === 'sms');
              let channelMethod = '';
              if (sms) {
                const target = sms.label || sms.root || sms.input;
                target?.click?.();
                await sleep(120);
                radioEntries.forEach((entry) => {
                  entry.input.checked = entry.input === sms.input;
                  entry.input.dispatchEvent(new Event('input', { bubbles: true }));
                  entry.input.dispatchEvent(new Event('change', { bubbles: true }));
                  entry.label?.setAttribute?.('data-state', entry.input === sms.input ? 'on' : 'off');
                  entry.root?.setAttribute?.('data-state', entry.input === sms.input ? 'on' : 'off');
                });
                if (channelInput) {
                  channelInput.value = 'sms';
                  dispatchInputEvents(channelInput);
                }
                channelMethod = 'sms_radio';
              }

              // 图中短信/WhatsApp 是分段控件时，页面未必有 radio；按可见文案再点一次短信模式。
              const modeCandidates = Array.from(form.querySelectorAll('button, [role="button"], label, [role="radio"], div, span'));
              const smsMode = modeCandidates.find((el) => {
                if (!visible(el)) return false;
                const text = normalize([el.textContent, el.getAttribute?.('aria-label'), el.getAttribute?.('title')].filter(Boolean).join(' '));
                if (!text || /whats\\s*app/i.test(text)) return false;
                return /\\b(text\\s*message|sms)\\b/i.test(text) || /短信/.test(text);
              });
              if (smsMode) {
                smsMode.click();
                await sleep(120);
                if (channelInput) {
                  channelInput.value = 'sms';
                  dispatchInputEvents(channelInput);
                }
                channelMethod = channelMethod || 'sms_segment';
              }

              const phoneInput = form.querySelector('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"]');
              if (!phoneInput) return { ok: false, reason: 'missing_phone_input', url: location.href };
              phoneInput.focus();
              setNativeValue(phoneInput, payload.nationalPhoneNumber);
              phoneInput.dispatchEvent(new Event('blur', { bubbles: true }));

              const hidden = form.querySelector('input[name="phoneNumber"]');
              if (hidden) {
                setNativeValue(hidden, payload.phoneNumber);
              }
              await sleep(120);
              document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
              document.dispatchEvent(new KeyboardEvent('keyup', { key: 'Escape', bubbles: true }));
              await sleep(80);

              const buttons = Array.from(form.querySelectorAll('button[type="submit"], input[type="submit"], button'));
              const isSubmitCandidate = (button) => {
                if (!visible(button) || button.disabled || button.getAttribute('aria-disabled') === 'true') return false;
                const label = normalize([button.value, button.textContent, button.getAttribute('aria-label'), button.getAttribute('title')].filter(Boolean).join(' '));
                if (button.getAttribute('aria-haspopup') === 'listbox' || button.getAttribute('role') === 'combobox') return false;
                if (/whats\\s*app|text\\s*message|\\bsms\\b|短信/i.test(label)) return false;
                if (/\\(\\+\\d+\\)/.test(label)) return false;
                return button.type === 'submit'
                  || /continue|send\\s*code|send|next|verify|继续|发送|下一步|次へ|続ける|続行/i.test(label);
              };
              const submit = buttons.find(isSubmitCandidate);
              if (!submit) return { ok: false, reason: 'missing_submit_button', url: location.href };
              submit.click();
              const countrySelect = Array.from(document.querySelectorAll('select')).find((sel) => sel.options && sel.options.length > 10);
              return {
                ok: true,
                url: location.href,
                selectedCountry: countrySelect ? countrySelect.value : '',
                channel: channelInput ? channelInput.value : (channelMethod || (sms ? 'sms' : '')),
                visibleValue: phoneInput.value || '',
                hiddenValue: hidden ? hidden.value : '',
              };
            }
            """,
            payload,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"dom_exception: {exc}", "url": str(page.url or "")}

    if not isinstance(result, dict):
        result = {"ok": False, "reason": "dom_result_invalid", "url": str(page.url or "")}
    if result.get("ok"):
        log(
            "  add-phone DOM 提交: "
            f"country={result.get('selectedCountry') or iso_code or '-'} "
            f"channel={result.get('channel') or '-'} "
            f"hidden={'yes' if result.get('hiddenValue') else 'no'}"
        )
    return result


def _submit_phone_otp_dom(page, code: str, log) -> dict:
    otp = str(code or "").strip()
    if not otp:
        return {"ok": False, "status": 400, "url": str(page.url or ""), "text": "empty phone otp"}
    try:
        result = page.evaluate(
            """
            async (code) => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const setNativeValue = (el, value) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              };
              const form = document.querySelector('form[action*="/phone-verification" i]');
              if (!form) return { ok: false, reason: 'missing_phone_verification_form', url: location.href };
              const input = form.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]');
              if (!input || !visible(input)) return { ok: false, reason: 'missing_code_input', url: location.href };
              input.focus();
              setNativeValue(input, code);
              input.dispatchEvent(new Event('blur', { bubbles: true }));
              await sleep(120);
              const buttons = Array.from(form.querySelectorAll('button[type="submit"], input[type="submit"], button'));
              const submit = buttons.find((button) => {
                if (!visible(button) || button.disabled || button.getAttribute('aria-disabled') === 'true') return false;
                const text = String([button.value, button.textContent, button.getAttribute('aria-label')].filter(Boolean).join(' '));
                return !/resend/i.test(text);
              }) || buttons.find((button) => visible(button));
              if (!submit) return { ok: false, reason: 'missing_submit_button', url: location.href };
              submit.click();
              return { ok: true, url: location.href, value: input.value || '' };
            }
            """,
            otp,
        )
    except Exception as exc:
        return {"ok": False, "status": 0, "url": str(page.url or ""), "text": f"phone otp dom exception: {exc}"}
    if not isinstance(result, dict) or not result.get("ok"):
        return {
            "ok": False,
            "status": 0,
            "url": str((result or {}).get("url") or page.url),
            "text": str((result or {}).get("reason") or "phone otp dom submit failed"),
        }
    log("  phone-otp DOM 已填写并提交")
    deadline = time.time() + 25
    last_url = str(page.url or "")
    while time.time() < deadline:
        status = _phone_page_status(page)
        current_url = str(status.get("url") or page.url or "")
        last_url = current_url or last_url
        if status.get("verifyError"):
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": str(status.get("verifyError") or "")}
        if "phone-verification" not in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if any(key in current_url for key in ("code=", "consent", "sign-in-with-chatgpt", "workspace", "organization")):
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        time.sleep(0.4)
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "phone otp submit stayed on verification page"}


def _is_login_password_url(url: str) -> bool:
    return bool(re.search(r"(?:auth|accounts)\.openai\.com/.*log-?in/password", str(url or ""), flags=re.I))


def _build_manual_flow_state(page_type: str, current_url: str) -> dict:
    state = _extract_flow_state(None, current_url)
    state["page_type"] = page_type
    state["current_url"] = current_url
    return state


def _get_visible_page_text(page) -> str:
    try:
        return str(page.evaluate("() => document.body?.innerText || ''") or "")
    except Exception:
        return ""


def _has_signup_registration_choice(page) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if _find_first_selector(page, SIGNUP_RECOVERY_SELECTORS):
        return True
    text = _get_visible_page_text(page)
    return bool(re.search(r"sign\s*up|register|create\s*account|还没有帐户|还没有账户|請註冊|请注册|去注册|注册", text, flags=re.I))


def _click_passwordless_login_if_available(page, log, *, context: str) -> bool:
    selector = _click_first(page, PASSWORDLESS_LOGIN_SELECTORS, timeout=1)
    if selector:
        log(f"{context} 已选择一次性验证码登录: {selector}")
        time.sleep(1)
        return True
    try:
        clicked = bool(
            page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll('button, [role="button"], a'));
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const target = nodes.find((el) => {
                    const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return visible(el) && /使用一次性验证码登录|使用一次性驗證碼登入|one-time code|one time code|passwordless|ワンタイムコード|一回限りのコード|認証コード/i.test(text);
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """
            )
        )
    except Exception:
        clicked = False
    if clicked:
        log(f"{context} 已选择一次性验证码登录")
        time.sleep(1)
    return clicked


def _get_page_oauth_url(page) -> str:
    try:
        return str(
            page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const anchors = Array.from(document.querySelectorAll('a[href*="/api/oauth/authorize"], a[href*="/oauth/authorize"]'));
                  const anchor = anchors.find((el) => visible(el));
                  return anchor ? String(anchor.href || anchor.getAttribute('href') || '') : '';
                }
                """
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _oauth_url_matches_state(url: str, state: str) -> bool:
    if not url or not state:
        return False
    return f"state={state}" in url or f"state%3D{state}" in url


def _extract_auth_error_text(page) -> str:
    selectors = [
        "text=Failed to create account",
        "text=Sorry, we cannot create your account",
        "text=Please try again",
        "text=Invalid code",
        "text=Enter a valid age to continue",
        "text=doesn't look right",
        "[role='alert']",
        ".error, [class*='error'], [class*='Error']",
    ]
    for selector in selectors:
        try:
            text = str(page.locator(selector).first.text_content(timeout=350) or "").strip()
        except Exception:
            text = ""
        if text and "oai_log" not in text and "SSR_HTML" not in text:
            return text
    return ""


EMAIL_OTP_INVALID_MARKERS = (
    "incorrect code",
    "invalid code",
    "wrong code",
    "expired code",
    "code is incorrect",
    "invalid otp",
    "incorrect otp",
    "invalid verification code",
    "incorrect verification code",
    "verification code is incorrect",
    "code didn't work",
    "code did not work",
    "\u9a8c\u8bc1\u7801\u4e0d\u6b63\u786e",
    "\u9a8c\u8bc1\u7801\u65e0\u6548",
)


def _is_invalid_email_otp_text(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    return bool(normalized and any(marker in normalized for marker in EMAIL_OTP_INVALID_MARKERS))


def _extract_email_otp_error_text(page) -> str:
    selectors = [
        "text=Incorrect code",
        "text=Invalid code",
        "text=Wrong code",
        "text=Expired code",
        "[role='alert']",
        "[aria-live='assertive']",
        "[aria-live='polite']",
        ".react-aria-FieldError",
        "[slot='errorMessage']",
        "[id$='-error']",
        "[class*='error' i]",
    ]
    for selector in selectors:
        try:
            text = str(page.locator(selector).first.text_content(timeout=350) or "").strip()
        except Exception:
            text = ""
        if text and _is_invalid_email_otp_text(text):
            return text
    text = _get_visible_page_text(page)
    if _is_invalid_email_otp_text(text):
        return text[:300]
    return ""


def _refresh_otp_callback_baseline(otp_callback, log) -> None:
    refresh = getattr(otp_callback, "refresh_before_ids", None)
    if not callable(refresh):
        return
    try:
        before_ids = refresh()
        try:
            count = len(before_ids or [])
        except Exception:
            count = 0
        log(f"Email OTP mailbox baseline refreshed before resend: before_ids={count}")
    except Exception as exc:
        log(f"Email OTP mailbox baseline refresh failed before resend: {exc}")


def _resend_browser_email_otp(page, otp_callback, log) -> bool:
    _refresh_otp_callback_baseline(otp_callback, log)
    selectors = [
        'button:has-text("Resend email")',
        'button:has-text("Resend code")',
        'button:has-text("Resend")',
        'a:has-text("Resend email")',
        'a:has-text("Resend code")',
        'a:has-text("Resend")',
        'button[data-testid="resend-link"]',
        'button:has-text("\u91cd\u65b0\u53d1\u9001")',
        'button:has-text("\u518d\u53d1\u9001")',
        'a:has-text("\u91cd\u65b0\u53d1\u9001")',
        'a:has-text("\u518d\u53d1\u9001")',
    ]
    clicked = _click_first_no_wait(page, selectors, timeout=5)
    if clicked:
        log(f"Email OTP resend clicked: {clicked}")
        time.sleep(1.5)
        return True

    referer = str(getattr(page, "url", "") or f"{OPENAI_AUTH}/email-verification")
    try:
        result = _send_browser_email_otp(page, referer=referer)
    except Exception as exc:
        log(f"Email OTP resend fallback request failed: {exc}")
        return False
    status = int((result or {}).get("status") or 0)
    if (result or {}).get("ok") or status in (200, 201, 204, 302):
        log(f"Email OTP resend fallback request ok: status={status}")
        time.sleep(1.5)
        return True
    log(f"Email OTP resend failed: status={status} text={str((result or {}).get('text') or '')[:160]}")
    return False


def _fill_input_like_user(page, selector: str, value: str) -> bool:
    try:
        locator = page.locator(selector)
        locator = getattr(locator, "first", locator)
        locator.wait_for(state="visible", timeout=2000)
        current = str(locator.input_value() or "").strip()
        if current == str(value).strip():
            return True
        locator.click(timeout=1500)
        _browser_pause(page)
        try:
            locator.fill("")
        except Exception:
            pass
        _browser_pause(page, headed=False)
        try:
            locator.type(value, delay=random.randint(35, 85))
        except Exception:
            try:
                page.fill(selector, value)
            except Exception:
                return False
        final_value = str(locator.input_value() or "").strip()
        if final_value == str(value):
            return True
        # For phone numbers: compare digits only (page may reformat with spaces/dashes)
        import re as _re
        final_digits = _re.sub(r"\D", "", final_value)
        expected_digits = _re.sub(r"\D", "", str(value))
        if final_digits == expected_digits:
            return True
        # Some phone widgets render the selected country code inside the input.
        if expected_digits and final_digits.endswith(expected_digits):
            return True
    except Exception:
        pass

    try:
        ok = page.evaluate(
            """
            ({ selector, value }) => {
              const input = document.querySelector(selector);
              if (!input) return false;
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
              if (!setter) return false;
              setter.call(input, value);
              input.dispatchEvent(new Event('input', { bubbles: true }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              return String(input.value || '') === String(value || '');
            }
            """,
            {"selector": selector, "value": value},
        )
        return bool(ok)
    except Exception:
        return False


def _phone_input_matches_expected(page, selector: str, dial_code: str, local_number: str) -> bool:
    try:
        locator = page.locator(selector)
        locator = getattr(locator, "first", locator)
        actual = str(locator.input_value() or "").strip()
    except Exception:
        return False
    actual_digits = re.sub(r"\D", "", actual)
    expected_digits = re.sub(r"\D", "", str(local_number or ""))
    dial_digits = re.sub(r"\D", "", str(dial_code or ""))
    if not expected_digits:
        return False
    if actual_digits == expected_digits:
        return True
    return bool(dial_digits and actual_digits == f"{dial_digits}{expected_digits}")


def _submit_form_with_fallback(page, input_selector: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                (selector) => {
                  const input = document.querySelector(selector);
                  if (!input) return false;
                  const form = input.form || input.closest?.('form');
                  if (form?.requestSubmit) {
                    form.requestSubmit();
                    return true;
                  }
                  if (form?.submit) {
                    form.submit();
                    return true;
                  }
                  input.focus?.();
                  for (const type of ['keydown', 'keypress', 'keyup']) {
                    input.dispatchEvent(new KeyboardEvent(type, {
                      key: 'Enter',
                      code: 'Enter',
                      bubbles: true,
                      cancelable: true,
                    }));
                  }
                  return true;
                }
                """,
                input_selector,
            )
        )
    except Exception:
        return False


def _sync_hidden_birthday_input(page, birthdate: str, log) -> bool:
    try:
        synced = bool(
            page.evaluate(
                """
                (value) => {
                  const input = document.querySelector("input[name='birthday']");
                  if (!input) return false;
                  input.value = value;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  return String(input.value || '') === String(value || '');
                }
                """,
                birthdate,
            )
        )
    except Exception:
        synced = False
    if synced:
        log(f"about_you 已同步隐藏 birthday: {birthdate}")
    return synced


def _collect_visible_text_inputs(page) -> list[dict]:
    try:
        inputs = page.evaluate(
            """
            () => {
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const nodes = Array.from(document.querySelectorAll("input:not([type='hidden']):not([disabled]):not([readonly])"));
              const visible = nodes.filter((el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style
                  && style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && rect.width > 0
                  && rect.height > 0;
              });
              return visible.map((el, visibleIndex) => {
                const explicitLabels = Array.from(document.querySelectorAll('label'))
                  .filter((label) => String(label.getAttribute('for') || '') === String(el.id || ''))
                  .map((label) => normalize(label.textContent));
                const wrappedLabel = normalize(el.closest('label')?.textContent || '');
                const ariaLabel = normalize(el.getAttribute('aria-label'));
                const labelledByText = normalize(
                  String(el.getAttribute('aria-labelledby') || '')
                    .split(/\\s+/)
                    .filter(Boolean)
                    .map((id) => normalize(document.getElementById(id)?.textContent || ''))
                    .join(' ')
                );
                const parentText = normalize(el.parentElement?.textContent || '');
                return {
                  visibleIndex,
                  type: normalize(el.getAttribute('type') || el.type || ''),
                  name: normalize(el.getAttribute('name') || ''),
                  id: normalize(el.id || ''),
                  placeholder: normalize(el.getAttribute('placeholder') || ''),
                  ariaLabel,
                  labels: explicitLabels.filter(Boolean),
                  wrappedLabel,
                  labelledByText,
                  parentText,
                };
              });
            }
            """
        ) or []
    except Exception:
        inputs = []
    return [item for item in inputs if isinstance(item, dict)]


def _about_you_input_hints(entry: dict) -> str:
    parts: list[str] = []
    labels = entry.get("labels") or []
    if isinstance(labels, list):
        parts.extend(str(item or "") for item in labels)
    parts.extend(
        [
            str(entry.get("wrappedLabel") or ""),
            str(entry.get("labelledByText") or ""),
            str(entry.get("ariaLabel") or ""),
            str(entry.get("placeholder") or ""),
            str(entry.get("name") or ""),
            str(entry.get("id") or ""),
            str(entry.get("parentText") or ""),
        ]
    )
    return " ".join(part for part in parts if part).strip().lower()


def _pick_best_about_you_input(entries: list[dict], field: str, exclude_visible_indices: set[int] | None = None) -> dict | None:
    exclude = {int(value) for value in (exclude_visible_indices or set())}
    best_entry = None
    best_score = float("-inf")
    for entry in entries:
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            continue
        if visible_index in exclude:
            continue
        hints = _about_you_input_hints(entry)
        if not hints:
            continue

        score = 0
        if field == "name":
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet", "vollständiger name", "nome completo")):
                score += 10
            if any(token in hints for token in (" name ", "name", "autocomplete=name", "nombre", "nom", "nome")):
                score += 3
            if any(token in hints for token in ("age", "年龄", "edad", "âge", "alter", "idade", "birthday", "birth", "date of birth", "出生", "生日")):
                score -= 8
        elif field == "age":
            if any(token in hints for token in ("age", "年龄", "how old", "edad", "âge", "alter", "idade", "나이")):
                score += 10
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet")):
                score -= 10
            if "name" in hints and "age" not in hints and "年龄" not in hints and "edad" not in hints:
                score -= 6
            if any(token in hints for token in ("birthday", "birth", "date of birth", "出生", "生日", "fecha de nacimiento", "nascimento")):
                score -= 3
        else:
            continue

        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score > 0:
        return best_entry

    if field == "age" and len(entries) == 2:
        ordered = []
        for entry in entries:
            try:
                visible_index = int(entry.get("visibleIndex"))
            except Exception:
                continue
            if visible_index not in exclude:
                ordered.append(entry)
        if len(ordered) == 1:
            return ordered[0]
        if len(ordered) == 2:
            return ordered[1]
    return None


def _derive_registration_state_from_page(page) -> dict:
    current_url = str(page.url or "")
    if _find_first_selector(page, PASSWORD_INPUT_SELECTORS) or _is_visible_password_registration_page(page):
        page_type = "login_password" if _is_login_password_url(current_url) else "create_account_password"
        return _build_manual_flow_state(page_type, current_url)

    state = _extract_flow_state(None, current_url)
    if state.get("page_type"):
        return state

    otp_selector = _find_first_selector(page, OTP_INPUT_SELECTORS)
    if otp_selector and "password" not in otp_selector:
        return _build_manual_flow_state("email_otp_verification", current_url)

    try:
        about_visible = bool(
            page.evaluate(
                """
                () => {
                  const inputs = Array.from(document.querySelectorAll("input:not([type='hidden'])"));
                  const text = String(document.body?.innerText || '').toLowerCase();
                  const hasName = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('name') || hint.includes('姓名') || hint.includes('全名');
                  });
                  const hasAgeOrBirth = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('age') || hint.includes('birth') || hint.includes('birthday') || hint.includes('年龄') || hint.includes('生日');
                  });
                  return (hasName && hasAgeOrBirth) || text.includes('about you');
                }
                """
            )
        )
    except Exception:
        about_visible = False
    if about_visible:
        return _build_manual_flow_state("about_you", current_url)

    return state


def _refresh_registration_state_from_page_if_password_visible(page, state: dict, log=None) -> dict:
    current = state if isinstance(state, dict) else {}
    try:
        page_state = _derive_registration_state_from_page(page)
    except Exception:
        return current
    if _is_password_registration(page_state) or str(page_state.get("page_type") or "") == "login_password":
        if str(current.get("page_type") or "") != str(page_state.get("page_type") or "") and callable(log):
            log(
                "Phone-first signup: visible password page overrides stale "
                f"{current.get('page_type') or '-'} state"
            )
        return page_state
    return current


def _wait_for_phone_first_otp_or_password_state(page, state: dict, log=None, timeout: float = 12.0) -> dict:
    current = state if isinstance(state, dict) else {}
    deadline = time.time() + max(float(timeout or 0), 0.0)
    while True:
        try:
            page_state = _derive_registration_state_from_page(page)
        except Exception:
            page_state = {}
        if _is_password_registration(page_state) or str(page_state.get("page_type") or "") == "login_password":
            if callable(log):
                log(
                    "Phone-first signup: password page appeared before SMS wait; "
                    f"overriding {current.get('page_type') or '-'} state"
                )
            return page_state
        if str(page_state.get("page_type") or "") == "phone_otp_verification" and _is_visible_phone_first_sms_otp_page(page):
            return page_state
        if _is_email_otp(page_state) and _is_visible_phone_first_sms_otp_page(page):
            return page_state
        if not _is_email_otp(page_state) and page_state.get("page_type"):
            return page_state
        if time.time() >= deadline:
            if callable(log):
                log(
                    "Phone-first signup: SMS OTP page not confirmed yet; "
                    "waiting for page transition"
                )
            return _build_manual_flow_state("pending_transition", str(getattr(page, "url", "") or ""))
        time.sleep(0.5)


def _recover_signup_password_page(page, log) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if not _has_signup_registration_choice(page):
        return False
    selector = _click_first(page, SIGNUP_RECOVERY_SELECTORS, timeout=2)
    if not selector:
        return False
    log(f"密码页落到登录态，尝试点击注册入口恢复: {selector}")
    time.sleep(1.2)
    return True


def _wait_for_signup_entry_transition(
    page,
    log,
    timeout: int = 20,
    *,
    allow_chatgpt_home: bool = True,
    click_passwordless_login: bool = True,
) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if click_passwordless_login and _click_passwordless_login_if_available(page, log, context="邮箱页提交后"):
            time.sleep(0.5)
            continue
        state = _derive_registration_state_from_page(page)
        page_type = state.get("page_type")
        if page_type == "chatgpt_home" and not allow_chatgpt_home:
            error_text = _extract_auth_error_text(page)
            if error_text:
                raise RuntimeError(f"phone identity submit failed: {error_text}")
            time.sleep(0.5)
            continue
        if page_type in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "phone_otp_verification",
            "phone_entry",
            "about_you",
            "add_phone",
            "chatgpt_home",
            "oauth_callback",
        }:
            if state.get("page_type") == "login_password" and _recover_signup_password_page(page, log):
                return _derive_registration_state_from_page(page)
            return state
        error_text = _extract_auth_error_text(page)
        if error_text:
            raise RuntimeError(f"邮箱页提交失败: {error_text}")
        time.sleep(0.25)
    raise RuntimeError("邮箱页提交后未进入密码/验证码页面")


def _start_browser_signup_via_page(page, email: str, log) -> dict:
    for entry_url in (PLATFORM_LOGIN_ENTRY, f"{OPENAI_AUTH}/log-in"):
        try:
            log(f"打开 OpenAI 注册入口: {entry_url}")
            _goto_with_retry(page, entry_url, wait_until="domcontentloaded", timeout=30000, log=log)
        except Exception as exc:
            log(f"注册入口访问失败: {entry_url} -> {exc}")
            continue

        initial_state = _derive_registration_state_from_page(page)
        if initial_state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
        }:
            return initial_state

        email_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=12)
        if not email_selector:
            continue
        if not _fill_input_like_user(page, email_selector, email):
            raise RuntimeError("邮箱页填写失败")
        log(f"邮箱页输入框: {email_selector}")

        inline_state = _derive_registration_state_from_page(page)
        if inline_state.get("page_type") in {"create_account_password", "login_password"}:
            if inline_state.get("page_type") == "login_password" and _recover_signup_password_page(page, log):
                return _derive_registration_state_from_page(page)
            return inline_state

        submit_selector = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
        if submit_selector:
            log(f"邮箱页已点击继续按钮: {submit_selector}")
        elif _submit_form_with_fallback(page, email_selector):
            log("邮箱页未找到可点击 Continue，已使用表单 fallback 提交")
        else:
            raise RuntimeError("邮箱页未找到 Continue 按钮")

        return _wait_for_signup_entry_transition(page, log)

    raise RuntimeError("未找到 OpenAI 注册入口邮箱输入框")


def _start_browser_signup_via_authorize(page, email: str, device_id: str, log) -> dict:
    log("访问 ChatGPT 首页...")
    _goto_with_retry(page, f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=30000, log=log)

    log("获取 CSRF token...")
    csrf_token = _get_browser_csrf_token(page)
    if not csrf_token:
        raise RuntimeError("获取 CSRF token 失败")

    log(f"提交邮箱: {email}")
    authorize_url = _start_browser_signin(page, email, device_id, csrf_token)
    if not authorize_url:
        raise RuntimeError("提交邮箱失败，未获取 authorize URL")

    final_url = _browser_authorize(page, authorize_url, log)
    if not final_url:
        raise RuntimeError("访问 authorize URL 失败")
    return _derive_registration_state_from_page(page)


def _find_visible_phone_input_selector(page) -> str:
    for selector in PHONE_INPUT_SELECTORS:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=800)
            return selector
        except Exception:
            continue
    return ""


def _click_phone_entry_if_available(page, log) -> bool:
    try:
        clicked = bool(
            page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll('button, [role="button"], a'));
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const target = nodes.find((el) => {
                    const text = String(el.innerText || el.textContent || el.getAttribute('aria-label') || '')
                      .replace(/\\s+/g, ' ')
                      .trim()
                      .toLowerCase();
                    return visible(el) && /(phone|mobile|sms|text message|\\u624b\\u673a|\\u624b\\u673a\\u53f7|\\u7535\\u8bdd|\\u96fb\\u8a71|\\u96fb\\u8a71\\u756a\\u53f7)/i.test(text);
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """
            )
        )
    except Exception:
        clicked = False
    if clicked:
        log("Phone-first signup: switched to phone entry")
        time.sleep(1)
    return clicked


def _sync_generic_phone_hidden_value(page, e164_phone_number: str) -> None:
    try:
        page.evaluate(
            """
            (value) => {
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
              const inputs = Array.from(document.querySelectorAll('input[type="hidden"], input[name*="phone" i]'));
              inputs.forEach((input) => {
                const name = String(input.getAttribute('name') || input.id || '').toLowerCase();
                if (!/(phone|mobile|tel)/.test(name)) return;
                if (setter) setter.call(input, value);
                else input.value = value;
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
              });
            }
            """,
            e164_phone_number,
        )
    except Exception:
        pass


def _click_signup_link_if_on_login(page, log) -> None:
    """If the page shows login state, click Sign up link to switch to create-account."""
    try:
        signup_selectors = [
            'a:has-text("Sign up")',
            'a:has-text("sign up")',
            'a:has-text("Create account")',
            'a:has-text("create account")',
        ]
        for sel in signup_selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1000):
                    loc.click(timeout=2000)
                    log(f"Phone-first signup: clicked signup link: {sel}")
                    time.sleep(1.5)
                    return
            except Exception:
                continue
    except Exception:
        pass


def _click_visible_text_control(page, needles: list[str], log_label: str, log) -> bool:
    try:
        clicked = bool(
            page.evaluate(
                """
                (needles) => {
                  const normalize = (value) => String(value || '')
                    .normalize('NFD')
                    .replace(/[\\u0300-\\u036f]/g, '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toLowerCase();
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style
                      && style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && rect.width > 0
                      && rect.height > 0
                      && !el.disabled
                      && el.getAttribute('aria-disabled') !== 'true';
                  };
                  const controls = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                  const target = controls.find((el) => {
                    if (!visible(el)) return false;
                    const text = normalize([
                      el.innerText,
                      el.textContent,
                      el.getAttribute('aria-label'),
                      el.getAttribute('title'),
                      el.getAttribute('data-testid'),
                      el.id,
                    ].filter(Boolean).join(' '));
                    return needles.some((needle) => text.includes(normalize(needle)));
                  });
                  if (!target) return false;
                  target.scrollIntoView({ block: 'center', inline: 'center' });
                  target.click();
                  return true;
                }
                """,
                needles,
            )
        )
    except Exception as exc:
        log(f"Phone-first signup: {log_label} click failed: {exc}")
        return False
    if clicked:
        log(f"Phone-first signup: clicked {log_label}")
        time.sleep(1.5)
    return clicked


def _click_visible_selector_like_user(page, selector: str, log_label: str, log) -> bool:
    try:
        locator = page.locator(selector).first
        if not locator.is_visible(timeout=1200):
            return False
        try:
            locator.scroll_into_view_if_needed(timeout=1200)
        except Exception:
            pass
        for kwargs in (
            {"timeout": 3500},
            {"timeout": 3500, "force": True},
            {"timeout": 3500, "no_wait_after": True},
        ):
            try:
                locator.click(**kwargs)
                log(f"Phone-first signup: clicked {log_label}: {selector}")
                time.sleep(1.5)
                return True
            except Exception:
                continue
    except Exception as exc:
        log(f"Phone-first signup: {log_label} selector click failed: {exc}")
    return False


def _dismiss_chatgpt_cookie_banner(page, log) -> None:
    selectors = [
        'div[role="dialog"] button:has-text("Reject non-essential")',
        'div[role="dialog"] button:has-text("Reject")',
        'div[role="dialog"] button:has-text("Decline")',
        'div[role="dialog"] button:has-text("\ube44\ud544\uc218\uc0ac\ud56d \uac70\ubd80")',
        'div[role="dialog"] button[aria-label="Close"]',
        'div[role="dialog"] button[aria-label="\ub2eb\uae30"]',
        'div[role="dialog"] button[data-testid="close-button"]',
        'div[role="dialog"] button:has-text("Accept all")',
        'div[role="dialog"] button:has-text("Allow all")',
        'div[role="dialog"] button:has-text("\ubaa8\ub450 \ud5c8\uc6a9")',
    ]
    for selector in selectors:
        if _click_visible_selector_like_user(page, selector, "cookie banner", log):
            time.sleep(0.8)
            return


def _has_phone_number_continue_control(page) -> bool:
    if _find_phone_identity_input_selector(page, timeout=1):
        return True
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const normalize = (value) => String(value || '')
                    .normalize('NFD')
                    .replace(/[\\u0300-\\u036f]/g, '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toLowerCase();
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style
                      && style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && rect.width > 0
                      && rect.height > 0
                      && !el.disabled
                      && el.getAttribute('aria-disabled') !== 'true';
                  };
                  const phoneRe = /(phone|mobile|sms|text message|telephone|tel\\b|telefono|celular|numero de telefono|\\uc804\\ud654|\\uc804\\ud654\\ubc88\\ud638|\\ud734\\ub300|\\ud734\\ub300\\ud3f0|\\ubb38\\uc790|\\u624b\\u673a|\\u7535\\u8bdd|\\u96fb\\u8a71)/i;
                  return Array.from(document.querySelectorAll('button, a, [role="button"]')).some((el) => {
                    if (!visible(el)) return false;
                    const haystack = normalize([
                      el.innerText,
                      el.textContent,
                      el.getAttribute('aria-label'),
                      el.getAttribute('title'),
                      el.getAttribute('data-testid'),
                      el.id,
                    ].filter(Boolean).join(' '));
                    return phoneRe.test(haystack);
                  });
                }
                """
            )
        )
    except Exception:
        return False


def _has_phone_first_auth_prompt(page) -> bool:
    if _has_phone_number_continue_control(page):
        return True
    try:
        current_url = str(page.url or "").lower()
        if "auth.openai.com" in current_url or "accounts.openai.com" in current_url:
            return True
    except Exception:
        pass
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style
                      && style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && rect.width > 0
                      && rect.height > 0;
                  };
                  const authTextRe = /(phone|mobile|email|e-mail|password|log\\s*in|sign\\s*up|create\\s*account|continue|\\uc804\\ud654|\\ud734\\ub300|\\uc774\\uba54\\uc77c|\\ub85c\\uadf8\\uc778|\\ud68c\\uc6d0\\s*\\uac00\\uc785)/i;
                  if (Array.from(document.querySelectorAll('dialog[open], [role="dialog"]'))
                    .some((el) => {
                      if (!visible(el)) return false;
                      const text = String(el.innerText || el.textContent || '');
                      if (/cookie|\\ucfe0\\ud0a4/i.test(text) && !authTextRe.test(text)) return false;
                      return authTextRe.test(text);
                    })) {
                    return true;
                  }
                  return Array.from(document.querySelectorAll('input:not([type="hidden"])')).some((el) => {
                    if (!visible(el)) return false;
                    const hint = String([
                      el.type,
                      el.name,
                      el.id,
                      el.placeholder,
                      el.getAttribute('autocomplete'),
                      el.getAttribute('aria-label'),
                    ].filter(Boolean).join(' ')).toLowerCase();
                    return hint.includes('email') || hint.includes('username') || hint.includes('phone') || hint.includes('tel');
                  });
                }
                """
            )
        )
    except Exception:
        return False


def _wait_for_phone_first_auth_prompt(page, timeout: int = 10) -> bool:
    deadline = time.time() + max(int(timeout or 1), 1)
    while time.time() < deadline:
        if _has_phone_first_auth_prompt(page):
            return True
        time.sleep(0.4)
    return False


def _click_chatgpt_homepage_login_or_signup(page, log) -> bool:
    if _wait_for_phone_first_auth_prompt(page, timeout=1):
        return True
    _dismiss_chatgpt_cookie_banner(page, log)
    selectors = [
        'header button[data-testid="signup-button"]',
        'header button[data-testid="login-button"]',
        'header a[data-testid="signup-button"]',
        'header a[data-testid="login-button"]',
        'button[data-testid="signup-button"]',
        'button[data-testid="login-button"]',
        'a[data-testid="signup-button"]',
        'a[data-testid="login-button"]',
        '[role="button"][data-testid="signup-button"]',
        '[role="button"][data-testid="login-button"]',
        'button:has-text("Sign up")',
        'button:has-text("Log in")',
        'button:has-text("Login")',
        'button:has-text("Get started")',
        'a:has-text("Sign up")',
        'a:has-text("Log in")',
        'a:has-text("Login")',
        'a:has-text("Get started")',
        '[role="button"]:has-text("Sign up")',
        '[role="button"]:has-text("Log in")',
        '[role="button"]:has-text("Login")',
        '[role="button"]:has-text("Get started")',
        'button:has-text("\ubb34\ub8cc\ub85c \ud68c\uc6d0 \uac00\uc785")',
        'button:has-text("\ub85c\uadf8\uc778")',
        'a:has-text("\ubb34\ub8cc\ub85c \ud68c\uc6d0 \uac00\uc785")',
        'a:has-text("\ub85c\uadf8\uc778")',
        '[role="button"]:has-text("\ubb34\ub8cc\ub85c \ud68c\uc6d0 \uac00\uc785")',
        '[role="button"]:has-text("\ub85c\uadf8\uc778")',
    ]
    for selector in selectors:
        if not _click_visible_selector_like_user(page, selector, "login/signup", log):
            continue
        if _wait_for_phone_first_auth_prompt(page, timeout=8):
            return True
        log("Phone-first signup: login/signup click produced no auth prompt, trying next entry")
        _dismiss_chatgpt_cookie_banner(page, log)
    return _click_visible_text_control(
        page,
        [
            "sign up",
            "signup",
            "log in",
            "login",
            "get started",
            "free sign up",
            "continue",
            "\ubb34\ub8cc\ub85c \ud68c\uc6d0 \uac00\uc785",
            "\ub85c\uadf8\uc778",
        ],
        "login/signup",
        log,
    )


def _click_phone_number_continue_control(page, log) -> bool:
    if _find_phone_identity_input_selector(page, timeout=1):
        return True
    selectors = [
        'button[data-testid*="phone" i]',
        'a[data-testid*="phone" i]',
        '[role="button"][data-testid*="phone" i]',
        'button:has-text("Continue with phone")',
        'button:has-text("Continue with phone number")',
        'button:has-text("Use phone")',
        'button:has-text("Phone")',
        'button:has-text("Mobile")',
        'button:has-text("SMS")',
        'button:has-text("\uc804\ud654\ubc88\ud638\ub85c \uacc4\uc18d")',
        'button:has-text("\uc804\ud654\ub85c \uacc4\uc18d")',
        'button:has-text("\ud734\ub300\ud3f0\uc73c\ub85c \uacc4\uc18d")',
        'button:has-text("\uc804\ud654\ubc88\ud638")',
        'button:has-text("\ud734\ub300\ud3f0")',
        'a:has-text("Continue with phone")',
        'a:has-text("Phone")',
        'a:has-text("\uc804\ud654\ubc88\ud638")',
        '[role="button"]:has-text("Continue with phone")',
        '[role="button"]:has-text("Phone")',
        '[role="button"]:has-text("\uc804\ud654\ubc88\ud638")',
    ]
    for selector in selectors:
        if not _click_visible_selector_like_user(page, selector, "phone-number continue", log):
            continue
        if _find_phone_identity_input_selector(page, timeout=8):
            return True
        if _has_phone_number_continue_control(page):
            return True
    return _click_visible_text_control(
        page,
        [
            "phone",
            "mobile",
            "sms",
            "text message",
            "telephone",
            "telefono",
            "celular",
            "numero de telefono",
            "\uc804\ud654",
            "\uc804\ud654\ubc88\ud638",
            "\ud734\ub300",
            "\ud734\ub300\ud3f0",
            "\ubb38\uc790",
        ],
        "phone-number continue",
        log,
    )


def _find_phone_identity_input_selector(page, *, timeout: int = 10) -> str:
    deadline = time.time() + max(int(timeout or 1), 1)
    while time.time() < deadline:
        input_selector = _find_visible_phone_input_selector(page)
        if input_selector:
            return input_selector
        try:
            marked = bool(
                page.evaluate(
                    """
                    () => {
                      const normalize = (value) => String(value || '')
                        .normalize('NFD')
                        .replace(/[\\u0300-\\u036f]/g, '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                      const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style
                          && style.display !== 'none'
                          && style.visibility !== 'hidden'
                          && rect.width > 0
                          && rect.height > 0
                          && !el.disabled
                          && !el.readOnly;
                      };
                      const labelsFor = (el) => {
                        const labels = Array.from(document.querySelectorAll('label'))
                          .filter((label) => String(label.getAttribute('for') || '') === String(el.id || ''))
                          .map((label) => normalize(label.textContent));
                        labels.push(normalize(el.closest('label')?.textContent || ''));
                        labels.push(normalize(el.parentElement?.textContent || ''));
                        return labels.join(' ');
                      };
                      const bodyText = normalize(document.body?.innerText || '');
                      const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'))
                        .filter(visible);
                      let best = null;
                      let bestScore = -999;
                      for (const input of inputs) {
                        const type = normalize(input.getAttribute('type') || input.type || '');
                        const hint = normalize([
                          type,
                          input.getAttribute('name'),
                          input.id,
                          input.getAttribute('placeholder'),
                          input.getAttribute('autocomplete'),
                          input.getAttribute('aria-label'),
                          labelsFor(input),
                        ].filter(Boolean).join(' '));
                        let score = 0;
                        if (type === 'tel') score += 20;
                        if (hint.includes('tel') || hint.includes('phone') || hint.includes('mobile') || hint.includes('sms')) score += 12;
                        if (hint.includes('telefono') || hint.includes('celular') || hint.includes('numero')) score += 8;
                        if (hint.includes('\\uc804\\ud654') || hint.includes('\\uc804\\ud654\\ubc88\\ud638') || hint.includes('\\ud734\\ub300') || hint.includes('\\ud734\\ub300\\ud3f0')) score += 8;
                        if (type === 'email' || hint.includes('email') || hint.includes('mail')) score -= 20;
                        if (type === 'password' || hint.includes('password')) score -= 40;
                        if (input.getAttribute('role') === 'combobox') score -= 6;
                        if (best === null || score > bestScore) {
                          best = input;
                          bestScore = score;
                        }
                      }
                      if (!best) return false;
                      if (bestScore < 2 && !(inputs.length === 1 && (
                        bodyText.includes('phone')
                        || bodyText.includes('telefono')
                        || bodyText.includes('mobile')
                        || bodyText.includes('\\uc804\\ud654')
                        || bodyText.includes('\\ud734\\ub300')
                      ))) {
                        return false;
                      }
                      document.querySelectorAll('input[data-codex-phone-first-input="1"]')
                        .forEach((el) => el.removeAttribute('data-codex-phone-first-input'));
                      best.setAttribute('data-codex-phone-first-input', '1');
                      return true;
                    }
                    """
                )
            )
        except Exception:
            marked = False
        if marked:
            return 'input[data-codex-phone-first-input="1"]'
        time.sleep(0.5)
    return ""


def _submit_phone_identity_via_page(page, phone_number: str, log) -> dict:
    _click_signup_link_if_on_login(page, log)
    input_selector = _find_phone_identity_input_selector(page, timeout=10)
    if not input_selector:
        _dump_debug(page, "phone_input_not_found")
        raise RuntimeError("Phone-first signup did not find a phone input")

    dial_code, local_number, country_name = _parse_phone_country_and_local(phone_number)
    country_selected = False
    country_state = {}
    if dial_code:
        try:
            country_selected = _select_phone_country_ui(page, dial_code, country_name, log)
        except Exception as exc:
            log(f"Phone-first signup: country select failed: {exc}")
        country_state = _get_phone_country_select_state(page, dial_code, country_name)
        country_selected = bool(
            country_state.get("matchesIso")
            or country_state.get("matchesDial")
            or country_state.get("matchesCountry")
        )
        log(
            "Phone-first signup: country select state "
            f"hasTrigger={country_state.get('hasTrigger')} "
            f"matchesDial={country_state.get('matchesDial')} "
            f"matchesCountry={country_state.get('matchesCountry')}"
        )
        if not country_selected:
            _dump_debug(page, "phone_country_not_selected")
            raise RuntimeError(f"Phone-first signup country not selected: +{dial_code} {country_name}")
    e164_phone = "+" + str(phone_number or "").lstrip("+").strip()
    fill_value = local_number if local_number else e164_phone
    log(f"Phone-first signup: filling phone {_mask_phone_number(fill_value)}")
    if not _fill_input_like_user(page, input_selector, fill_value):
        _dump_debug(page, "phone_fill_failed")
        raise RuntimeError("Phone-first signup failed to fill phone input")
    if dial_code and not _phone_input_matches_expected(page, input_selector, dial_code, local_number):
        _dump_debug(page, "phone_fill_unexpected_value")
        raise RuntimeError("Phone-first signup phone input contains unexpected country code/value")
    if dial_code:
        _sync_generic_phone_hidden_value(page, e164_phone)

    submit_selector = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"Phone-first signup clicked continue: {submit_selector}")
    elif _submit_form_with_fallback(page, input_selector):
        log("Phone-first signup used form fallback submit")
    else:
        raise RuntimeError("Phone-first signup did not find a continue button")

    return _wait_for_signup_entry_transition(page, log, allow_chatgpt_home=False, click_passwordless_login=False)


def _wait_for_page_ready(page, *, timeout: int = 15) -> None:
    """Wait until page has a visible input or button (past Cloudflare challenge)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            has_input = page.evaluate(
                """() => {
                    const inputs = document.querySelectorAll('input:not([type="hidden"])');
                    const buttons = document.querySelectorAll('button');
                    return inputs.length > 0 || buttons.length > 0;
                }"""
            )
            if has_input:
                return
        except Exception:
            pass
        time.sleep(0.5)


def _is_cloudflare_challenge_page(page) -> bool:
    try:
        title = str(page.title() or "").lower()
        if "just a moment" in title or "cloudflare" in title:
            return True
        body = str(page.evaluate("() => (document.body?.innerText || '').substring(0, 1200)") or "").lower()
        if "just a moment" in body or "checking your browser" in body or "cloudflare" in body:
            return True
        if page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="cloudflare.com"], input[name="cf-turnstile-response"]').count() > 0:
            return True
    except Exception:
        pass
    return False


def _wait_for_cloudflare_clear(page, log, timeout: int = 90) -> bool:
    deadline = time.time() + max(int(timeout or 1), 1)
    while time.time() < deadline:
        if _is_cloudflare_challenge_page(page):
            if _click_first(
                page,
                [
                    'button:has-text("Verify you are human")',
                    'button:has-text("Continue")',
                    'button:has-text("Verify")',
                    'button:has-text("Checking your browser")',
                ],
                timeout=1,
            ):
                time.sleep(1)
            time.sleep(1.5)
            continue
        if not _is_cloudflare_challenge_page(page):
            return True
        time.sleep(1.5)
    return not _is_cloudflare_challenge_page(page)


def _return_phone_first_signup_to_phone_entry(page, log) -> bool:
    selectors = [
        'button:has-text("Edit")',
        'a:has-text("Edit")',
        'button:has-text("Change")',
        'a:has-text("Change")',
        'button:has-text("Switch")',
        'a:has-text("Switch")',
        'button:has-text("Modify")',
        'a:has-text("Modify")',
        'button:has-text("Back")',
        'a:has-text("Back")',
    ]
    clicked = _click_first(page, selectors, timeout=2)
    if clicked:
        log(f"Phone-first signup: clicked phone edit entry: {clicked}")
    else:
        clicked = "text-control" if _click_visible_text_control(
            page,
            [
                "edit",
                "change",
                "switch",
                "modify",
                "try another",
                "use another",
                "cambiar",
                "editar",
                "modificar",
                "volver",
                "\uc218\uc815",
                "\ubcc0\uacbd",
                "\ubc14\uafb8",
                "\ub2e4\ub978",
                "\ub2e4\uc2dc",
            ],
            "phone edit/change",
            log,
        ) else ""
    if not clicked:
        log("Phone-first signup: no phone edit/change control found")
        _dump_debug(page, "phone_first_edit_change_not_found")
        return False
    if _find_phone_identity_input_selector(page, timeout=8):
        return True
    log("Phone-first signup: edit/change control did not reveal phone input")
    _dump_debug(page, "phone_first_edit_change_no_phone_input")
    return False

def _clear_auth_session_cookies(page, log) -> None:
    """Clear auth state so the next phone retry starts from a clean auth entry."""
    auth_cookie_names = {
        "login_session",
        "oai-client-auth-session",
        "__Secure-next-auth.session-token",
        "__Host-next-auth.csrf-token",
        "__Secure-next-auth.callback-url",
        "auth0",
        "auth0_compat",
    }
    auth_cookie_prefixes = (
        "__Secure-next-auth.",
        "__Host-next-auth.",
        "auth0",
        "login_session",
        "oai-client-auth-session",
    )

    def should_drop_cookie(cookie: dict) -> bool:
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if name in auth_cookie_names or any(name.startswith(prefix) for prefix in auth_cookie_prefixes):
            return True
        return domain in {"auth.openai.com", "chatgpt.com"} and (
            "auth" in name.lower() or "session" in name.lower() or "callback" in name.lower()
        )

    try:
        ctx = page.context
        cookies = ctx.cookies()
        if not any(should_drop_cookie(cookie) for cookie in cookies):
            return
        ctx.clear_cookies()
        sanitized = []
        for cookie in cookies:
            if should_drop_cookie(cookie):
                continue
            entry = {}
            for key in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"):
                if key in cookie:
                    entry[key] = cookie[key]
            if entry.get("expires") == -1:
                entry.pop("expires", None)
            if "name" in entry and "value" in entry and ("domain" in entry or "url" in entry):
                sanitized.append(entry)
        if sanitized:
            ctx.add_cookies(sanitized)
        log("Phone-first signup: cleared auth session cookies for fresh retry")
    except Exception as exc:
        log(f"Phone-first signup: cookie clear warning: {exc}")

    try:
        page.evaluate(
            """() => {
                try { window.localStorage?.clear(); } catch (_) {}
                try { window.sessionStorage?.clear(); } catch (_) {}
            }"""
        )
        log("Phone-first signup: cleared auth browser storage for fresh retry")
    except Exception as exc:
        log(f"Phone-first signup: storage clear warning: {exc}")


def _is_session_ended_page(page) -> bool:
    """Detect if current page shows 'Your session has ended' state."""
    try:
        title = str(page.title() or "").lower()
        if "session" in title and "ended" in title:
            return True
        text = str(page.evaluate("() => (document.body?.innerText || '').substring(0, 500)") or "").lower()
        if "session" in text and ("ended" in text or "expired" in text):
            return True
    except Exception:
        pass
    return False


def _nuke_all_browser_state(page, log) -> None:
    """Clear ALL cookies and browser storage to fully reset session state.

    Used when partial cookie clearing is insufficient (e.g. session-ended page persists).
    """
    try:
        ctx = page.context
        ctx.clear_cookies()
        log("Phone-first signup: nuked all cookies")
    except Exception as exc:
        log(f"Phone-first signup: cookie nuke warning: {exc}")
    # Clear localStorage and sessionStorage
    try:
        page.evaluate("""() => {
            try { localStorage.clear(); } catch(e) {}
            try { sessionStorage.clear(); } catch(e) {}
        }""")
    except Exception:
        pass


def _reset_phone_callback_for_new_number(phone_callback, reason: str) -> None:
    if not phone_callback:
        return
    marker = getattr(phone_callback, "mark_send_failed", None)
    if callable(marker):
        try:
            marker(str(reason or "phone rejected"))
            return
        except Exception:
            pass
    for name, value in (
        ("phase", "need_number"),
        ("activation", None),
        ("completed", False),
        ("awaiting_external_success", False),
    ):
        try:
            if hasattr(phone_callback, name):
                setattr(phone_callback, name, value)
        except Exception:
            pass


def _start_phone_first_signup_from_forced_entry(page, phone_callback, log) -> tuple[dict, str]:
    if not phone_callback:
        raise RuntimeError('Phone-first signup requires an SMS phone callback')
    phone_number = str(phone_callback() or '').strip()
    if not phone_number:
        raise RuntimeError('Phone-first signup did not receive a phone number')
    log(f'Phone-first signup using phone: {_mask_phone_number(phone_number)}')
    try:
        page.goto('about:blank', wait_until='domcontentloaded', timeout=5000)
    except Exception:
        pass
    time.sleep(0.5)

    log('Phone-first signup step 1: visiting chatgpt.com homepage')
    _goto_with_retry(page, f'{CHATGPT_APP}/', wait_until='domcontentloaded', timeout=30000, log=log)
    _wait_for_page_ready(page, timeout=20)
    if _is_cloudflare_challenge_page(page):
        log('Phone-first signup: homepage hit Cloudflare challenge, waiting for clear or solver')
        _wait_for_cloudflare_clear(page, log, timeout=90)
    time.sleep(1.5)

    log('Phone-first signup step 2: clicking login/signup button')
    login_clicked = False
    for _attempt in range(3):
        login_clicked = _click_chatgpt_homepage_login_or_signup(page, log)
        if login_clicked:
            break
        time.sleep(1)
    if not login_clicked:
        _dump_debug(page, "phone_first_login_button_not_found")
        raise RuntimeError("Phone-first signup did not find login/signup button on homepage")

    log('Phone-first signup step 3: clicking phone-number continue button')
    phone_clicked = False
    for _attempt in range(3):
        if _find_phone_identity_input_selector(page, timeout=1):
            phone_clicked = True
            break
        phone_clicked = _click_phone_number_continue_control(page, log)
        if phone_clicked:
            break
        time.sleep(1)
    if not phone_clicked:
        _dump_debug(page, "phone_first_phone_button_not_found")
        raise RuntimeError("Phone-first signup did not find phone-number continue button")
    if _is_session_ended_page(page):
        raise RuntimeError("Phone-first signup encountered session-ended page")

    log('Phone-first signup step 4: filling phone number')
    state = _submit_phone_identity_via_page(page, phone_number, log)
    return state, phone_number


def _start_browser_phone_signup_via_authorize(page, phone_callback, device_id: str, log) -> tuple[dict, str]:
    """Compatibility wrapper. Phone-first signup now starts from chatgpt.com UI."""
    return _start_phone_first_signup_from_forced_entry(page, phone_callback, log)


def _dump_debug(page, prefix: str) -> None:
    try:
        page.screenshot(path=f"/tmp/{prefix}.png")
    except Exception:
        pass
    try:
        with open(f"/tmp/{prefix}.html", "w", encoding="utf-8", errors="replace") as f:
            f.write(page.content())
    except Exception:
        pass


def _get_cookies(page) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies()}


def _cookies_to_header(cookies_dict: dict) -> str:
    parts = []
    for name, value in (cookies_dict or {}).items():
        if name and value not in (None, ""):
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _decode_jwt_payload_no_verify(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_chatgpt_account_id(access_token: str) -> str:
    payload = _decode_jwt_payload_no_verify(access_token)
    auth_info = payload.get("https://api.openai.com/auth") or {}
    if isinstance(auth_info, dict):
        account_id = str(auth_info.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    return str(payload.get("sub") or "").strip()


def _chatgpt_session_result_from_data(data: dict, page, cookies_dict: dict, log) -> tuple[dict | None, str]:
    if not isinstance(data, dict):
        return None, "session API JSON 不是对象"

    access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
    if not access_token:
        return None, "session API 未返回 accessToken"

    latest_cookies = dict(cookies_dict or {})
    try:
        latest_cookies.update(_get_cookies(page))
    except Exception as exc:
        log(f"ChatGPT session cookies 读取失败，使用已捕获 cookies: {exc}")
    session_token = str(latest_cookies.get("__Secure-next-auth.session-token") or "").strip()
    account_id = _extract_chatgpt_account_id(access_token)
    result = {
        "access_token": access_token,
        "refresh_token": str(data.get("refreshToken") or data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("idToken") or data.get("id_token") or "").strip(),
        "session_token": session_token,
        "account_id": account_id,
        "workspace_id": str(data.get("workspaceId") or data.get("workspace_id") or "").strip(),
        "profile": data.get("user") if isinstance(data.get("user"), dict) else {},
        "expires_at": str(data.get("expires") or "").strip(),
        "cookies": _cookies_to_header(latest_cookies),
        "session": data,
    }
    log(
        "ChatGPT session 获取成功: "
        f"accessToken=yes, session_token={'yes' if session_token else 'no'}, "
        f"account_id={account_id or '-'}"
    )
    return result, ""


def _chatgpt_session_result_from_text(text: str, page, cookies_dict: dict, log) -> tuple[dict | None, str]:
    try:
        data = json.loads(text)
    except Exception as exc:
        return None, f"session API JSON 解析失败: {exc}"
    return _chatgpt_session_result_from_data(data, page, cookies_dict, log)


def _fetch_chatgpt_session_via_same_origin(page, cookies_dict: dict, log, session_url: str) -> tuple[dict | None, str, bool]:
    current_url = str(getattr(page, "url", "") or "")
    if "chatgpt.com" not in current_url.lower():
        return None, "", False

    log(f"浏览器内请求 ChatGPT session API: {session_url}")
    try:
        payload = page.evaluate(
            """
            async (sessionUrl) => {
              const response = await fetch(sessionUrl, {
                method: "GET",
                credentials: "include",
                headers: { "accept": "application/json" },
              });
              return {
                status: response.status,
                url: response.url,
                text: await response.text(),
              };
            }
            """,
            session_url,
        )
    except Exception as exc:
        return None, str(exc), True

    if not isinstance(payload, dict):
        return None, "session API 浏览器内请求未返回对象", True

    status = int(payload.get("status") or 0)
    response_url = str(payload.get("url") or "")
    text = str(payload.get("text") or "")
    log(f"ChatGPT session API 浏览器内请求状态: {status} url={response_url}")
    if status == 200 and text:
        return (*_chatgpt_session_result_from_text(text, page, cookies_dict, log), True)
    return None, f"session API HTTP {status}: {text}", True


def _fetch_chatgpt_session_from_page(page, cookies_dict: dict, log, timeout: int = 45) -> dict:
    deadline = time.time() + max(int(timeout or 0), 5)
    last_error = ""
    session_url = f"{CHATGPT_APP}/api/auth/session"
    log(f"打开 ChatGPT session API: {session_url}")

    while time.time() < deadline:
        same_origin_result, same_origin_error, same_origin_attempted = _fetch_chatgpt_session_via_same_origin(
            page,
            cookies_dict,
            log,
            session_url,
        )
        if same_origin_result:
            return same_origin_result
        if same_origin_attempted and same_origin_error:
            last_error = same_origin_error
            log(f"ChatGPT session API 浏览器内请求暂未拿到 token: {last_error}")
            if "object has no attribute 'evaluate'" not in last_error:
                time.sleep(2)
                continue

        try:
            response = page.goto(session_url, wait_until="domcontentloaded", timeout=15000)
            status = int(response.status if response else 0)
            if response:
                try:
                    text = response.text()
                except Exception as body_exc:
                    last_error = str(body_exc)
                    log(f"ChatGPT session API 响应体不可直接读取，改读页面正文: {last_error}")
                    text = page.locator("body").inner_text(timeout=3000)
            else:
                text = page.locator("body").inner_text(timeout=3000)
            current_url = str(getattr(page, "url", "") or "")
            log(f"ChatGPT session API 状态: {status} url={current_url}")
            if status == 200 and text:
                result, error = _chatgpt_session_result_from_text(text, page, cookies_dict, log)
                if result:
                    return result
                last_error = error
            else:
                last_error = f"session API HTTP {status}: {text}"
            log(f"ChatGPT session API 暂未拿到 token: {last_error}")
        except Exception as exc:
            last_error = str(exc)
            log(f"ChatGPT session API 打开异常: {last_error}")
        time.sleep(2)

    raise RuntimeError(f"ChatGPT session 未返回 accessToken: {last_error}")


def _random_chrome_ua() -> str:
    patch = random.randint(0, 220)
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/136.0.7103.{patch} Safari/537.36"
    )


def _infer_sec_ch_ua(user_agent: str) -> str:
    match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
    major = str(match.group(1) if match else "136")
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'


def _build_browser_headers(
    *,
    user_agent: str,
    accept: str,
    referer: str = "",
    origin: str = "",
    content_type: str = "",
    navigation: bool = False,
    extra_headers: dict | None = None,
) -> dict:
    headers = {
        "user-agent": user_agent or _random_chrome_ua(),
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": _infer_sec_ch_ua(user_agent),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "accept": accept,
    }
    if referer:
        headers["referer"] = referer
    if origin:
        headers["origin"] = origin
    if content_type:
        headers["content-type"] = content_type
    if navigation:
        headers["sec-fetch-dest"] = "document"
        headers["sec-fetch-mode"] = "navigate"
        headers["sec-fetch-user"] = "?1"
        headers["upgrade-insecure-requests"] = "1"
    else:
        headers["sec-fetch-dest"] = "empty"
        headers["sec-fetch-mode"] = "cors"
    for key, value in dict(extra_headers or {}).items():
        if value is not None:
            headers[key] = value
    return headers


def _browser_pause(page, *, headed: bool = True):
    delay_ms = random.randint(150, 450) if headed else random.randint(60, 180)
    try:
        page.wait_for_timeout(delay_ms)
    except Exception:
        time.sleep(delay_ms / 1000)


def _generate_datadog_trace_headers() -> dict:
    trace_hex = secrets.token_hex(8).rjust(16, "0")
    parent_hex = secrets.token_hex(8).rjust(16, "0")
    trace_id = str(int(trace_hex, 16))
    parent_id = str(int(parent_hex, 16))
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _infer_page_type(data: dict | None, current_url: str = "") -> str:
    raw = data if isinstance(data, dict) else {}
    page_type = str(((raw.get("page") or {}).get("type")) or "").strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    if page_type:
        return page_type
    url = (current_url or "").lower()
    if "code=" in url:
        return "oauth_callback"
    if "create-account/password" in url:
        return "create_account_password"
    if "email-verification" in url or "email-otp" in url:
        return "email_otp_verification"
    if "about-you" in url:
        return "about_you"
    if "log-in/password" in url:
        return "login_password"
    if "sign-in-with-chatgpt" in url and "consent" in url:
        return "consent"
    if "workspace" in url and "select" in url:
        return "workspace_selection"
    if "organization" in url and "select" in url:
        return "organization_selection"
    if "add-phone" in url:
        return "add_phone"
    if "add-email" in url:
        return "add_email"
    if "/api/oauth/oauth2/auth" in url:
        return "external_url"
    if "chatgpt.com" in url:
        return "chatgpt_home"
    return ""


def _extract_flow_state(data: dict | None, current_url: str = "") -> dict:
    raw = data if isinstance(data, dict) else {}
    page = raw.get("page") or {}
    payload = page.get("payload") or {}
    continue_url = str(raw.get("continue_url") or payload.get("url") or "").strip()
    if continue_url and continue_url.startswith("/"):
        continue_url = urljoin(OPENAI_AUTH, continue_url)
    effective_url = continue_url or current_url
    return {
        "page_type": _infer_page_type(raw, effective_url),
        "continue_url": continue_url,
        "method": str(raw.get("method") or payload.get("method") or "GET").upper(),
        "current_url": effective_url,
        "payload": payload if isinstance(payload, dict) else {},
        "raw": raw,
    }


def _extract_code_from_url(url: str) -> str:
    if not url or "code=" not in url:
        return ""
    try:
        parsed = urlparse(url)
        values = parse_qs(parsed.query, keep_blank_values=True)
        return str((values.get("code") or [""])[0] or "").strip()
    except Exception:
        return ""


def _mask_log_value(value: str, *, head: int = 8, tail: int = 4) -> str:
    """日志用脱敏：保留可定位前后缀，不打印完整授权码/代理密码。"""
    text = str(value or "")
    if not text:
        return "(空)"
    if len(text) <= head + tail + 3:
        return text
    return f"{text[:head]}...{text[-tail:]}(len={len(text)})"


def _mask_proxy_for_log(proxy: str | None) -> str:
    """代理日志脱敏；空代理意味着 token exchange 使用本机出口 IP。"""
    value = str(proxy or "").strip()
    if not value:
        return "(无，使用本机出口 IP)"
    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = parsed.scheme or "proxy"
        auth = "有认证@" if parsed.username or parsed.password else ""
        return f"{scheme}://{auth}{host}{port}"
    except Exception:
        return _mask_log_value(value)


def _oauth_authorize_debug_summary(oauth_start, proxy: str | None) -> str:
    """说明授权登录链接的生成来源与关键参数，便于排查地区/代理问题。"""
    parsed = urlparse(str(getattr(oauth_start, "auth_url", "") or ""))
    query = parse_qs(parsed.query, keep_blank_values=True)

    def one(key: str) -> str:
        return str((query.get(key) or [""])[0] or "")

    return (
        "  OAuth 授权链接来源: 本地 generate_oauth_url(Codex client + PKCE); "
        f"host={parsed.netloc or '-'} "
        f"client_id={one('client_id') or getattr(oauth_start, 'client_id', '')} "
        f"redirect_uri={one('redirect_uri') or getattr(oauth_start, 'redirect_uri', '')} "
        f"scope={one('scope') or '-'} "
        f"prompt={one('prompt') or '-'} "
        f"state={_mask_log_value(one('state') or getattr(oauth_start, 'state', ''))} "
        f"code_challenge={_mask_log_value(one('code_challenge'))} "
        f"proxy={_mask_proxy_for_log(proxy)}"
    )


def _callback_debug_summary(callback_url: str, oauth_start, proxy: str | None) -> str:
    """记录 callback 与 token exchange 关键参数；不打印完整 code。"""
    parsed = urlparse(str(callback_url or ""))
    query = parse_qs(parsed.query, keep_blank_values=True)

    def one(key: str) -> str:
        return str((query.get(key) or [""])[0] or "")

    code = one("code")
    state = one("state")
    expected_state = str(getattr(oauth_start, "state", "") or "")
    state_status = "missing" if not state else ("match" if state == expected_state else "mismatch")
    return (
        "  OAuth callback 捕获: "
        f"host={parsed.netloc or '-'} path={parsed.path or '-'} "
        f"code={_mask_log_value(code)} "
        f"state={_mask_log_value(state)} state_status={state_status} "
        f"expected_state={_mask_log_value(expected_state)} "
        f"scope={one('scope') or '-'} "
        f"token_client_id={getattr(oauth_start, 'client_id', '')} "
        f"token_redirect_uri={getattr(oauth_start, 'redirect_uri', '')} "
        f"proxy={_mask_proxy_for_log(proxy)}"
    )


def _extract_callback_error_from_url(callback_url: str) -> str:
    """提取 OAuth error callback；有 code 时仍按成功 callback 处理。"""
    value = str(callback_url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        if "localhost" not in parsed.netloc.lower() or not parsed.path.endswith("/auth/callback"):
            return ""
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("code"):
            return ""
        error = str((query.get("error") or [""])[0] or "").strip()
        if not error:
            return ""
        description = str((query.get("error_description") or [""])[0] or "").strip()
        return f"{error}: {description}" if description else error
    except Exception:
        return ""


def _oauth_restart_required_result(callback_url: str, reason: str) -> dict:
    """返回给 OAuth 状态机的内部重启标记。"""
    return {
        "oauth_restart_required": True,
        "callback_captured": True,
        "callback_url": str(callback_url or ""),
        "error": str(reason or "OAuth callback error"),
    }


def _oauth_resume_url_after_phone(auth_url: str) -> str:
    """手机号验证通过后重访 OAuth 时改用 prompt=none，避免强制二次登录。"""
    value = str(auth_url or "").strip()
    if not value:
        return ""
    if "prompt=login" in value:
        return value.replace("prompt=login", "prompt=none")
    if "prompt=" not in value:
        sep = "&" if "?" in value else "?"
        return f"{value}{sep}prompt=none"
    return value


def _normalize_url(target_url: str, base_url: str = OPENAI_AUTH) -> str:
    value = str(target_url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    try:
        return urljoin(base_url, value)
    except Exception:
        return value


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        pad = "=" * ((4 - (len(payload) % 4)) % 4)
        return json.loads(base64.urlsafe_b64decode((payload + pad).encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


class _SentinelTokenGenerator:
    def __init__(self, device_id: str, user_agent: str):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or _random_chrome_ua()
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        return f"{h & 0xFFFFFFFF:08x}"

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")

    def _config(self) -> list:
        perf_now = 1000 + random.random() * 49000
        return [
            "1920x1080",
            time.strftime("%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            "webkitTemporaryStorage−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            int(time.time() * 1000 - perf_now),
        ]

    def generate_requirements_token(self) -> str:
        cfg = self._config()
        cfg[3] = 1
        cfg[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._b64(cfg)

    def generate_token(self, seed: str, difficulty: str) -> str:
        max_attempts = 500000
        cfg = self._config()
        start_ms = int(time.time() * 1000)
        diff = str(difficulty or "0")
        for nonce in range(max_attempts):
            cfg[3] = nonce
            cfg[9] = round(int(time.time() * 1000) - start_ms)
            encoded = self._b64(cfg)
            digest = self._fnv1a32((seed or "") + encoded)
            if digest[: len(diff)] <= diff:
                return "gAAAAAB" + encoded + "~S"
        return "gAAAAAB" + self._b64(None)


def _browser_fetch(page, url: str, *, method: str = "GET", headers: dict | None = None, body: str | None = None, redirect: str = "manual", timeout_ms: int = 30000) -> dict:
    return page.evaluate(
        """
        async ({ url, method, headers, body, redirect, timeoutMs }) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(new Error(`fetch timeout after ${timeoutMs}ms`)), timeoutMs);
          try {
            const resp = await fetch(url, {
              method,
              headers: headers || {},
              body: body === null ? undefined : body,
              redirect,
              signal: controller.signal,
            });
            const respHeaders = {};
            resp.headers.forEach((v, k) => { respHeaders[k] = v; });
            let text = '';
            try { text = await resp.text(); } catch {}
            let data = null;
            try { data = JSON.parse(text); } catch {}
            return { ok: resp.ok, status: resp.status, url: resp.url || url, headers: respHeaders, text, data };
          } catch (e) {
            return { ok: false, status: 0, url, headers: {}, text: String(e && e.message || e), data: null };
          } finally {
            clearTimeout(timer);
          }
        }
        """,
        {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
            "redirect": redirect,
            "timeoutMs": timeout_ms,
        },
    )


def _build_browser_sentinel_token(page, device_id: str, flow: str, user_agent: str) -> str:
    generator = _SentinelTokenGenerator(device_id, user_agent)
    req_body = json.dumps(
        {"p": generator.generate_requirements_token(), "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    result = _browser_fetch(
        page,
        SENTINEL_REQ_URL,
        method="POST",
        headers=_build_browser_headers(
            user_agent=user_agent,
            accept="*/*",
            referer=SENTINEL_FRAME_URL,
            origin=SENTINEL_BASE,
            content_type="text/plain;charset=UTF-8",
            extra_headers={
                "sec-fetch-site": "same-origin",
            },
        ),
        body=req_body,
        redirect="follow",
    )
    data = result.get("data") or {}
    challenge_token = str(data.get("token") or "").strip()
    if not challenge_token:
        return ""
    pow_meta = data.get("proofofwork") or {}
    if pow_meta.get("required") and pow_meta.get("seed"):
        p_value = generator.generate_token(str(pow_meta.get("seed") or ""), str(pow_meta.get("difficulty") or "0"))
    else:
        p_value = generator.generate_requirements_token()
    return json.dumps(
        {
            "p": p_value,
            "t": "",
            "c": challenge_token,
            "id": device_id,
            "flow": flow,
        },
        separators=(",", ":"),
    )


def _submit_browser_user_register(page, email: str, password: str, device_id: str, user_agent: str) -> dict:
    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=f"{OPENAI_AUTH}/create-account/password",
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )
    sentinel = _build_browser_sentinel_token(page, device_id, "username_password_create", user_agent)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/user/register",
        method="POST",
        headers=headers,
        body=json.dumps({"username": email, "password": password}),
        redirect="follow",
    )


def _send_browser_email_otp(page, *, referer: str = "") -> dict:
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/email-otp/send",
        method="GET",
        headers={
            "accept": "application/json, text/plain, */*",
            "referer": referer or f"{OPENAI_AUTH}/create-account/password",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "accept-language": "en-US,en;q=0.9",
        },
        redirect="follow",
    )


def _decode_oauth_session_cookie(cookies_dict: dict) -> dict:
    raw = str(cookies_dict.get("oai-client-auth-session") or "").strip()
    if not raw:
        return {}
    first = raw.split(".")[0]
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            pad = "=" * ((4 - (len(first) % 4)) % 4)
            decoded = decoder((first + pad).encode("ascii")).decode("utf-8")
            parsed = json.loads(decoded)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return {}


def _extract_workspace_from_consent_html(session, consent_url: str) -> dict:
    try:
        response = session.get(consent_url, allow_redirects=True, timeout=30)
        html = response.text or ""
        if "workspaces" not in html:
            return {}
        ids = re.findall(r'"id"(?:,|:)"([0-9a-f-]{36})"', html, flags=re.I)
        kinds = re.findall(r'"kind"(?:,|:)"([^"]+)"', html, flags=re.I)
        if not ids:
            return {}
        seen: set[str] = set()
        workspaces: list[dict] = []
        for idx, workspace_id in enumerate(ids):
            if workspace_id in seen:
                continue
            seen.add(workspace_id)
            item = {"id": workspace_id}
            if idx < len(kinds):
                item["kind"] = kinds[idx]
            workspaces.append(item)
        return {"workspaces": workspaces} if workspaces else {}
    except Exception:
        return {}


def _seed_session_cookies(session, cookies_dict: dict):
    for name, value in cookies_dict.items():
        for domain in [".openai.com", ".chatgpt.com", ".auth.openai.com", "auth.openai.com", "chatgpt.com"]:
            try:
                session.cookies.set(name, value, domain=domain, path="/")
            except Exception:
                pass


def _follow_redirects_for_code(session, start_url: str, log, *, max_redirects: int = 12) -> str:
    current_url = start_url
    for idx in range(max_redirects):
        response = session.get(current_url, allow_redirects=False, timeout=30)
        log(f"  redirect-follow[{idx+1}] {response.status_code} {str(current_url)}")
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            break
        next_url = urljoin(current_url, location)
        code = _extract_code_from_url(next_url)
        if code:
            return next_url
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        current_url = next_url
    return ""


def _complete_oauth_with_session(cookies_dict: dict, oauth_start, proxy: str | None, log) -> dict | None:
    from .oauth import submit_callback_url
    from curl_cffi import requests as cffi_requests

    s = cffi_requests.Session(impersonate="chrome131")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    _seed_session_cookies(s, cookies_dict)

    try:
        session_meta = _decode_oauth_session_cookie(cookies_dict)
        consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        workspaces = list(session_meta.get("workspaces") or [])
        if not workspaces:
            session_meta = _extract_workspace_from_consent_html(s, consent_url)
            workspaces = list(session_meta.get("workspaces") or [])
        if not workspaces:
            log("  ⚠️ 缺少 oai-client-auth-session workspaces，OAuth 失败")
            return None
        workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
        log(f"  选择 workspace: {workspace_id}")
        ws_resp = s.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers={
                "accept": "application/json",
                "referer": consent_url,
                "origin": OPENAI_AUTH,
                "content-type": "application/json",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            },
            data=json.dumps({"workspace_id": workspace_id}),
            allow_redirects=False,
            timeout=30,
        )
        log(f"  workspace/select -> {ws_resp.status_code}")

        next_url = str(ws_resp.headers.get("Location") or "").strip()
        next_data = {}
        if not next_url:
            try:
                next_data = ws_resp.json() or {}
            except Exception:
                next_data = {}
            next_url = str(next_data.get("continue_url") or "").strip()
        next_url = _normalize_url(next_url, consent_url)
        direct_code = _extract_code_from_url(next_url)
        if direct_code:
            result_json = submit_callback_url(
                callback_url=next_url,
                expected_state=oauth_start.state,
                code_verifier=oauth_start.code_verifier,
                proxy_url=proxy,
            )
            return json.loads(result_json)

        orgs = list((((next_data.get("data") or {}).get("orgs")) or []))
        if orgs and orgs[0].get("id"):
            org_id = str(orgs[0].get("id") or "").strip()
            org_body = {"org_id": org_id}
            projects = list(orgs[0].get("projects") or [])
            if projects and projects[0].get("id"):
                org_body["project_id"] = str(projects[0].get("id") or "").strip()
            log(f"  选择 organization: {org_id}")
            org_resp = s.post(
                "https://auth.openai.com/api/accounts/organization/select",
                headers={
                    "accept": "application/json",
                    "referer": consent_url,
                    "origin": OPENAI_AUTH,
                    "content-type": "application/json",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                },
                data=json.dumps(org_body),
                allow_redirects=False,
                timeout=30,
            )
            log(f"  organization/select -> {org_resp.status_code}")
            next_url = str(org_resp.headers.get("Location") or "").strip() or next_url
            if not next_url:
                try:
                    org_data = org_resp.json() or {}
                    next_url = str(org_data.get("continue_url") or "").strip()
                    if not next_url:
                        org_state = _extract_flow_state(org_data, str(org_resp.url))
                        next_url = org_state.get("continue_url") or org_state.get("current_url") or ""
                except Exception:
                    next_url = ""
            next_url = _normalize_url(next_url, consent_url)

        if not next_url and next_data:
            state = _extract_flow_state(next_data, str(ws_resp.url))
            next_url = state.get("continue_url") or state.get("current_url") or ""
            next_url = _normalize_url(next_url, consent_url)

        if not next_url:
            next_url = "https://auth.openai.com/api/oauth/oauth2/auth?" + oauth_start.auth_url.split("?", 1)[1]

        callback_url = _follow_redirects_for_code(s, next_url, log)
        if not callback_url:
            log("  ⚠️ 未能跟到 OAuth callback")
            return None
        result_json = submit_callback_url(
            callback_url=callback_url,
            expected_state=oauth_start.state,
            code_verifier=oauth_start.code_verifier,
            proxy_url=proxy,
        )
        return json.loads(result_json)
    except Exception as e:
        log(f"  OAuth 会话补全异常: {e}")
        return None


def _exchange_callback_code_without_state(callback_url: str, oauth_start, proxy: str | None) -> dict:
    """Codex 简化流偶发 callback 只带 code；此时仍可用 PKCE code_verifier 换 token。"""
    code = _extract_code_from_url(callback_url)
    if not code:
        raise ValueError("callback url missing ?code=")

    from .oauth import OAUTH_TOKEN_URL, _jwt_claims_no_verify, _post_form
    import time as _time

    token_resp = _post_form(
        OAUTH_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": str(getattr(oauth_start, "client_id", "") or ""),
            "code": code,
            "redirect_uri": str(getattr(oauth_start, "redirect_uri", "") or ""),
            "code_verifier": str(getattr(oauth_start, "code_verifier", "") or ""),
        },
        proxy_url=proxy,
    )
    access_token = str(token_resp.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("OAuth token response missing access_token")
    refresh_token = str(token_resp.get("refresh_token") or "").strip()
    id_token = str(token_resp.get("id_token") or "").strip()
    expires_in = int(token_resp.get("expires_in") or 0)
    claims = _jwt_claims_no_verify(id_token)
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    now = int(_time.time())
    return {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": str(auth_claims.get("chatgpt_account_id") or ""),
        "email": str(claims.get("email") or ""),
        "type": "codex",
        "expired": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now + max(expires_in, 0))),
        "last_refresh": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now)),
    }


def _submit_callback_result(callback_url: str, oauth_start, proxy: str | None, log=None) -> dict:
    from .oauth import submit_callback_url

    try:
        result_json = submit_callback_url(
            callback_url=callback_url,
            expected_state=oauth_start.state,
            code_verifier=oauth_start.code_verifier,
            redirect_uri=oauth_start.redirect_uri,
            client_id=oauth_start.client_id,
            proxy_url=proxy,
        )
        return json.loads(result_json)
    except ValueError as exc:
        message = str(exc)
        if "missing ?state=" not in message or not _extract_code_from_url(callback_url):
            raise
        if callable(log):
            log("  OAuth callback 缺少 state，改用 code+PKCE 直接换 token")
        return _exchange_callback_code_without_state(callback_url, oauth_start, proxy)


def _format_callback_exchange_error(exc: Exception) -> str:
    """把 callback code 已拿到但 token 交换失败的原因转成用户可行动提示。"""
    message = str(exc or "").strip() or "unknown error"
    if "unsupported_country_region_territory" in message or "Country, region, or territory not supported" in message:
        return (
            "已捕获 OAuth callback code，但 token 交换失败："
            "OpenAI 返回 unsupported_country_region_territory，当前代理/IP 地区不受支持。"
            "请更换支持地区代理后重试。"
        )
    return f"已捕获 OAuth callback code，但 token 交换失败：{message}"


def _is_transient_callback_exchange_error(exc: Exception) -> bool:
    message = str(exc or "").strip().lower()
    if not message:
        return False
    if "unsupported_country_region_territory" in message or "invalid_grant" in message:
        return False
    if re.search(r"token exchange failed:\s*(429|5\d\d)\b", message):
        return True
    markers = (
        "network error",
        "timeout",
        "timed out",
        "connection",
        "proxy",
        "curl",
        "tls",
        "ssl",
        "reset",
        "temporarily",
        "try again",
    )
    return any(marker in message for marker in markers)


def _submit_callback_result_or_error(callback_url: str, oauth_start, proxy: str | None, log=None) -> dict:
    """callback 是 OAuth 终点；成功返回 token，失败返回错误，不再回退找 workspace。"""
    callback_error = _extract_callback_error_from_url(callback_url)
    if callback_error:
        if callable(log):
            log(f"  OAuth callback 返回 error，需重走授权登录: {callback_error}")
        return _oauth_restart_required_result(callback_url, callback_error)
    if callable(log):
        log(_callback_debug_summary(callback_url, oauth_start, proxy))
        try:
            from .oauth import OAUTH_TOKEN_URL

            log(f"  OAuth token exchange 请求: endpoint={OAUTH_TOKEN_URL}")
        except Exception:
            pass
    last_exc: Exception | None = None
    for attempt in range(1, 7):
        try:
            return _submit_callback_result(callback_url, oauth_start, proxy, log=log)
        except Exception as exc:
            last_exc = exc
            if not _is_transient_callback_exchange_error(exc) or attempt >= 6:
                break
            if callable(log):
                log(f"  OAuth callback token exchange 瞬时失败，准备重试 ({attempt + 1}/6): {exc}")
            time.sleep(min(2 * attempt, 10))
    if callable(log):
        log(f"  OAuth callback token exchange 失败: {last_exc}")
    return {
        "error": _format_callback_exchange_error(last_exc or RuntimeError("unknown token exchange error")),
        "callback_captured": True,
    }


def _wait_for_oauth_callback_result(
    page,
    oauth_start,
    proxy: str | None,
    log,
    *,
    timeout_sec: int = 90,
) -> dict | None:
    """Wait for the browser to land on the localhost OAuth callback and exchange it."""
    deadline = time.time() + max(int(timeout_sec or 0), 1)
    seen_urls: set[str] = set()

    while time.time() < deadline:
        candidates: list[str] = []
        try:
            candidates.append(str(page.url or ""))
        except Exception:
            pass
        try:
            location_href = str(page.evaluate("() => location.href") or "")
            if location_href:
                candidates.append(location_href)
        except Exception:
            pass

        for url in candidates:
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if "localhost" in url or "code=" in url:
                log(f"  OAuth callback wait 检测到 URL: {url}")
            callback_error = _extract_callback_error_from_url(url)
            if callback_error:
                log(f"  OAuth callback wait 检测到 error callback，准备重走授权登录: {callback_error}")
                return _oauth_restart_required_result(url, callback_error)
            if not _extract_code_from_url(url):
                continue
            result = _submit_callback_result_or_error(url, oauth_start, proxy, log=log)
            if result.get("access_token"):
                log("  OAuth callback 已换取 token")
            return result
        time.sleep(0.8)
    return None


def _extract_callback_url_from_exception(exc: Exception) -> str:
    text = str(exc or "")
    if not text:
        return ""
    match = re.search(r"(https?://localhost[^\s\"')]+)", text, flags=re.I)
    if not match:
        return ""
    callback_url = str(match.group(1) or "").strip().rstrip(".,")
    return callback_url if _extract_code_from_url(callback_url) else ""


def _is_add_email_page(page) -> bool:
    current_url = str(getattr(page, "url", "") or "")
    if "add-email" in current_url.lower():
        return True
    if not _find_first_selector(page, EMAIL_INPUT_SELECTORS):
        return False
    try:
        text = str(page.evaluate("() => document.body?.innerText || ''") or "")
    except Exception:
        text = ""
    return bool(
        re.search(
            "add[\\s-]*email|email\\s+address\\s+required|provide\\s+(?:an?\\s+)?email|"
            "\u6dfb\u52a0(?:\u7535\u5b50\u90ae\u4ef6|\u90ae\u7bb1)|"
            "\u7ed1\u5b9a(?:\u7535\u5b50\u90ae\u4ef6|\u90ae\u7bb1)",
            text,
            flags=re.I,
        )
    )


def _derive_oauth_state_from_page(page) -> dict:
    state = _derive_registration_state_from_page(page)
    if state.get("page_type"):
        return state
    current_url = str(page.url or "")
    if _is_add_email_page(page):
        return _build_manual_flow_state("add_email", current_url)
    if _find_first_selector(page, EMAIL_INPUT_SELECTORS):
        return _build_manual_flow_state("login_email", current_url)
    return _extract_flow_state(None, current_url)


OAUTH_EMAIL_SUBMIT_SUCCESS_PAGE_TYPES = {
    "login_password",
    "create_account_password",
    "email_otp_verification",
    "about_you",
    "consent",
    "workspace_selection",
    "organization_selection",
    "add_phone",
    "external_url",
    "oauth_callback",
    "chatgpt_home",
}

OTP_PAGE_RESUMABLE_PAGE_TYPES = OAUTH_EMAIL_SUBMIT_SUCCESS_PAGE_TYPES | {
    "login_email",
    "add_email",
}


def _oauth_login_page_diagnostic(page) -> dict:
    try:
        result = page.evaluate(
            """
            () => {
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'))
                .filter((el) => visible(el))
                .slice(0, 8)
                .map((el) => normalize([el.value, el.textContent, el.getAttribute?.('aria-label'), el.getAttribute?.('title')].filter(Boolean).join(' ')))
                .filter(Boolean);
              const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'))
                .filter((el) => visible(el))
                .slice(0, 8)
                .map((el) => normalize([el.type, el.name, el.id, el.getAttribute?.('autocomplete'), el.getAttribute?.('placeholder')].filter(Boolean).join(':')))
                .filter(Boolean);
              return {
                url: location.href,
                text: normalize(document.body?.innerText || '').slice(0, 240),
                buttons,
                inputs,
              };
            }
            """
        )
        return result if isinstance(result, dict) else {}
    except Exception:
        return {"url": str(page.url or ""), "text": "", "buttons": [], "inputs": []}


def _submit_login_email_form_fallback(page, input_selector: str) -> str:
    try:
        return str(
            page.evaluate(
                """
                (selector) => {
                  const input = document.querySelector(selector);
                  if (!input) return '';
                  const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                  const form = input.form || input.closest?.('form');
                  const root = form || document;
                  const buttons = Array.from(root.querySelectorAll('button[type="submit"], input[type="submit"], button'));
                  let target = buttons.find((el) => {
                    const label = normalize([el.value, el.textContent, el.getAttribute?.('aria-label'), el.getAttribute?.('title')].filter(Boolean).join(' ')).toLowerCase();
                    return visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true' && /continue|next|submit|log\\s*in|sign\\s*in|继续|下一步|ログイン|続ける|次へ/i.test(label);
                  });
                  if (!target) {
                    target = buttons.find((el) => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true');
                  }
                  if (form && target && typeof form.requestSubmit === 'function') {
                    form.requestSubmit(target);
                    return 'requestSubmit(button)';
                  }
                  if (target) {
                    target.click();
                    return 'click(visible-button)';
                  }
                  input.focus?.();
                  for (const type of ['keydown', 'keypress', 'keyup']) {
                    input.dispatchEvent(new KeyboardEvent(type, {
                      key: 'Enter',
                      code: 'Enter',
                      bubbles: true,
                      cancelable: true,
                    }));
                  }
                  if (form && typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                    return 'requestSubmit(form)';
                  }
                  if (form && typeof form.submit === 'function') {
                    form.submit();
                    return 'submit(form)';
                  }
                  return 'keyboard-enter';
                }
                """,
                input_selector,
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _wait_for_login_email_transition(page, start_url: str, log, *, timeout: int = 20) -> dict:
    deadline = time.time() + max(int(timeout or 0), 1)
    last_url = str(page.url or start_url or "")
    last_text = ""
    while time.time() < deadline:
        current_url = str(page.url or "")
        last_url = current_url or last_url
        retry_state = _auth_timeout_retry_page_state(page, path_patterns=[r"/log-in(?:[/?#]|$)", r"/email-verification(?:[/?#]|$)"])
        if retry_state.get("retryPage"):
            last_text = str(retry_state.get("text") or "")
            recovery = _recover_auth_timeout_retry_page(
                page,
                log,
                path_patterns=[r"/log-in(?:[/?#]|$)", r"/email-verification(?:[/?#]|$)"],
            )
            if recovery.get("recovered"):
                time.sleep(0.8)
                state = _derive_oauth_state_from_page(page)
                page_type = str(state.get("page_type") or "")
                if page_type and page_type != "login_email":
                    return {"ok": True, "status": 200, "url": str(page.url or ""), "data": None, "text": ""}
                break
            return {
                "ok": False,
                "status": 0,
                "url": str(recovery.get("url") or current_url),
                "data": None,
                "text": str(recovery.get("text") or "OpenAI auth retry page recovery failed"),
            }

        if _click_passwordless_login_if_available(page, log, context="OAuth email page after submit"):
            time.sleep(0.5)
            continue
        state = _derive_oauth_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in OAUTH_EMAIL_SUBMIT_SUCCESS_PAGE_TYPES:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if current_url != start_url and page_type != "login_email":
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        error_text = _extract_auth_error_text(page)
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": last_text or "OAuth email page submit did not transition"}


def _submit_login_email_via_page(page, email: str, log, *, recover_url: str = "") -> dict:
    start_url = str(page.url or "")
    last_url = start_url
    last_text = ""

    for submit_attempt in range(1, 4):
        start_url = str(page.url or start_url or "")
        input_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=15)
        if not input_selector:
            retry_state = _auth_timeout_retry_page_state(page, path_patterns=[r"/log-in(?:[/?#]|$)", r"/email-verification(?:[/?#]|$)"])
            if retry_state.get("retryPage"):
                recovery = _recover_auth_timeout_retry_page(
                    page,
                    log,
                    path_patterns=[r"/log-in(?:[/?#]|$)", r"/email-verification(?:[/?#]|$)"],
                )
                if recovery.get("recovered"):
                    continue
            raise RuntimeError("OAuth 邮箱页未找到输入框")
        if not _fill_input_like_user(page, input_selector, email):
            raise RuntimeError("OAuth 邮箱页填写失败")
        log(f"OAuth 邮箱页输入框: {input_selector}")
        _browser_pause(page)

        submit_selector = _click_first_no_wait(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
        if submit_selector:
            log(f"OAuth 邮箱页已点击继续按钮: {submit_selector}")
        elif _submit_form_with_fallback(page, input_selector):
            log("OAuth 邮箱页未找到可点击 Continue，已使用表单 fallback 提交")
        else:
            raise RuntimeError("OAuth 邮箱页未找到 Continue 按钮")

        transition = _wait_for_login_email_transition(page, start_url, log, timeout=20)
        last_url = str(transition.get("url") or last_url)
        last_text = str(transition.get("text") or last_text)
        if transition.get("ok") or int(transition.get("status") or 0) >= 400:
            return transition

        fallback_method = _submit_login_email_form_fallback(page, input_selector)
        if fallback_method:
            log(f"OAuth email page fallback submit after no transition: {fallback_method}")
            transition = _wait_for_login_email_transition(page, start_url, log, timeout=12)
            last_url = str(transition.get("url") or last_url)
            last_text = str(transition.get("text") or last_text)
            if transition.get("ok") or int(transition.get("status") or 0) >= 400:
                return transition

        diag = _oauth_login_page_diagnostic(page)
        diag_text = str(diag.get("text") or "").replace("\n", " ")[:180]
        log(
            "OAuth email page still on login page after submit "
            f"attempt={submit_attempt}/3 url={str(diag.get('url') or last_url)[:160]} "
            f"buttons={diag.get('buttons') or []} inputs={diag.get('inputs') or []} "
            f"text={diag_text}"
        )
        if recover_url and submit_attempt < 3:
            try:
                log("OAuth email page retry: reopening current OAuth authorize URL before next submit")
                _goto_with_retry(page, recover_url, wait_until="domcontentloaded", timeout=30000, log=log)
                time.sleep(1)
            except Exception as exc:
                log(f"OAuth email page retry: reopen authorize URL failed: {exc}")
        log(f"OAuth 邮箱页提交后未跳转，准备重试提交 ({submit_attempt}/3)")

    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": last_text or "OAuth 邮箱页提交后未跳转"}


def _do_codex_oauth(
    page,
    cookies_dict: dict,
    email: str,
    password: str,
    otp_callback,
    phone_callback,
    proxy: str | None,
    log,
    *,
    allow_add_phone_retry: bool = True,
    max_phone_attempts: int | None = None,
    oauth_start=None,
) -> dict | None:
    """在真实浏览器会话内完成 Codex OAuth，返回完整 token 包。

    如果传入 ``oauth_start``，则使用预生成的 OAuth 参数（重用 state/code_verifier），
    这样外层可以用同一 code_verifier 完成 token 交换（fallback 场景）。
    """
    from .oauth import generate_oauth_url
    from .constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE

    if oauth_start is None:
        oauth_start = generate_oauth_url(
            redirect_uri=CODEX_REDIRECT_URI,
            scope=CODEX_SCOPE,
            client_id=CODEX_CLIENT_ID,
        )
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip() or _random_chrome_ua()
    except Exception:
        user_agent = _random_chrome_ua()
    device_id = str(cookies_dict.get("oai-did") or uuid.uuid4())
    log(f"  Codex OAuth 授权链接: {oauth_start.auth_url}")
    log(f"  OAuth state={oauth_start.state[:20]}...")
    log(_oauth_authorize_debug_summary(oauth_start, proxy))
    resume_auth_url = _oauth_resume_url_after_phone(oauth_start.auth_url)
    oauth_restart_count = 0
    try:
        add_phone_attempt_limit = max(
            1,
            int(max_phone_attempts or PHONE_ATTEMPTS_PER_COUNTRY * PHONE_MAX_COUNTRIES),
        )
    except Exception:
        add_phone_attempt_limit = PHONE_ATTEMPTS_PER_COUNTRY * PHONE_MAX_COUNTRIES

    def _restart_oauth_login_from_error(callback_url: str, reason: str) -> bool:
        """callback?error 代表当前授权链断开，重建 PKCE/state 后重新登录授权。"""
        nonlocal oauth_start, resume_auth_url, oauth_restart_count
        if oauth_restart_count >= 2:
            log(f"  OAuth callback error 重试已达上限，停止重走授权: {reason}")
            return False
        oauth_restart_count += 1
        oauth_start = generate_oauth_url(
            redirect_uri=CODEX_REDIRECT_URI,
            scope=CODEX_SCOPE,
            client_id=CODEX_CLIENT_ID,
        )
        resume_auth_url = _oauth_resume_url_after_phone(oauth_start.auth_url)
        log(
            f"  OAuth callback error={reason}，重新走授权登录流程 "
            f"({oauth_restart_count}/2): {str(callback_url or '')}"
        )
        log(f"  Codex OAuth 授权链接(重启): {oauth_start.auth_url}")
        log(f"  OAuth state(重启)={oauth_start.state[:20]}...")
        log(_oauth_authorize_debug_summary(oauth_start, proxy))
        try:
            _goto_with_retry(page, oauth_start.auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
            return True
        except Exception as exc:
            log(f"  OAuth 重走授权导航失败: {exc}")
            return False

    def _maybe_restart_oauth_from_result(result: dict | None) -> bool | None:
        if not isinstance(result, dict) or not result.get("oauth_restart_required"):
            return None
        if _restart_oauth_login_from_error(
            str(result.get("callback_url") or ""),
            str(result.get("error") or "OAuth callback error"),
        ):
            return True
        return False

    try:
        try:
            _goto_with_retry(page, oauth_start.auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
        except Exception as exc:
            callback_url = _extract_callback_url_from_exception(exc)
            if callback_url:
                log(f"  OAuth bootstrap 直接捕获 callback: {callback_url}")
                return _submit_callback_result_or_error(callback_url, oauth_start, proxy, log=log)
            raise

        current_url = str(page.url or "")
        log(f"  OAuth bootstrap -> {current_url}")

        for step in range(20):
            state = _derive_oauth_state_from_page(page)
            current_url = str(page.url or "")
            next_url = str(state.get("continue_url") or "").strip()
            log(
                f"  OAuth state step[{step+1}/20]: "
                f"page={state.get('page_type') or '-'} next={next_url}"
                f" url={current_url}"
            )

            callback_url = ""
            if _extract_code_from_url(current_url):
                callback_url = current_url
            elif _extract_code_from_url(next_url):
                callback_url = next_url
            if callback_url:
                return _submit_callback_result_or_error(callback_url, oauth_start, proxy, log=log)

            callback_error = _extract_callback_error_from_url(current_url) or _extract_callback_error_from_url(next_url)
            if callback_error:
                error_url = current_url if _extract_callback_error_from_url(current_url) else next_url
                if _restart_oauth_login_from_error(error_url, callback_error):
                    continue
                return {"error": f"OAuth callback error 后重走授权失败: {callback_error}"}

            page_oauth_url = _get_page_oauth_url(page)
            if (
                page_oauth_url
                and page_oauth_url != current_url
                and _oauth_url_matches_state(page_oauth_url, oauth_start.state)
            ):
                log("  OAuth 页面检测到更新的授权链接，跟随页面授权链接...")
                _goto_with_retry(page, page_oauth_url, wait_until="domcontentloaded", timeout=30000, log=log)
                continue

            if state["page_type"] == "login_email":
                log("  OAuth 页面需要邮箱登录，提交邮箱...")
                email_resp = _submit_login_email_via_page(page, email, log, recover_url=oauth_start.auth_url)
                log(f"  OAuth 邮箱页提交状态: {email_resp.get('status', 0)}")
                if not email_resp.get("ok"):
                    raise RuntimeError(f"OAuth 邮箱页提交失败: {(email_resp.get('text') or '')}")
                continue

            if state["page_type"] == "add_email":
                log("  OAuth requires binding email; submitting mailbox address...")
                email_resp = _submit_login_email_via_page(page, email, log, recover_url=oauth_start.auth_url)
                log(f"  OAuth add-email submit status: {email_resp.get('status', 0)}")
                if not email_resp.get("ok"):
                    raise RuntimeError(f"OAuth add-email submit failed: {(email_resp.get('text') or '')}")
                continue

            if state["page_type"] in {"login_password", "create_account_password"}:
                log("  OAuth 页面需要密码登录，提交密码...")
                # OAuth 流程中直接填密码登录，不尝试恢复到注册态
                password_resp = _submit_oauth_password_direct(page, password, log)
                log(f"  OAuth 密码页提交状态: {password_resp.get('status', 0)}")
                if not password_resp.get("ok"):
                    raise RuntimeError(f"OAuth 密码页提交失败: {(password_resp.get('text') or '')}")
                continue

            if state["page_type"] == "email_otp_verification":
                if not otp_callback:
                    log("  ⚠️ OAuth 需要邮箱 OTP 但没有 otp_callback")
                    return None
                log("  OAuth 等待邮箱验证码...")
                otp_resp = _submit_email_otp_with_retry(
                    page,
                    otp_callback,
                    log,
                    max_invalid_retries=3,
                    max_transient_retries=6,
                    label="OAuth email OTP",
                    recover_url=oauth_start.auth_url,
                )
                log(f"  OAuth 验证码页提交状态: {otp_resp.get('status', 0)}")
                if not otp_resp.get("ok"):
                    raise RuntimeError(f"OAuth 验证码校验失败: {(otp_resp.get('text') or '')}")
                continue

            if state["page_type"] == "about_you":
                log("  OAuth 页面出现 about_you，继续页面填写...")
                about_resp = _submit_about_you_via_page(page, log)
                log(f"  OAuth about_you 提交状态: {about_resp.get('status', 0)}")
                if not about_resp.get("ok"):
                    raise RuntimeError(f"OAuth about_you 提交失败: {(about_resp.get('text') or '')}")
                continue

            if state["page_type"] in {"consent", "workspace_selection", "organization_selection", "external_url"}:
                browser_result = _complete_oauth_in_browser(page, oauth_start, proxy, log)
                if browser_result:
                    restart_decision = _maybe_restart_oauth_from_result(browser_result)
                    if restart_decision is True:
                        continue
                    if restart_decision is False:
                        return {"error": str(browser_result.get("error") or "OAuth callback error")}
                    return browser_result
                cookies_dict = _get_cookies(page)
                session_result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
                if session_result:
                    return session_result
                log("  ⚠️ 页面已到 consent/workspace，但会话补全失败")
                return None

            if state["page_type"] == "add_phone":
                if phone_callback:
                    log("  OAuth 检测到 add_phone，优先执行短信验证...")
                    phone_verification_completed = False
                    try:
                        phone_state = _handle_add_phone_challenge(
                            page, phone_callback,
                            device_id=device_id, user_agent=user_agent,
                            log=log, resume_url=resume_auth_url or oauth_start.auth_url,
                            max_phone_attempts=add_phone_attempt_limit,
                        )
                        phone_verification_completed = bool(getattr(phone_callback, "completed", False))
                        for candidate_url in (
                            str(page.url or ""),
                            str((phone_state or {}).get("continue_url") or ""),
                            str((phone_state or {}).get("current_url") or ""),
                        ):
                            if _extract_code_from_url(candidate_url):
                                log("  手机验证后已到 OAuth callback，开始换 token")
                                return _submit_callback_result_or_error(candidate_url, oauth_start, proxy, log=log)
                        if (phone_state or {}).get("page_type") in {"consent", "workspace_selection", "organization_selection", "external_url"}:
                            browser_result = _complete_oauth_in_browser(page, oauth_start, proxy, log)
                            if browser_result:
                                restart_decision = _maybe_restart_oauth_from_result(browser_result)
                                if restart_decision is True:
                                    continue
                                if restart_decision is False:
                                    return {"error": str(browser_result.get("error") or "OAuth callback error")}
                                return browser_result
                        callback_result = _wait_for_oauth_callback_result(
                            page,
                            oauth_start,
                            proxy,
                            log,
                            timeout_sec=15,
                        )
                        if callback_result:
                            restart_decision = _maybe_restart_oauth_from_result(callback_result)
                            if restart_decision is True:
                                continue
                            if restart_decision is False:
                                return {"error": str(callback_result.get("error") or "OAuth callback error")}
                            return callback_result
                        if resume_auth_url:
                            log("  手机验证后未捕获 callback，重访 OAuth(prompt=none) 承接授权...")
                            _goto_with_retry(page, resume_auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
                            callback_result = _wait_for_oauth_callback_result(
                                page,
                                oauth_start,
                                proxy,
                                log,
                                timeout_sec=20,
                            )
                            if callback_result:
                                restart_decision = _maybe_restart_oauth_from_result(callback_result)
                                if restart_decision is True:
                                    continue
                                if restart_decision is False:
                                    return {"error": str(callback_result.get("error") or "OAuth callback error")}
                                return callback_result
                            post_phone_state = _derive_oauth_state_from_page(page)
                            if post_phone_state.get("page_type") in {"consent", "workspace_selection", "organization_selection", "external_url"}:
                                browser_result = _complete_oauth_in_browser(page, oauth_start, proxy, log)
                                if browser_result:
                                    restart_decision = _maybe_restart_oauth_from_result(browser_result)
                                    if restart_decision is True:
                                        continue
                                    if restart_decision is False:
                                        return {"error": str(browser_result.get("error") or "OAuth callback error")}
                                    return browser_result
                            if post_phone_state.get("page_type") == "login_email":
                                log("  OAuth returned to login_email after phone verification; continue relogin flow")
                        continue
                    except Exception as exc:
                        if phone_verification_completed or bool(getattr(phone_callback, "completed", False)):
                            log(f"  手机验证已成功，后续 OAuth 承接异常，继续状态机重试: {exc}")
                            continue
                        log(f"  短信验证失败，停止 OAuth 流程: {exc}")
                        error_msg = str(exc)
                        if PHONE_REJECTED_SENTINEL in error_msg or _is_retryable_phone_rejection_text(error_msg):
                            return {"error": error_msg, "error_type": "phone_rejected_retryable"}
                        return None

                if not allow_add_phone_retry:
                    log("  OAuth 检测到 add_phone，等待手动完成手机号验证并跳转 callback...")
                    callback_result = _wait_for_oauth_callback_result(
                        page,
                        oauth_start,
                        proxy,
                        log,
                        timeout_sec=180,
                    )
                    if callback_result:
                        restart_decision = _maybe_restart_oauth_from_result(callback_result)
                        if restart_decision is True:
                            continue
                        if restart_decision is False:
                            return {"error": str(callback_result.get("error") or "OAuth callback error")}
                        return callback_result
                    return {"error": "OpenAI OAuth 要求手机号验证，等待后未捕获 callback URL"}

                # 先尝试跳过 add_phone，直接重新访问 OAuth 授权 URL
                # 用户已登录，重新访问 auth URL 应该能直接跳到 callback
                log("  检测到 add_phone，尝试跳过...")
                try:
                    _goto_with_retry(page, resume_auth_url or oauth_start.auth_url, wait_until="domcontentloaded", timeout=15000, log=log)
                    time.sleep(2)
                    current_url = str(page.url or "")

                    # 检查是否直接拿到了 callback
                    callback_url = ""
                    if "code=" in current_url:
                        callback_url = current_url
                    else:
                        # 可能需要跟随重定向
                        for _ in range(5):
                            time.sleep(1)
                            current_url = str(page.url or "")
                            if "code=" in current_url:
                                callback_url = current_url
                                break

                    if callback_url:
                        log("  [OK] 成功跳过 add_phone，获取到 OAuth callback")
                        return _submit_callback_result_or_error(callback_url, oauth_start, proxy, log=log)

                    # 检查页面状态
                    skip_state = _derive_registration_state_from_page(page)
                    if skip_state.get("page_type") in {"consent", "workspace_selection", "organization_selection"}:
                        log("  [OK] 跳过 add_phone 到达 consent 页面")
                        # 尝试在浏览器里完成 consent 流程
                        browser_result = _complete_oauth_in_browser(page, oauth_start, proxy, log)
                        if browser_result:
                            restart_decision = _maybe_restart_oauth_from_result(browser_result)
                            if restart_decision is True:
                                continue
                            if restart_decision is False:
                                return {"error": str(browser_result.get("error") or "OAuth callback error")}
                            return browser_result
                        # 回退到 curl session 方式
                        cookies_dict = _get_cookies(page)
                        session_result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
                        if session_result:
                            return session_result

                    if skip_state.get("page_type") == "add_phone":
                        log("  跳过失败，仍在 add_phone 页面")
                    else:
                        log(f"  跳过后页面状态: {skip_state.get('page_type') or '-'}")
                        # 继续状态机循环
                        continue

                except Exception as exc:
                    callback_url = _extract_callback_url_from_exception(exc)
                    if callback_url:
                        return _submit_callback_result_or_error(callback_url, oauth_start, proxy, log=log)
                    log(f"  跳过 add_phone 异常: {exc}")

                log("  ⚠️ add_phone 无法跳过且无可用接码服务")
                return None

            # chatgpt_home: 页面可能正在 JS 重定向（如跳转到 add-phone）
            # 等待更长时间让重定向完成
            if state["page_type"] == "chatgpt_home":
                # 检查是否是错误页面
                if "error" in current_url:
                    error_msg = current_url.split("error=")[-1].split("&")[0] if "error=" in current_url else "unknown"
                    log(f"  OAuth 错误页面: {error_msg} url={current_url}")
                    raise RuntimeError(f"OpenAI OAuth 错误: {error_msg}")
                time.sleep(2)
                new_url = str(page.url or "")
                if new_url != current_url:
                    continue
                # 检查 cookie 里是否有 session
                cookies_dict = _get_cookies(page)
                for ck, cv in cookies_dict.items():
                    if "session" in ck.lower() and cv:
                        log(f"  chatgpt_home 检测到 session cookie: {ck}")
                        session_result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
                        if session_result:
                            return session_result
                        break
                continue

            target_url = _normalize_url(state.get("continue_url") or "", OPENAI_AUTH)
            if target_url and target_url != current_url:
                try:
                    _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
                except Exception as exc:
                    callback_url = _extract_callback_url_from_exception(exc)
                    if callback_url:
                        return _submit_callback_result_or_error(callback_url, oauth_start, proxy, log=log)
                    log(f"  OAuth navigation failed: {exc}")
                    break
                continue

            error_text = _extract_auth_error_text(page)
            if error_text:
                raise RuntimeError(f"OAuth 页面错误: {error_text}")
            time.sleep(0.5)
    except Exception as e:
        log(f"  OAuth 异常: {e}")
        return None

    cookies_dict = _get_cookies(page)
    result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
    if result:
        return result

    session_token = cookies_dict.get("__Secure-next-auth.session-token", "")
    if not session_token:
        log("  ⚠️ 无 session_token，OAuth 失败")
        return None
    log("  ⚠️ 完整 OAuth 失败，回退 session access_token")
    return None


def _wait_for_access_token(page, timeout: int = 60) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = page.evaluate("""
            async () => {
                const r = await fetch('/api/auth/session');
                const j = await r.json();
                return j.accessToken || '';
            }
            """)
            if r:
                return r
        except Exception:
            pass
        time.sleep(2)
    return ""


def _is_registration_complete(state: dict) -> bool:
    page_type = str(state.get("page_type") or "")
    url = str(state.get("current_url") or state.get("continue_url") or "").lower()
    return page_type in {"callback", "oauth_callback"} or (
        "chatgpt.com" in url
        and "redirect_uri" not in url
        and "about-you" not in url
        and "/api/auth/session" in url
    )


def _handle_post_signup_onboarding(page, log) -> None:
    current_url = str(page.url or "")
    if "chatgpt.com" not in current_url:
        return
    try:
        # 可能弹出 persistent storage 提示，优先点 Allow，不影响主流程也可点 Block。
        allow_selector = _click_first(
            page,
            [
                'button:has-text("Allow")',
                'button:has-text("allow")',
                'button:has-text("Block")',
                'button:has-text("block")',
                'button:has-text("許可")',
                'button:has-text("ブロック")',
                'button:has-text("拒否")',
            ],
            timeout=1,
        )
        if allow_selector:
            log(f"已处理浏览器弹窗: {allow_selector}")
    except Exception:
        pass

    # 新账号常见 onboarding 问卷页，优先 Skip。
    try:
        if page.locator("text=What brings you to ChatGPT?").first.count() > 0:
            skip_selector = _click_first(
                page,
                [
                    'button:has-text("Skip")',
                    'button:has-text("skip")',
                    'button:has-text("Next")',
                    'button:has-text("next")',
                    'button:has-text("スキップ")',
                    'button:has-text("次へ")',
                ],
                timeout=5,
            )
            if skip_selector:
                log(f"已处理 onboarding 页面: {skip_selector}")
                _browser_pause(page)
    except Exception:
        pass


def _is_password_registration(state: dict) -> bool:
    return str(state.get("page_type") or "") in {"create_account_password", "password"}


def _is_email_otp(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "email_otp_verification" or "email-verification" in target or "email-otp" in target


def _is_about_you(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "about_you" or "about-you" in target


def _is_add_phone(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "add_phone" or "add-phone" in target


def _mask_phone_number(phone_number: str) -> str:
    text = str(phone_number or "").strip()
    if len(text) <= 4:
        return text
    if text.startswith("+"):
        digits = text[1:]
        if len(digits) <= 8:
            return f"+{digits[:2]}****{digits[-2:]}"
        return f"+{digits[:4]}****{digits[-2:]}"
    if len(text) <= 8:
        return f"{text[:2]}****{text[-2:]}"
    return f"{text[:4]}****{text[-2:]}"


def _is_invalid_phone_otp_response(result: dict) -> bool:
    status = int((result or {}).get("status") or 0)
    if status != 400:
        return False
    data = (result or {}).get("data")
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").lower()
            code = str(error.get("code") or "").lower()
            return code == "invalid_input" and "invalid otp code" in message
    text = str((result or {}).get("text") or "").lower()
    return "invalid otp code" in text


def _resolve_add_phone_attempt_limit(phone_callback, default_limit: int) -> int:
    """根据接码来源决定 add_phone 拒号换号上限。"""
    limit = max(1, int(default_limit or 1))
    provider_key = str(getattr(phone_callback, "provider_key", "") or "").strip().lower()
    if provider_key in {"codex_sms_pool", "codex_sms_pool_api", "chatgpt-api", "chatgpt_api"}:
        try:
            from core.base_sms import parse_codex_sms_pool_entries

            config = getattr(phone_callback, "config", {}) or {}
            pool_text = str(
                config.get("codex_sms_pool_text")
                or config.get("codex_sms_pool")
                or config.get("chatGptApiSmsPoolText")
                or ""
            )
            pool_size = len(parse_codex_sms_pool_entries(pool_text))
            if pool_size > 0:
                return pool_size
        except Exception:
            return limit
    hook = getattr(phone_callback, "get_add_phone_attempt_limit", None)
    if callable(hook):
        try:
            return max(1, int(hook(limit) or limit))
        except Exception:
            return limit
    return limit


def _handle_add_phone_challenge(
    page,
    phone_callback,
    *,
    device_id: str,
    user_agent: str,
    log,
    resume_url: str = "",
    max_phone_attempts: int = PHONE_ATTEMPTS_PER_COUNTRY * PHONE_MAX_COUNTRIES,
) -> dict:
    """在 add-phone 页面通过 UI 交互完成手机号验证。

    流程: 选择国家 -> 输入本地号码 -> 点击发送 -> 填写 OTP -> 点击验证。
    号码被拒时自动换号；进入验证码页后 60 秒无短信则跳过当前账号。
    """
    if not phone_callback:
        raise RuntimeError(
            "ChatGPT 注册遇到手机号验证，但未配置 phone_callback。"
            "请在 RegisterConfig.extra 中配置接码服务，或手动完成手机验证。"
        )

    attempt_limit = _resolve_add_phone_attempt_limit(phone_callback, max_phone_attempts)
    last_error = None
    for phone_attempt in range(attempt_limit):
        if phone_attempt > 0:
            log(f"换号重试第 {phone_attempt + 1}/{attempt_limit} 次...")
            # 回到 add-phone 页面
            try:
                _goto_with_retry(page, f"{OPENAI_AUTH}/add-phone", wait_until="domcontentloaded", timeout=15000, log=log)
                time.sleep(1)
            except Exception:
                pass

        try:
            result = _do_add_phone_attempt(
                page, phone_callback,
                device_id=device_id, user_agent=user_agent,
                log=log, resume_url=resume_url,
            )
            return result
        except RuntimeError as exc:
            last_error = exc
            error_msg = str(exc)
            if PHONE_CODE_TIMEOUT_SENTINEL in error_msg:
                log(f"⚠️ 短信验证码 {PHONE_CODE_TIMEOUT_SECONDS}s 未到达，跳过当前账号")
                if hasattr(phone_callback, "cleanup"):
                    phone_callback.cleanup()
                raise

            # 号码已被使用、虚拟号/不支持等页面拒号时换号重试，其他错误直接抛出
            should_retry = (
                PHONE_REJECTED_SENTINEL in error_msg
                or _is_retryable_phone_rejection_text(error_msg)
                or "phone_number_in_use" in error_msg
                or "already" in error_msg.lower()
                or "in use" in error_msg.lower()
            )
            if not should_retry:
                raise
            log(f"⚠️ 当前手机号不可用，准备换号重试: {error_msg}")
            # 取消当前号码
            if hasattr(phone_callback, "cleanup"):
                phone_callback.cleanup()
            # 重置 phone_callback 状态为 need_number
            if hasattr(phone_callback, "phase"):
                phone_callback.phase = "need_number"
                phone_callback.activation = None
                phone_callback.completed = False

    raise last_error or RuntimeError("短信验证失败: 多次换号均未收到验证码")


def _do_add_phone_attempt(
    page,
    phone_callback,
    *,
    device_id: str,
    user_agent: str,
    log,
    resume_url: str = "",
) -> dict:
    """单次手机号验证尝试（内部函数）。"""

    # 保留 HTTP resend 回调供 SMS provider 内部使用
    referer = _normalize_url(str(page.url or ""), OPENAI_AUTH) or f"{OPENAI_AUTH}/add-phone"
    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=referer,
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )

    def _request_openai_resend():
        # 浏览器模式下只通过页面 UI 点击 Resend 按钮
        resend_clicked = _click_first_no_wait(page, [
            'button:has-text("Resend")',
            'button:has-text("resend")',
            'button:has-text("Resend code")',
            'button[data-testid="resend-link"]',
            'button:has-text("重新发送")',
            'a:has-text("Resend")',
            'a:has-text("resend")',
            'a:has-text("Resend code")',
            'button:has-text("再送信")',
            'button:has-text("コードを再送")',
            'a:has-text("再送信")',
            'a:has-text("コードを再送")',
        ], timeout=3)
        if resend_clicked:
            log(f"  phone-otp/resend -> 已点击页面 Resend 按钮: {resend_clicked}")
        else:
            log("  phone-otp/resend -> 页面未找到 Resend 按钮，跳过（浏览器模式不走 HTTP）")

    if hasattr(phone_callback, "set_resend_callback"):
        phone_callback.set_resend_callback(_request_openai_resend)
    if hasattr(phone_callback, "set_code_timeout"):
        phone_callback.set_code_timeout(PHONE_CODE_TIMEOUT_SECONDS)

    def _raise_phone_send_failed(reason: str):
        # 统一上报接码 provider，令本地号池/第三方 provider 能把当前号标失败。
        text = str(reason or "").strip() or "手机号提交失败"
        if hasattr(phone_callback, "mark_send_failed"):
            phone_callback.mark_send_failed(text)
        if _is_retryable_phone_rejection_text(text):
            raise RuntimeError(f"{PHONE_REJECTED_SENTINEL}: {text}")
        raise RuntimeError(f"手机号提交失败: {text}")

    # ---- 第1步: 获取手机号 ----
    log("注册流程已进入 add_phone，开始准备租号并接收短信验证码...")
    phone_number = str(phone_callback() or "").strip()
    if not phone_number:
        raise RuntimeError("未获取到手机号")
    log(f"检测到 add_phone，提交手机号(UI): {_mask_phone_number(phone_number)}")

    # 解析国家拨号码和本地号码
    dial_code, local_number, country_name = _parse_phone_country_and_local(phone_number)
    log(f"  解析号码: 国家={country_name or '未知'} 拨号码=+{dial_code} 本地号={local_number[:4]}...")

    # 确保在 add-phone 页面
    current_url = str(page.url or "")
    if "add-phone" not in current_url:
        _goto_with_retry(page, f"{OPENAI_AUTH}/add-phone", wait_until="domcontentloaded", timeout=30000, log=log)
    time.sleep(1)

    country_selected = False
    if dial_code or country_name:
        country_selected = _select_phone_country_ui(page, dial_code, country_name, log)
        if not country_selected:
            log("  国家区号选择未确认，将尝试用完整手机号提交")

    submit_result = _submit_add_phone_dom(
        page,
        phone_number=phone_number,
        dial_code=dial_code,
        local_number=local_number,
        country_name=country_name,
        log=log,
    )
    if not submit_result.get("ok"):
        log(f"  add-phone DOM 提交失败，回退旧 UI 路径: {submit_result.get('reason') or submit_result}")
        if not country_selected:
            country_selected = _select_phone_country_ui(page, dial_code, country_name, log)
        _browser_pause(page)
        phone_input_sel = _wait_for_any_selector(page, PHONE_INPUT_SELECTORS, timeout=10)
        if not phone_input_sel:
            raise RuntimeError("未找到手机号输入框")
        fill_value = local_number if local_number else phone_number
        if not _fill_input_like_user(page, phone_input_sel, fill_value):
            raise RuntimeError(f"手机号输入框填写失败: {phone_input_sel}")
        if dial_code and not _phone_input_matches_expected(page, phone_input_sel, dial_code, local_number):
            raise RuntimeError("手机号输入框包含重复区号或非预期值")
        send_sel = _click_first_no_wait(page, PHONE_SEND_SELECTORS, timeout=8)
        if send_sel:
            log(f"  已点击发送按钮: {send_sel}")
        elif _submit_form_with_fallback(page, phone_input_sel):
            log("  未找到发送按钮，已使用表单 fallback 提交")
        else:
            raise RuntimeError("未找到发送验证码按钮")

    phone_status = _wait_for_phone_verification_ready(page, timeout=30)
    if phone_status.get("addPhoneError"):
        _raise_phone_send_failed(str(phone_status.get("addPhoneError") or ""))
    if not phone_status.get("phoneVerificationReady"):
        error_text = _extract_auth_error_text(page)
        if error_text:
            _raise_phone_send_failed(error_text)
        page_text = str(phone_status.get("text") or _get_visible_page_text(page) or "")
        if _is_retryable_phone_rejection_text(page_text):
            _raise_phone_send_failed(page_text)
        raise RuntimeError(f"手机号提交后未进入验证码页: {str(phone_status.get('url') or page.url)}")

    # 检查发送是否成功（页面应出现 OTP 输入框或 URL 变化）
    error_text = _extract_auth_error_text(page)
    if error_text:
        _raise_phone_send_failed(error_text)

    if hasattr(phone_callback, "mark_send_succeeded"):
        phone_callback.mark_send_succeeded()
    log("手机号提交成功(UI)，开始等待短信验证码...")

    # ---- 第5步: 等待 SMS 验证码并在页面 OTP 输入框中填写 ----
    for code_attempt in range(3):
        try:
            sms_code = str(phone_callback() or "").strip()
        except RuntimeError as exc:
            message = str(exc)
            if "timeout" in message.lower() or "超时" in message:
                raise RuntimeError(f"{PHONE_CODE_TIMEOUT_SENTINEL}: 等待短信验证码超过 {PHONE_CODE_TIMEOUT_SECONDS}s") from exc
            raise
        if not sms_code:
            raise RuntimeError(f"{PHONE_CODE_TIMEOUT_SENTINEL}: 未获取到短信验证码")

        phone_status = _wait_for_phone_verification_ready(page, timeout=12)
        if not phone_status.get("phoneVerificationReady"):
            raise RuntimeError(f"未找到短信验证码输入框: {str(phone_status.get('url') or page.url)}")

        otp_resp = _submit_phone_otp_dom(page, sms_code, log)
        if not otp_resp.get("ok") and "missing_phone_verification" in str(otp_resp.get("text") or ""):
            otp_resp = _submit_otp_via_page(page, sms_code, log)
        otp_status = int(otp_resp.get("status") or 0)
        log(f"  phone-otp 页面提交状态: {otp_status}")

        if otp_resp.get("ok") or otp_status in (200, 201, 204):
            if hasattr(phone_callback, "report_success"):
                phone_callback.report_success()
            # 等待页面跳转
            time.sleep(1.5)
            state = _extract_flow_state(
                otp_resp.get("data"),
                otp_resp.get("url", page.url),
            )
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            next_url = _normalize_url(resume_url, OPENAI_AUTH) if resume_url else ""
            if next_url:
                try:
                    _goto_with_retry(page, next_url, wait_until="domcontentloaded", timeout=30000, log=log)
                except Exception as exc:
                    log(f"  手机验证码已提交成功，OAuth 承接跳转失败，继续后续状态机: {exc}")
                    return _derive_registration_state_from_page(page) or state
                return _extract_flow_state(None, page.url)
            return state

        # 检查是否是无效验证码
        page_error = _extract_auth_error_text(page)
        if page_error and any(kw in page_error.lower() for kw in ("invalid", "incorrect", "wrong", "expired")):
            log(f"短信验证码被判定无效: {page_error}，继续等待下一条...")
            if hasattr(phone_callback, "mark_code_failed"):
                phone_callback.mark_code_failed(page_error or "invalid otp code")
            continue

        if hasattr(phone_callback, "mark_code_failed"):
            phone_callback.mark_code_failed(page_error or f"status {otp_status}")
        raise RuntimeError(f"短信验证码校验失败: {page_error if page_error else f'status {otp_status}'}")

    raise RuntimeError("短信验证码校验失败: 多次验证码均无效或未通过")


def _requires_registration_navigation(state: dict) -> bool:
    if str(state.get("method") or "GET").upper() != "GET":
        return False
    if str(state.get("page_type") or "") == "external_url" and state.get("continue_url"):
        return True
    continue_url = str(state.get("continue_url") or "")
    current_url = str(state.get("current_url") or "")
    return bool(continue_url and continue_url != current_url)


def _browser_add_cookies(page, cookies: list[dict]) -> None:
    try:
        page.context.add_cookies(cookies)
    except Exception:
        pass


def _seed_browser_device_id(page, device_id: str) -> None:
    _browser_add_cookies(
        page,
        [
            {"name": "oai-did", "value": device_id, "domain": "chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "auth.openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".auth.openai.com", "path": "/"},
        ],
    )


def _get_browser_csrf_token(page) -> str:
    result = _browser_fetch(
        page,
        f"{CHATGPT_APP}/api/auth/csrf",
        method="GET",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
            "sec-fetch-site": "same-origin",
        },
        redirect="follow",
    )
    if result.get("ok") and isinstance(result.get("data"), dict):
        return str((result.get("data") or {}).get("csrfToken") or "").strip()
    return ""


def _start_browser_signin(page, email: str, device_id: str, csrf_token: str) -> str:
    from urllib.parse import urlencode

    query = urlencode(
        {
            "prompt": "login",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
    )
    body = urlencode(
        {
            "callbackUrl": f"{CHATGPT_APP}/",
            "csrfToken": csrf_token,
            "json": "true",
        }
    )
    result = _browser_fetch(
        page,
        f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
        method="POST",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
            "origin": CHATGPT_APP,
            "content-type": "application/x-www-form-urlencoded",
            "sec-fetch-site": "same-origin",
        },
        body=body,
        redirect="follow",
    )
    if result.get("ok") and isinstance(result.get("data"), dict):
        return str((result.get("data") or {}).get("url") or "").strip()
    return ""


def _browser_authorize(page, auth_url: str, log) -> str:
    if not auth_url:
        return ""
    try:
        _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
        final_url = page.url
        log(f"Authorize -> {final_url}")
        return final_url
    except Exception as exc:
        log(f"Authorize 失败: {exc}")
        return ""


def _validate_browser_email_otp(page, code: str, device_id: str, user_agent: str, referer: str) -> dict:
    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=referer or f"{OPENAI_AUTH}/email-verification",
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )
    sentinel = _build_browser_sentinel_token(page, device_id, "email_otp_validate", user_agent)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/email-otp/validate",
        method="POST",
        headers=headers,
        body=json.dumps({"code": code}),
        redirect="follow",
    )


def _submit_browser_about_you(page, device_id: str, user_agent: str, referer: str) -> dict:
    from .constants import generate_random_user_info

    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=referer or f"{OPENAI_AUTH}/about-you",
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )
    sentinel = _build_browser_sentinel_token(page, device_id, "oauth_create_account", user_agent)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    user_info = generate_random_user_info()
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/create_account",
        method="POST",
        headers=headers,
        body=json.dumps(user_info),
        redirect="follow",
    )


def _complete_oauth_in_browser(page, oauth_start, proxy, log) -> dict | None:
    """在浏览器里完成 OAuth consent 流程，多策略重试点击 Continue。

    参考 Chrome 扩展项目的 step9 实现:
    - consent 页面是一个 <form action="/sign-in-with-chatgpt/.../consent">
    - 首选 form.requestSubmit(button) 而非 button.click()
    - 多轮重试: requestSubmit → click → dispatchEvent → 刷新重试
    """
    CONSENT_FORM_SEL = OAUTH_CONSENT_FORM_SELECTOR
    MAX_ROUNDS = 4
    CLICK_EFFECT_TIMEOUT = 30

    def _try_extract_callback(url: str) -> dict | None:
        if not url:
            return None
        callback_error = _extract_callback_error_from_url(url)
        if callback_error:
            log(f"  [callback_wait] 检测到 error callback，准备重走授权登录: {callback_error}")
            return _oauth_restart_required_result(url, callback_error)
        if "code=" not in url:
            return None
        return _submit_callback_result_or_error(url, oauth_start, proxy, log=log)

    def _check_current_url() -> dict | None:
        url = str(page.url or "")
        result = _try_extract_callback(url)
        if result:
            return result
        cb = _extract_callback_url_from_exception(Exception(url))
        return _try_extract_callback(cb) if cb else None

    def _wait_for_callback(timeout_sec: int) -> dict | None:
        deadline = time.time() + timeout_sec
        checked_urls = set()
        while time.time() < deadline:
            try:
                url = str(page.url or "")
            except Exception:
                url = ""
            if url and url not in checked_urls:
                checked_urls.add(url)
                if "code=" in url or "localhost" in url:
                    log(f"  [callback_wait] 检测到 URL 变化: {url[:150]}")
            result = _check_current_url()
            if result:
                return result
            # 也检查是否有导航到 localhost 的请求（即使页面加载失败）
            if "localhost" in url and ("code=" in url or "error=" in url):
                result = _try_extract_callback(url)
                if result:
                    return result
            time.sleep(0.8)
        # 最后再检查一次
        try:
            final_url = str(page.url or "")
            if "code=" in final_url or "error=" in final_url:
                log(f"  [callback_wait] 超时后最终 URL: {final_url}")
                result = _try_extract_callback(final_url)
                if result:
                    return result
        except Exception:
            pass
        return None

    def _find_consent_button():
        """按优先级查找 consent 页面的 Continue 按钮"""
        # 策略 1: 在 consent form 内找 submit 按钮
        _sel = CONSENT_FORM_SEL
        btn = page.evaluate("""(sel) => {
            const form = document.querySelector(sel);
            if (!form) return null;
            const buttons = form.querySelectorAll('button[type="submit"], input[type="submit"], [role="button"]');
            for (const el of buttons) {
                if (el.offsetParent === null) continue;
                const text = (el.textContent || '').trim().toLowerCase();
                const ddName = el.getAttribute('data-dd-action-name') || '';
                if (ddName === 'Continue' || /continue|继续|continuar|fortfahren|continuer|続ける/i.test(text)) return 'form-continue';
            }
            const first = Array.from(buttons).find(el => el.offsetParent !== null);
            if (first) return 'form-submit';
            return null;
        }""", _sel)
        if btn:
            return btn
        # 策略 2: 全局查找 Continue 按钮
        for sel in [
            'button[type="submit"][data-dd-action-name="Continue"]',
            'button:has-text("Continue")',
            'button:has-text("继续")',
            'button:has-text("Continuar")',
            'button:has-text("Fortfahren")',
            'button:has-text("Continuer")',
            'button:has-text("Allow")',
            'button:has-text("Authorize")',
            'button:has-text("続ける")',
            'button:has-text("続行")',
            'button:has-text("許可")',
            'button:has-text("認可")',
            'button[type="submit"]',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    return sel
            except Exception:
                continue
        return None

    def _click_strategy_request_submit(log_round: int) -> bool:
        """策略 1: form.requestSubmit(button) — 最可靠的表单提交方式"""
        try:
            result = page.evaluate("""(sel) => {
                const form = document.querySelector(sel);
                if (!form) return 'no-form';
                const buttons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
                let target = null;
                for (const el of buttons) {
                    if (el.offsetParent === null) continue;
                    const text = (el.textContent || '').trim().toLowerCase();
                    const ddName = el.getAttribute('data-dd-action-name') || '';
                    if (ddName === 'Continue' || /continue|继续|continuar|fortfahren|continuer|続ける|続行|許可|認可/i.test(text)) { target = el; break; }
                }
                if (!target) target = Array.from(buttons).find(el => el.offsetParent !== null);
                if (!target) return 'no-button';
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(target);
                    return 'requestSubmit';
                }
                target.click();
                return 'click-fallback';
            }""", CONSENT_FORM_SEL)
            log(f"  consent 第{log_round}轮 requestSubmit: {result}")
            return result not in ("no-form", "no-button")
        except Exception as e:
            log(f"  consent requestSubmit 异常: {e}")
            return False

    def _click_strategy_playwright(log_round: int) -> bool:
        """策略 2: Playwright locator.click()"""
        for sel in [
            'button:has-text("Continue")',
            'button:has-text("继续")',
            'button:has-text("Continuar")',
            'button:has-text("Fortfahren")',
            'button:has-text("Continuer")',
            'button:has-text("続ける")',
            'button:has-text("続行")',
            'button:has-text("許可")',
            'button:has-text("認可")',
            'button[type="submit"]',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    loc.click()
                    log(f"  consent 第{log_round}轮 playwright click: {sel}")
                    return True
            except Exception:
                continue
        return False

    def _click_strategy_js_dispatch(log_round: int) -> bool:
        """策略 3: JS dispatchEvent 模拟点击"""
        try:
            result = page.evaluate("""() => {
                const buttons = document.querySelectorAll('button, [role="button"]');
                for (const el of buttons) {
                    if (el.offsetParent === null) continue;
                    const text = (el.textContent || '').trim().toLowerCase();
                    const ddName = el.getAttribute('data-dd-action-name') || '';
                    if (ddName === 'Continue' || /continue|继续|continuar|fortfahren|continuer/i.test(text)) {
                        el.focus();
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                        return text || 'dispatched';
                    }
                }
                return null;
            }
            """)
            if result:
                log(f"  consent 第{log_round}轮 JS dispatch: {result}")
                return True
            return False
        except Exception:
            return False

    strategies = [
        _click_strategy_request_submit,
        _click_strategy_playwright,
        _click_strategy_js_dispatch,
        _click_strategy_request_submit,
    ]

    try:
        current_url = str(page.url or "")
        log(f"  浏览器 consent 处理: {current_url}")

        # 先检查当前 URL 是否已经有 code
        result = _check_current_url()
        if result:
            log("  [OK] 页面已在 callback URL")
            return result

        # 等待页面加载
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        time.sleep(1)

        # 检查 "Try again" 按钮
        try:
            try_again = page.query_selector('button:has-text("Try again"), button:has-text("再試行"), button:has-text("もう一度")')
            if try_again and try_again.is_visible():
                log("  consent 页面报错，点击 Try again...")
                try_again.click()
                time.sleep(3)
        except Exception:
            pass

        # 多轮策略重试
        for round_idx in range(MAX_ROUNDS):
            result = _check_current_url()
            if result:
                log("  [OK] 浏览器 OAuth consent 完成")
                return result

            strategy_fn = strategies[min(round_idx, len(strategies) - 1)]
            clicked = strategy_fn(round_idx + 1)

            if clicked:
                # consent 提交后会跳转到 localhost:1455/auth/callback
                # 由于没有本地服务监听，浏览器可能报连接错误，但 URL 已经更新
                try:
                    page.wait_for_url("**/auth/callback*", timeout=15000)
                except Exception:
                    pass  # 超时或导航错误都忽略，下面会检查 URL
                time.sleep(1)
                result = _wait_for_callback(CLICK_EFFECT_TIMEOUT)
                if result:
                    log("  [OK] 浏览器 OAuth consent 完成")
                    return result
                log(f"  consent 第{round_idx + 1}轮点击后页面未跳转")
            else:
                log(f"  consent 第{round_idx + 1}轮未找到按钮")

            # 最后一轮前刷新页面重试
            if round_idx < MAX_ROUNDS - 1:
                log(f"  consent 刷新页面准备第{round_idx + 2}轮...")
                try:
                    _reload_with_retry(page, wait_until="domcontentloaded", timeout=15000, log=log)
                except Exception:
                    pass
                time.sleep(2)

        final_url = str(page.url or "")
        final_result = _try_extract_callback(final_url)
        if final_result:
            log("  [OK] consent 末端从当前 callback URL 换取 token")
            return final_result
        log(f"  consent {MAX_ROUNDS}轮尝试后仍未完成，当前: {final_url}")
        return None
    except Exception as exc:
        cb = _extract_callback_url_from_exception(exc)
        if cb:
            result = _try_extract_callback(cb)
            if result:
                log("  [OK] 从异常中提取 callback 完成 OAuth")
                return result
        log(f"  浏览器 OAuth consent 异常: {exc}")
        return None


def _submit_oauth_password_direct(page, password: str, log) -> dict:
    """OAuth 流程专用：直接填密码登录，不尝试恢复到注册态。"""
    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        # 密码输入框没出现，可能页面还在加载或跳转了
        # 等一下再试
        time.sleep(2)
        input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=10)
    if not input_selector:
        raise RuntimeError("OAuth 密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("OAuth 密码页填写失败")
    log(f"  OAuth 密码页输入框: {input_selector}")
    _browser_pause(page)

    submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"  OAuth 密码页已点击继续按钮: {submit_selector}")
    elif _submit_form_with_fallback(page, input_selector):
        log("  OAuth 密码页使用表单 fallback 提交")
    else:
        raise RuntimeError("OAuth 密码页未找到 Continue 按钮")

    deadline = time.time() + 20
    while time.time() < deadline:
        current_url = str(page.url or "")
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in {"email_otp_verification", "about_you", "consent", "workspace_selection",
                         "organization_selection", "add_phone", "oauth_callback", "chatgpt_home", "external_url"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "code=" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        error_text = _extract_auth_error_text(page)
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": str(page.url or ""), "data": None, "text": "OAuth 密码提交后未跳转"}


def _submit_password_via_page(page, password: str, log) -> dict:
    if _recover_signup_password_page(page, log):
        time.sleep(1)

    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        raise RuntimeError("密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("密码页填写失败")
    log(f"密码页输入框: {input_selector}")
    _browser_pause(page)

    start_url = str(page.url or "")
    submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"密码页已点击继续按钮: {submit_selector}")
    elif _submit_form_with_fallback(page, input_selector):
        log("密码页未找到可点击 Continue，已使用表单 fallback 提交")
    else:
        raise RuntimeError("密码页未找到 Continue 按钮")

    deadline = time.time() + 20
    last_url = str(page.url or "")
    while time.time() < deadline:
        current_url = str(page.url or "")
        last_url = current_url or last_url
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in {"email_otp_verification", "about_you", "add_phone", "oauth_callback", "chatgpt_home"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if current_url != start_url and page_type and page_type not in {"create_account_password", "login_password"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if page_type == "login_password" and _recover_signup_password_page(page, log):
            input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=5)
            if not input_selector:
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后未找到注册密码输入框"}
            if not _fill_input_like_user(page, input_selector, password):
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后密码重新填写失败"}
            submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=5)
            if submit_selector:
                log(f"恢复后重新点击密码提交按钮: {submit_selector}")
                start_url = str(page.url or start_url)
                time.sleep(0.4)
                continue
            if _submit_form_with_fallback(page, input_selector):
                log("恢复后未找到密码提交按钮，已使用表单 fallback 提交")
                start_url = str(page.url or start_url)
                time.sleep(0.4)
                continue
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后未找到提交方式"}
        error_text = _extract_auth_error_text(page)
        if error_text:
            _dump_debug(page, "chatgpt_password_fail")
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    _dump_debug(page, "chatgpt_password_fail")
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "密码页提交后未跳转"}


def _submit_otp_via_page(page, code: str, log) -> dict:
    otp = str(code or "").strip()
    if not otp:
        return {"ok": False, "status": 400, "url": page.url, "data": None, "text": "验证码为空"}

    # 等待页面加载完成，确保 OTP 输入框已渲染
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    time.sleep(1)

    transition = _otp_page_transition_result(page)
    if transition:
        return transition

    filled = False

    # 先尝试 6 格 OTP 输入框
    try:
        digit_inputs = page.locator(
            "input[inputmode='numeric'], input[autocomplete='one-time-code'], input[type='tel'], input[type='number']"
        )
        count = digit_inputs.count()
        if count >= len(otp):
            done = 0
            for i in range(min(count, len(otp))):
                box = digit_inputs.nth(i)
                try:
                    box.wait_for(state="visible", timeout=800)
                    box.fill("")
                    box.type(otp[i], delay=random.randint(20, 60))
                    done += 1
                except Exception:
                    break
            if done >= len(otp):
                filled = True
                log(f"验证码页已填写 {done} 位分格输入框")
    except Exception:
        pass

    # 再尝试单输入框
    if not filled:
        otp_candidates = [
            page.get_by_label(re.compile(r"verification code|code|otp|認証コード|確認コード|ワンタイムコード", re.IGNORECASE)),
            page.get_by_role("textbox", name=re.compile(r"verification code|code|otp|認証コード|確認コード|ワンタイムコード", re.IGNORECASE)),
            page.locator("input[autocomplete='one-time-code']"),
            page.locator("input[name*='code' i]"),
            page.locator("input[id*='code' i]"),
            page.locator("input[type='text']"),
            page.locator("input"),
        ]
        for candidate in otp_candidates:
            try:
                target = candidate.first
                target.wait_for(state="visible", timeout=1200)
                target.click(timeout=1200)
                target.fill("")
                target.type(otp, delay=random.randint(18, 45))
                final_value = str(target.input_value() or "").strip()
                if final_value:
                    filled = True
                    log("验证码页已填写单输入框")
                    break
            except Exception:
                continue

    if not filled:
        # 再等 3 秒重试一次（页面可能还在渲染）
        time.sleep(3)
        transition = _otp_page_transition_result(page)
        if transition:
            return transition
        otp_retry_selectors = [
            "input[inputmode='numeric']",
            "input[autocomplete='one-time-code']",
            "input[name*='code' i]",
            "input[type='text']",
        ]
        for sel in otp_retry_selectors:
            try:
                target = page.locator(sel).first
                if target.is_visible(timeout=2000):
                    target.click(timeout=1500)
                    target.fill("")
                    target.type(otp, delay=random.randint(18, 45))
                    if str(target.input_value() or "").strip():
                        filled = True
                        log("验证码页已填写单输入框(重试)")
                        break
            except Exception:
                continue

    if not filled:
        return {"ok": False, "status": 0, "url": page.url, "data": None, "text": "验证码页未找到可填写输入框"}

    _browser_pause(page)
    submit_selector = _click_first(
        page,
        [
            'button[type="submit"]',
            'button[data-testid="continue-button"]',
            'button:has-text("Continue")',
            'button:has-text("continue")',
            'button:has-text("Verify")',
            'button:has-text("verify")',
            'button:has-text("Next")',
            'button:has-text("next")',
            'button:has-text("続ける")',
            'button:has-text("確認")',
            'button:has-text("認証")',
            'button:has-text("次へ")',
        ],
        timeout=8,
    )
    if not submit_selector:
        return {"ok": False, "status": 0, "url": page.url, "data": None, "text": "验证码页未找到 Continue 按钮"}
    log(f"验证码页已点击继续按钮: {submit_selector}")

    deadline = time.time() + 20
    last_url = page.url
    while time.time() < deadline:
        current_url = page.url
        last_url = current_url or last_url
        if "about-you" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "add-phone" in current_url or "chatgpt.com" in current_url or "code=" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "consent" in current_url or "sign-in-with-chatgpt" in current_url or "workspace" in current_url or "organization" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        error_text = _extract_email_otp_error_text(page)
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "验证码页提交后未跳转"}


def _otp_page_transition_result(page) -> dict | None:
    current_url = str(getattr(page, "url", "") or "")
    if _extract_code_from_url(current_url):
        return {"ok": True, "status": 200, "url": current_url, "data": None, "text": "OTP page already reached callback"}
    try:
        state = _derive_oauth_state_from_page(page)
    except Exception:
        state = {}
    page_type = str((state or {}).get("page_type") or "")
    if page_type and page_type != "email_otp_verification" and page_type in OTP_PAGE_RESUMABLE_PAGE_TYPES:
        return {
            "ok": True,
            "status": 200,
            "url": current_url,
            "data": None,
            "text": f"OTP page moved to {page_type}",
        }
    if any(key in current_url for key in ("add-phone", "consent", "sign-in-with-chatgpt", "workspace", "organization", "chatgpt.com")):
        return {"ok": True, "status": 200, "url": current_url, "data": None, "text": "OTP page already transitioned"}
    return None


def _is_transient_otp_submit_failure(text: str, status: int = 0) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized:
        return int(status or 0) == 0
    markers = (
        "验证码页未找到可填写输入框",
        "验证码页未找到 continue 按钮",
        "验证码页提交后未跳转",
        "email otp not submitted",
        "not found",
        "missing",
        "did not transition",
        "no input",
    )
    return int(status or 0) == 0 or any(marker in normalized for marker in markers)


def _recover_otp_submit_page(page, log, *, recover_url: str = "") -> dict | None:
    transition = _otp_page_transition_result(page)
    if transition:
        return transition
    current_url = str(getattr(page, "url", "") or "")
    try:
        state = _derive_oauth_state_from_page(page)
    except Exception:
        state = {}
    page_type = str((state or {}).get("page_type") or "")
    try:
        if page_type == "email_otp_verification" or "email-verification" in current_url:
            log("Email OTP page recovery: reloading current verification page")
            page.reload(wait_until="domcontentloaded", timeout=30000)
        elif recover_url:
            log("Email OTP page recovery: reopening current OAuth authorize URL")
            _goto_with_retry(page, recover_url, wait_until="domcontentloaded", timeout=30000, log=log)
        else:
            log("Email OTP page recovery: reloading current page")
            page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        log(f"Email OTP page recovery navigation failed: {exc}")
    time.sleep(1.0)
    return _otp_page_transition_result(page)


def _submit_email_otp_with_retry(
    page,
    otp_callback,
    log,
    *,
    max_invalid_retries: int = 3,
    max_transient_retries: int = 6,
    label: str = "Email OTP",
    recover_url: str = "",
) -> dict:
    invalid_retries = max(0, int(max_invalid_retries or 0))
    transient_retries = max(0, int(max_transient_retries or 0))
    invalid_retry_count = 0
    transient_retry_count = 0
    submit_attempt = 0
    code = ""
    need_new_code = True
    last_resp: dict = {"ok": False, "status": 0, "url": str(page.url or ""), "text": "email otp not submitted"}
    while True:
        transition = _otp_page_transition_result(page)
        if transition:
            return transition

        if need_new_code:
            if invalid_retry_count > 0:
                log(f"{label}: previous code was rejected, resending email OTP ({invalid_retry_count}/{invalid_retries})...")
                if not _resend_browser_email_otp(page, otp_callback, log):
                    last_resp = {
                        "ok": False,
                        "status": 0,
                        "url": str(page.url or ""),
                        "data": None,
                        "text": "email otp resend failed",
                    }
                    return last_resp
            code = str(otp_callback() or "").strip()
            if not code:
                raise RuntimeError("verification code not received")
            need_new_code = False

        submit_attempt += 1
        otp_resp = _submit_otp_via_page(page, code, log)
        log(f"{label}: submit status={otp_resp.get('status', 0)} attempt={submit_attempt}")
        if otp_resp.get("ok"):
            return otp_resp
        error_text = str(otp_resp.get("text") or _extract_email_otp_error_text(page) or "")
        last_resp = dict(otp_resp)
        last_resp["text"] = error_text or str(last_resp.get("text") or "")
        if _is_invalid_email_otp_text(error_text):
            if invalid_retry_count >= invalid_retries:
                return last_resp
            invalid_retry_count += 1
            log(f"{label}: invalid code detected: {error_text[:160]}")
            need_new_code = True
            continue

        if _is_transient_otp_submit_failure(error_text, int(otp_resp.get("status") or 0)):
            if transient_retry_count >= transient_retries:
                return last_resp
            transient_retry_count += 1
            log(
                f"{label}: transient submit failure, recovery retry "
                f"{transient_retry_count}/{transient_retries}: {error_text[:160]}"
            )
            transition = _recover_otp_submit_page(page, log, recover_url=recover_url)
            if transition:
                return transition
            continue

        return last_resp


def _submit_about_you_via_page(page, log) -> dict:
    from .constants import generate_random_user_info

    user_info = generate_random_user_info()
    name = str(user_info.get("name") or "").strip()
    birthdate = str(user_info.get("birthdate") or "").strip()
    if not name or not birthdate:
        raise RuntimeError("about_you 数据生成失败")
    date_parts = birthdate.split("-")
    if len(date_parts) == 3:
        yyyy, mm, dd = date_parts
        us_birthdate = f"{mm}/{dd}/{yyyy}"
        cn_birthdate = f"{yyyy}/{mm}/{dd}"
    else:
        us_birthdate = birthdate
        cn_birthdate = birthdate.replace("-", "/")
    log(f"about_you 表单: name={name}, birthdate={birthdate}, ui_birthdate={us_birthdate}, cn_birthdate={cn_birthdate}")

    def _fill_locator(locator, value: str) -> bool:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=1500)
            target.click(timeout=1500)
            _browser_pause(page, headed=False)
            try:
                applied = bool(
                    target.evaluate(
                        """
                        (input, nextValue) => {
                          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                          if (!setter) return false;
                          setter.call(input, nextValue);
                          input.dispatchEvent(new Event('input', { bubbles: true }));
                          input.dispatchEvent(new Event('change', { bubbles: true }));
                          return String(input.value || '') === String(nextValue || '');
                        }
                        """,
                        value,
                    )
                )
            except Exception:
                applied = False
            if not applied:
                target.fill("")
                target.type(value, delay=random.randint(25, 70))
            try:
                target.dispatch_event("blur")
            except Exception:
                pass
            final_val = str(target.input_value() or "").strip()
            return final_val == str(value).strip()
        except Exception:
            return False

    def _locator_from_visible_input_entry(entry: dict):
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            return None
        return page.locator("input:visible:not([type='hidden']):not([disabled]):not([readonly])").nth(visible_index)

    def _fill_visible_input_entry(entry: dict | None, value: str) -> bool:
        if not entry:
            return False
        locator = _locator_from_visible_input_entry(entry)
        if locator is None:
            return False
        return _fill_locator(locator, value)

    def _resolve_visible_input_selector(selectors: list[str]) -> str | None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=500)
                return selector
            except Exception:
                continue
        return None

    def _fill_second_visible_input(values: list[str], excluded_visible_indices: set[int] | None = None) -> bool:
        """兜底：about_you 卡片一般是 Full name + Birthday/Age 两个输入框。"""
        try:
            locator = page.locator(
                "input:visible:not([type='hidden']):not([disabled]):not([readonly])"
            )
            count = locator.count()
            if count < 2:
                return False
            excluded = {int(value) for value in (excluded_visible_indices or set())}
            target_index = None
            for idx in range(count):
                if idx not in excluded:
                    target_index = idx
                    if idx > 0:
                        break
            if target_index is None:
                return False
            target = locator.nth(target_index)
            target.click(timeout=1200)
            _browser_pause(page, headed=False)
            for value in values:
                try:
                    target.fill("")
                except Exception:
                    pass
                try:
                    target.type(str(value), delay=random.randint(18, 45))
                except Exception:
                    continue
                final_val = str(target.input_value() or "").strip()
                if final_val:
                    return True
            return False
        except Exception:
            return False

    def _has_visible(locator) -> bool:
        try:
            locator.first.wait_for(state="visible", timeout=700)
            return True
        except Exception:
            return False

    def _fill_birthday_selects(yyyy: str, mm: str, dd: str) -> bool:
        """处理 Month/Day/Year 下拉样式的生日控件。"""
        try:
            select_locator = page.locator("select:visible")
            count = select_locator.count()
            if count < 2:
                return False

            month_num = int(mm)
            day_num = int(dd)
            year_num = int(yyyy)
            month_short = time.strftime("%b", time.strptime(str(month_num), "%m"))
            month_full = time.strftime("%B", time.strptime(str(month_num), "%m"))

            assigned = {"month": False, "day": False, "year": False}

            for i in range(count):
                sel = select_locator.nth(i)
                try:
                    options = sel.locator("option")
                    option_count = options.count()
                except Exception:
                    option_count = 0
                if option_count <= 0:
                    continue

                texts: list[str] = []
                for idx in range(min(option_count, 80)):
                    try:
                        texts.append(str(options.nth(idx).inner_text(timeout=300) or "").strip())
                    except Exception:
                        continue
                joined = " ".join(texts).lower()

                try:
                    if (not assigned["month"]) and (
                        "january" in joined or "february" in joined or "march" in joined or "april" in joined
                    ):
                        for candidate in (month_full, month_short, str(month_num), f"{month_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["month"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["month"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["year"]) and any(str(y) in joined for y in (year_num, year_num - 1, year_num + 1, 2026, 2025)):
                        for candidate in (str(year_num),):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["year"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["year"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["day"]) and any(str(x) in joined for x in (" 1 ", "2", "30", "31")):
                        for candidate in (str(day_num), f"{day_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["day"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["day"] = True
                                    break
                                except Exception:
                                    continue
                except Exception:
                    continue

            # 下拉顺序兜底：month/day/year
            if count >= 3:
                try:
                    if not assigned["month"]:
                        select_locator.nth(0).select_option(label=month_short, timeout=800)
                        assigned["month"] = True
                except Exception:
                    pass
                try:
                    if not assigned["day"]:
                        select_locator.nth(1).select_option(label=str(day_num), timeout=800)
                        assigned["day"] = True
                except Exception:
                    pass
                try:
                    if not assigned["year"]:
                        select_locator.nth(2).select_option(label=str(year_num), timeout=800)
                        assigned["year"] = True
                except Exception:
                    pass

            return assigned["month"] and assigned["day"] and assigned["year"]
        except Exception:
            return False

    visible_inputs = _collect_visible_text_inputs(page)
    if visible_inputs:
        log(
            "about_you 可见输入框: "
            + " | ".join(
                f"#{int(item.get('visibleIndex', 0))} {(_about_you_input_hints(item) or '-')[:80]}"
                for item in visible_inputs[:4]
            )
        )
    ordered_visible_entries = sorted(
        [item for item in visible_inputs if str(item.get("visibleIndex", "")).isdigit()],
        key=lambda item: int(item.get("visibleIndex", 0)),
    )
    name_entry = _pick_best_about_you_input(visible_inputs, "name")
    age_entry = _pick_best_about_you_input(
        visible_inputs,
        "age",
        exclude_visible_indices={int(name_entry.get("visibleIndex"))} if name_entry and str(name_entry.get("visibleIndex", "")).isdigit() else set(),
    )

    name_candidates = [
        page.get_by_label(re.compile(r"full\s*name", re.IGNORECASE)),
        page.get_by_label(re.compile(r"全名|姓名|氏名|お名前|フルネーム", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"full\s*name|name", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"全名|姓名|氏名|お名前|フルネーム", re.IGNORECASE)),
        page.locator("input[autocomplete='name']"),
        page.locator("input[name*='name' i]"),
        page.locator("input[id*='name' i]"),
        page.locator("input[name*='姓名']"),
        page.locator("input[id*='姓名']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'full name')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'全名') or contains(normalize-space(string(.)),'姓名')]/following::input[1]"),
    ]
    birthday_candidates = [
        page.get_by_label(re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_label(re.compile(r"生日|出生|生年月日|誕生日", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"生日|出生|生年月日|誕生日", re.IGNORECASE)),
        page.get_by_placeholder(re.compile(r"mm.?dd.?yyyy|yyyy.?mm.?dd|birthday|生日|生年月日|誕生日", re.IGNORECASE)),
        page.locator("input[name*='birth' i]"),
        page.locator("input[id*='birth' i]"),
        page.locator("input[placeholder*='MM' i]"),
        page.locator("input[placeholder*='DD' i]"),
        page.locator("input[placeholder*='YYYY' i]"),
        page.locator("input[placeholder*='年']"),
        page.locator("input[placeholder*='月']"),
        page.locator("input[placeholder*='日']"),
        page.locator("input[inputmode='numeric']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'birthday')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'生日') or contains(normalize-space(string(.)),'出生')]/following::input[1]"),
        page.locator("input[type='date']"),
    ]

    age_years = None
    try:
        birth_year = int(str(birthdate).split("-")[0])
        current_year = int(time.strftime("%Y"))
        age_years = max(25, min(40, current_year - birth_year))
    except Exception:
        age_years = random.randint(25, 35)

    age_candidates = [
        page.get_by_label(re.compile(r"age", re.IGNORECASE)),
        page.get_by_label(re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"age", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.locator("input[name*='age' i]"),
        page.locator("input[id*='age' i]"),
        page.locator("input[placeholder*='Age' i]"),
        page.locator("input[placeholder*='年龄']"),
        page.locator("input[placeholder*='年齢']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'age')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'年龄')]/following::input[1]"),
    ]

    fill_result = {"name": False, "birthdate": False, "age": False, "month": False, "day": False, "year": False}
    if _fill_visible_input_entry(name_entry, name):
        fill_result["name"] = True
    if not fill_result.get("name"):
        for candidate in name_candidates:
            if _fill_locator(candidate, name):
                fill_result["name"] = True
                break
    mode_probe = {}
    try:
        mode_probe = page.evaluate(
            """
            () => {
              const labels = Array.from(document.querySelectorAll('label'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const placeholders = Array.from(document.querySelectorAll('input'))
                .map((n) => String(n.placeholder || '').trim().toLowerCase())
                .filter(Boolean);
              const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const allText = labels.concat(placeholders).concat(headings);
              const hasAge = allText.some((t) => t === 'age' || t === 'edad' || t === 'âge' || t === 'alter' || t === 'idade' || t === '年齢' || t.includes('how old') || t.includes('年龄') || t.includes('年齢') || t.includes('나이'));
              const hasBirthday = allText.some((t) =>
                t.includes('birthday') || t.includes('date of birth') || t.includes('birth') || t.includes('生日') || t.includes('出生') || t.includes('生年月日') || t.includes('誕生日') || t.includes('fecha de nacimiento') || t.includes('nascimento') || t.includes('geburtstag') || t.includes('naissance')
              );
              return { labels, placeholders, headings, hasAge, hasBirthday };
            }
            """
        ) or {}
    except Exception:
        mode_probe = {}

    has_age_label = bool(mode_probe.get("hasAge"))
    has_birthday_label = bool(mode_probe.get("hasBirthday"))
    has_age_field = any(_has_visible(candidate) for candidate in age_candidates[:3])
    has_birthday_field = any(_has_visible(candidate) for candidate in birthday_candidates[:3])
    has_birthday_select = False
    try:
        has_birthday_select = page.locator("select:visible").count() >= 2
    except Exception:
        has_birthday_select = False
    if has_birthday_select:
        about_mode = "birthday_select"
    elif (has_age_label and not has_birthday_label) or (has_age_field and not has_birthday_field):
        about_mode = "age"
    else:
        about_mode = "birthday"
    log(f"about_you 页面模式: {about_mode} labels={mode_probe.get('labels', [])[:4]}")
    direct_name_selector = _resolve_visible_input_selector(
        [
            'input[name="name"]',
            'input[name="full_name"]',
            'input[autocomplete="name"]',
            'input[placeholder*="全名"]',
            'input[placeholder*="name" i]',
            'input[id*="name" i]:not([type="hidden"])',
        ]
    )
    direct_age_selector = _resolve_visible_input_selector(
        [
            'input[name="age"]',
            'input[placeholder="Age"]',
            'input[placeholder="age"]',
            'input[placeholder*="年龄"]',
            'input[id*="age" i]',
        ]
    )
    if about_mode == "age" and len(ordered_visible_entries) >= 2:
        name_entry = ordered_visible_entries[0]
        age_entry = ordered_visible_entries[1]
        log(
            f"about_you age 输入框映射: name=#{int(name_entry.get('visibleIndex', 0))}, "
            f"age=#{int(age_entry.get('visibleIndex', 0))}"
        )
    if about_mode == "age":
        log(
            "about_you age 直接定位: "
            f"name={direct_name_selector or '-'}, age={direct_age_selector or '-'}"
        )

    def _fill_segmented_date(mm: str, dd: str, yyyy: str) -> bool:
        """处理 MM / DD / YYYY 分段日期输入框（React DateField 样式）。
        特征：一个 Birthday label 下有多个小 input 或 div[data-type] 段。"""
        try:
            # 方式1: div[data-type] 段 (React Aria DateField)
            month_seg = page.locator('div[data-type="month"], input[data-type="month"]')
            day_seg = page.locator('div[data-type="day"], input[data-type="day"]')
            year_seg = page.locator('div[data-type="year"], input[data-type="year"]')
            if month_seg.count() > 0 and day_seg.count() > 0 and year_seg.count() > 0:
                month_seg.first.click(force=True)
                page.keyboard.type(mm, delay=50)
                time.sleep(0.3)
                day_seg.first.click(force=True)
                page.keyboard.type(dd, delay=50)
                time.sleep(0.3)
                year_seg.first.click(force=True)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式2: 单个 date input 里有 MM/DD/YYYY 占位符
            # 点击输入框，然后按顺序输入 MM DD YYYY（Tab 切换段）
            date_input = page.locator("input[placeholder*='MM'], input[placeholder*='mm'], input[type='date']")
            if date_input.count() > 0:
                date_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.type(mm, delay=50)
                page.keyboard.type(dd, delay=50)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式3: Birthday label 下的第二个可见 input，直接点击后按数字键输入
            birthday_input = page.get_by_label(re.compile(r"birthday|birth", re.IGNORECASE))
            if birthday_input.count() > 0:
                birthday_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.type(mm, delay=50)
                page.keyboard.type(dd, delay=50)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式4: 第二个可见 input（name 是第一个）
            inputs = page.locator("input:visible:not([type='hidden']):not([disabled])")
            if inputs.count() >= 2:
                target = inputs.nth(1)
                target.click(force=True)
                time.sleep(0.3)
                # 先清空
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                time.sleep(0.1)
                # 输入 MM，Tab 到 DD，Tab 到 YYYY
                page.keyboard.type(mm, delay=80)
                time.sleep(0.3)
                page.keyboard.type(dd, delay=80)
                time.sleep(0.3)
                page.keyboard.type(yyyy, delay=80)
                time.sleep(0.3)
                # 验证是否填入了正确的值
                val = str(target.input_value() or "").strip()
                if val and val != target.get_attribute("placeholder"):
                    return True
                # 如果直接输入不行，试 Tab 切换
                target.click(force=True)
                time.sleep(0.2)
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                for i, part in enumerate([mm, dd, yyyy]):
                    page.keyboard.type(part, delay=80)
                    if i < 2:
                        page.keyboard.press("Tab")
                        time.sleep(0.2)
                return True
        except Exception:
            pass
        return False

    if about_mode == "birthday_select":
        if len(date_parts) == 3 and _fill_birthday_selects(yyyy, mm, dd):
            fill_result["month"] = True
            fill_result["day"] = True
            fill_result["year"] = True
            fill_result["birthdate"] = True
    elif about_mode == "age":
        if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
            fill_result["name"] = True
        elif _fill_visible_input_entry(name_entry, name):
            fill_result["name"] = True
        if age_years is not None:
            if direct_age_selector and _fill_input_like_user(page, direct_age_selector, str(age_years)):
                fill_result["age"] = True
            elif _fill_visible_input_entry(age_entry, str(age_years)):
                fill_result["age"] = True
            if not fill_result.get("age") and len(ordered_visible_entries) < 2:
                for candidate in age_candidates:
                    if _fill_locator(candidate, str(age_years)):
                        fill_result["age"] = True
                        break
        # fallback: 直接找 placeholder="Age" 的输入框
        if not fill_result.get("age") and age_years is not None and len(ordered_visible_entries) < 2:
            try:
                age_input = page.locator("input[placeholder='Age'], input[placeholder='age']")
                if age_input.count() > 0:
                    age_input.first.click(force=True)
                    time.sleep(0.2)
                    age_input.first.fill("")
                    age_input.first.type(str(age_years), delay=random.randint(30, 60))
                    fill_result["age"] = True
            except Exception:
                pass
        if not fill_result.get("age") and age_years is not None:
            excluded_indices = set()
            if name_entry and str(name_entry.get("visibleIndex", "")).isdigit():
                excluded_indices.add(int(name_entry.get("visibleIndex")))
            if _fill_second_visible_input([str(age_years)], excluded_visible_indices=excluded_indices):
                fill_result["age"] = True
        if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
            fill_result["birthdate"] = True
    elif about_mode == "birthday" or about_mode == "birthday_text":
        # 先尝试分段日期输入（MM / DD / YYYY 格式的 DateField）
        if len(date_parts) == 3 and _fill_segmented_date(mm, dd, yyyy):
            fill_result["birthdate"] = True
            log("about_you 使用分段日期输入成功")
        # 再尝试普通文本输入
        if not fill_result.get("birthdate"):
            for candidate in birthday_candidates:
                if _fill_locator(candidate, cn_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, cn_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
        if not fill_result.get("birthdate"):
            fallback_values = [cn_birthdate, cn_birthdate.replace("/", " / "), cn_birthdate.replace("/", ""), us_birthdate, us_birthdate.replace("/", " / "), us_birthdate.replace("/", ""), birthdate]
            if _fill_second_visible_input(fallback_values):
                fill_result["birthdate"] = True

    log(f"about_you 填写结果: {fill_result}")
    if not fill_result.get("name"):
        raise RuntimeError("about_you 未成功填写 Full name")
    if not (
        fill_result.get("birthdate")
        or fill_result.get("age")
        or (fill_result.get("month") and fill_result.get("day") and fill_result.get("year"))
    ):
        raise RuntimeError("about_you 未成功填写 Birthday/Age")
    _browser_pause(page)

    submit_selector = _click_first(
        page,
        [
            'button:has-text("Finish creating account")',
            'button:has-text("finish creating account")',
            'button[type="submit"]',
            'button[data-testid="continue-button"]',
            'button:has-text("Continue")',
            'button:has-text("continue")',
            'button:has-text("Next")',
            'button:has-text("next")',
        ],
        timeout=8,
    )
    if not submit_selector:
        raise RuntimeError("about_you 未找到提交按钮")
    log(f"about_you 已点击继续按钮: {submit_selector}")

    deadline = time.time() + 20
    retried_generic_validation = False
    last_url = page.url
    while time.time() < deadline:
        current_url = page.url
        last_url = current_url or last_url
        if "code=" in current_url or "chatgpt.com" in current_url or "sign-in-with-chatgpt" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "add-phone" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        try:
            error_text = page.locator("text=Sorry, we cannot create your account").first.text_content(timeout=500)
        except Exception:
            error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=Enter a valid age to continue").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=doesn't look right").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("[role='alert']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator(".error, [class*='error'], [class*='Error']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if error_text and "oai_log" not in error_text and "SSR_HTML" not in error_text:
            normalized_error = str(error_text).strip().lower()
            if (
                about_mode == "age"
                and not retried_generic_validation
                and ("doesn't look right" in normalized_error or "try again" in normalized_error)
            ):
                retried_generic_validation = True
                log("about_you age 模式提交被拒，重新同步 Full name/Age/hidden birthday 后重试一次...")
                if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
                    fill_result["name"] = True
                elif _fill_visible_input_entry(name_entry, name):
                    fill_result["name"] = True
                elif len(ordered_visible_entries) < 2:
                    for candidate in name_candidates:
                        if _fill_locator(candidate, name):
                            fill_result["name"] = True
                            break
                if age_years is not None:
                    if direct_age_selector and _fill_input_like_user(page, direct_age_selector, str(age_years)):
                        fill_result["age"] = True
                    elif _fill_visible_input_entry(age_entry, str(age_years)):
                        fill_result["age"] = True
                    elif len(ordered_visible_entries) < 2:
                        for candidate in age_candidates:
                            if _fill_locator(candidate, str(age_years)):
                                fill_result["age"] = True
                                break
                if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
                    fill_result["birthdate"] = True
                _browser_pause(page)
                retry_submit_selector = _click_first(
                    page,
                    [
                        'button:has-text("Finish creating account")',
                        'button:has-text("finish creating account")',
                        'button[type="submit"]',
                        'button[data-testid="continue-button"]',
                        'button:has-text("Continue")',
                        'button:has-text("continue")',
                        'button:has-text("Next")',
                        'button:has-text("next")',
                    ],
                    timeout=5,
                )
                if retry_submit_selector:
                    log(f"about_you 重试提交按钮: {retry_submit_selector}")
                    time.sleep(0.5)
                    continue
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    _dump_debug(page, "chatgpt_about_you_fail")
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "about_you 提交后未跳转"}


def _browser_registration_flow_once(
    page,
    email: str,
    password: str,
    otp_callback,
    phone_callback,
    log,
    *,
    signup_method: str = "email",
    phone_change_limit: int = 10,
) -> dict:
    device_id = str(uuid.uuid4())
    phone_first_signup = str(signup_method or "").strip().lower() == "phone"
    signup_username = email
    phone_attempt_limit = max(int(phone_change_limit or 1), 1)
    phone_change_used = 0
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip() or _random_chrome_ua()
    except Exception:
        user_agent = _random_chrome_ua()

    _seed_browser_device_id(page, device_id)
    try:
        if phone_first_signup:
            log("Phone-first signup: starting with SMS phone registration")
            while True:
                try:
                    state, _signup_phone = _start_phone_first_signup_from_forced_entry(
                        page,
                        phone_callback,
                        log,
                    )
                    signup_username = _signup_phone or signup_username
                    break
                except RuntimeError as exc:
                    error_text = str(exc)
                    if not (
                        phone_callback
                        and phone_change_used < phone_attempt_limit
                        and _is_retryable_initial_phone_fetch_error(error_text)
                    ):
                        raise
                    log(
                        f"Phone-first signup: initial phone rejected, switching phone "
                        f"({phone_change_used + 1}/{phone_attempt_limit}): {error_text[:180]}"
                    )
                    _reset_phone_callback_for_new_number(phone_callback, error_text)
                    phone_change_used += 1
                    try:
                        page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    time.sleep(0.5)
        else:
            log("使用 ChatGPT NextAuth 注册入口启动浏览器注册")
            state = _start_browser_signup_via_authorize(page, email, device_id, log)
    except Exception as exc:
        log(f"ChatGPT NextAuth 注册入口失败: {exc}")
        raise
    auth_cookies = _get_cookies(page)
    log(
        "授权态 cookies: "
        f"login_session={'yes' if auth_cookies.get('login_session') else 'no'}, "
        f"oai-did={'yes' if auth_cookies.get('oai-did') else 'no'}"
    )
    log(f"注册状态起点: page={state.get('page_type') or '-'} url={(state.get('current_url') or '')}")
    register_submitted = False
    seen_states: dict[str, int] = {}

    for step in range(12):
        signature = "|".join(
            [
                str(state.get("page_type") or ""),
                str(state.get("method") or ""),
                str(state.get("continue_url") or ""),
                str(state.get("current_url") or ""),
            ]
        )
        seen_states[signature] = seen_states.get(signature, 0) + 1
        log(
            f"注册状态推进: step={step+1} page={state.get('page_type') or '-'} "
            f"next={str(state.get('continue_url') or '')} seen={seen_states[signature]}"
        )
        if str(state.get("page_type") or "") == "pending_transition":
            time.sleep(1)
            state = _wait_for_phone_first_otp_or_password_state(page, state, log, timeout=1.0)
            seen_states.clear()
            continue
        if seen_states[signature] > 2:
            raise RuntimeError(f"注册状态卡住: page={state.get('page_type') or '-'}")

        if _is_registration_complete(state):
            _handle_post_signup_onboarding(page, log)
            return _extract_flow_state(None, page.url)

        if _is_password_registration(state):
            if register_submitted:
                raise RuntimeError("重复进入密码注册阶段")
            log("提交注册密码...")
            pre_cookies = _get_cookies(page)
            log(
                "密码阶段 cookies: "
                f"login_session={'yes' if pre_cookies.get('login_session') else 'no'}, "
                f"oai-client-auth-session={'yes' if pre_cookies.get('oai-client-auth-session') else 'no'}"
            )
            reg_resp = _submit_password_via_page(page, password, log)
            log(f"密码页提交状态: {reg_resp.get('status', 0)}")
            if not reg_resp.get("ok"):
                error_text = str(reg_resp.get("text") or "")
                if (
                    phone_first_signup
                    and phone_callback
                    and phone_change_used < phone_attempt_limit
                    and _is_retryable_phone_rejection_text(error_text)
                ):
                    log(
                        f"Phone-first signup: account creation failed with current phone, "
                        f"switching phone "
                        f"({phone_change_used + 1}/{phone_attempt_limit}): {error_text[:180]}"
                    )
                    _reset_phone_callback_for_new_number(phone_callback, error_text)
                    phone_change_used += 1
                    if not _return_phone_first_signup_to_phone_entry(page, log):
                        raise RuntimeError("Phone-first signup phone edit/change did not reveal phone input; restart full homepage flow")
                    new_phone = str(phone_callback() or "").strip()
                    if not new_phone:
                        raise RuntimeError("Phone-first signup did not receive a replacement phone number")
                    log(f"Phone-first signup using replacement phone: {_mask_phone_number(new_phone)}")
                    signup_username = new_phone
                    state = _submit_phone_identity_via_page(page, new_phone, log)
                    state = _refresh_registration_state_from_page_if_password_visible(page, state, log)
                    register_submitted = False
                    seen_states.clear()
                    continue
                raise RuntimeError(f"密码页提交失败: {(reg_resp.get('text') or '')}")
            register_submitted = True
            state = _extract_flow_state(reg_resp.get("data"), reg_resp.get("url", page.url))
            if not state.get("page_type") or _is_password_registration(state):
                state = _derive_registration_state_from_page(page)
            continue

        if str(state.get("page_type") or "") == "login_password":
            if phone_first_signup and phone_callback and phone_change_used < phone_attempt_limit:
                log(
                    f"Phone-first signup: current phone resolved to existing account, "
                    f"switching phone ({phone_change_used + 1}/{phone_attempt_limit})"
                )
                _reset_phone_callback_for_new_number(phone_callback, "phone resolved to existing account")
                phone_change_used += 1
                log("Phone-first signup: restarting from chatgpt.com homepage")
                raise RuntimeError("Phone-first signup current phone resolved to existing account")
            if _recover_signup_password_page(page, log):
                state = _derive_registration_state_from_page(page)
                continue
            log("注册流程落到已有账号登录密码页，按登录流程继续认证...")
            login_resp = _submit_oauth_password_direct(page, password, log)
            log(f"登录密码页提交状态: {login_resp.get('status', 0)}")
            if not login_resp.get("ok"):
                raise RuntimeError(f"登录密码页提交失败: {(login_resp.get('text') or '')}")
            state = _extract_flow_state(login_resp.get("data"), login_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if str(state.get("page_type") or "") == "phone_entry":
            if not phone_first_signup or not phone_callback:
                raise RuntimeError("Phone entry page requires phone-first signup callback")
            new_phone = str(phone_callback() or "").strip()
            if not new_phone:
                raise RuntimeError("Phone-first signup did not receive a phone number for phone_entry")
            signup_username = new_phone
            state = _submit_phone_identity_via_page(page, new_phone, log)
            state = _refresh_registration_state_from_page_if_password_visible(page, state, log)
            register_submitted = False
            seen_states.clear()
            continue

        if str(state.get("page_type") or "") == "phone_otp_verification":
            if not phone_first_signup or not phone_callback:
                raise RuntimeError("Phone OTP page requires phone-first signup callback")
            if not _is_visible_phone_first_sms_otp_page(page):
                log("Phone-first signup: phone OTP state not confirmed by visible code input; waiting for page transition")
                state = _build_manual_flow_state("pending_transition", str(getattr(page, "url", "") or ""))
                continue
            sms_code = str(phone_callback() or "").strip()
            if not sms_code:
                raise RuntimeError("Phone-first signup did not receive an SMS code")
            otp_resp = _submit_phone_otp_dom(page, sms_code, log)
            log(f"Phone-first signup phone OTP submit status: {otp_resp.get('status', 0)}")
            if not otp_resp.get("ok"):
                reason = str(otp_resp.get("text") or "")
                if "missing_phone_verification_form" in reason or "missing_code_input" in reason:
                    log("Phone OTP DOM submit missing form/input, trying generic OTP submit")
                    otp_resp = _submit_otp_via_page(page, sms_code, log)
                if not otp_resp.get("ok"):
                    if hasattr(phone_callback, "mark_code_failed"):
                        try:
                            phone_callback.mark_code_failed(str(otp_resp.get("text") or "invalid phone otp"))
                        except Exception:
                            pass
                    raise RuntimeError(f"Phone OTP submit failed: {(otp_resp.get('text') or '')}")
            if hasattr(phone_callback, "report_success"):
                try:
                    phone_callback.report_success()
                except Exception:
                    pass
            state = _extract_flow_state(otp_resp.get("data"), otp_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if str(state.get("page_type") or "") == "pending_transition":
            time.sleep(1)
            state = _wait_for_phone_first_otp_or_password_state(page, state, log, timeout=1.0)
            continue

        if _is_email_otp(state):
            if phone_first_signup:
                state = _wait_for_phone_first_otp_or_password_state(page, state, log)
            else:
                state = _refresh_registration_state_from_page_if_password_visible(page, state, log)
            if _is_password_registration(state) or str(state.get("page_type") or "") == "login_password":
                log(
                    "Phone-first signup: visible password page overrides stale OTP state; "
                    "continuing password step"
                )
                continue
            if phone_first_signup and not _is_email_otp(state):
                continue
            if phone_first_signup and _is_email_otp(state) and not _is_visible_phone_first_sms_otp_page(page):
                log("Phone-first signup: email OTP state not confirmed by visible SMS code input; waiting for page transition")
                state = _build_manual_flow_state("pending_transition", str(getattr(page, "url", "") or ""))
                continue
            active_otp_callback = phone_callback if phone_first_signup else otp_callback
            if not active_otp_callback:
                raise RuntimeError("ChatGPT 注册需要验证码但未提供 callback")
            log("等待 ChatGPT 验证码")
            if phone_first_signup:
                code = active_otp_callback()
                if not code:
                    raise RuntimeError("未获取到验证码")
                otp_resp = _submit_otp_via_page(page, code, log)
            else:
                otp_resp = _submit_email_otp_with_retry(
                    page,
                    active_otp_callback,
                    log,
                    max_invalid_retries=3,
                    label="Register email OTP",
                )
            log(f"验证码页提交状态: {otp_resp.get('status', 0)}")
            if not otp_resp.get("ok"):
                if phone_first_signup and hasattr(phone_callback, "mark_code_failed"):
                    try:
                        phone_callback.mark_code_failed(str(otp_resp.get("text") or "invalid otp"))
                    except Exception:
                        pass
                raise RuntimeError(f"验证码校验失败: {(otp_resp.get('text') or '')}")
            if phone_first_signup and hasattr(phone_callback, "report_success"):
                try:
                    phone_callback.report_success()
                except Exception:
                    pass
            state = _extract_flow_state(otp_resp.get("data"), otp_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if _is_about_you(state):
            log("提交 about_you 信息...")
            target_url = _normalize_url(
                str(state.get("current_url") or state.get("continue_url") or f"{OPENAI_AUTH}/about-you"),
                OPENAI_AUTH,
            )
            if "about-you" not in str(page.url):
                log(f"跳转到 about_you 页面: {target_url[:120]}")
                _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
            about_resp = _submit_about_you_via_page(page, log)
            log(f"about_you 提交状态: {about_resp.get('status', 0)}")
            if not about_resp.get("ok"):
                raise RuntimeError(f"about_you 提交失败: {(about_resp.get('text') or '')}")
            state = _extract_flow_state(about_resp.get("data"), about_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            if _is_add_phone(state):
                if not phone_callback:
                    return state
                log("about_you 后进入 add_phone，尝试短信验证...")
                state = _handle_add_phone_challenge(
                    page,
                    phone_callback,
                    device_id=device_id,
                    user_agent=user_agent,
                    log=log,
                    resume_url=f"{CHATGPT_APP}/",
                )
            continue

        if _is_add_phone(state):
            if not phone_callback:
                return state
            log("注册流程进入 add_phone，尝试短信验证...")
            state = _handle_add_phone_challenge(
                page,
                phone_callback,
                device_id=device_id,
                user_agent=user_agent,
                log=log,
                resume_url=f"{CHATGPT_APP}/",
            )
            continue

        if _requires_registration_navigation(state):
            target_url = _normalize_url(str(state.get("continue_url") or state.get("current_url") or ""), OPENAI_AUTH)
            if not target_url:
                raise RuntimeError("缺少可跟随的 continue_url")
            _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
            state = _extract_flow_state(None, page.url)
            continue

        raise RuntimeError(f"未支持的注册状态: page={state.get('page_type') or '-'}")

    raise RuntimeError("注册状态机超出最大步数")


def _browser_registration_flow(
    page,
    email: str,
    password: str,
    otp_callback,
    phone_callback,
    log,
    *,
    signup_method: str = "email",
    phone_change_limit: int = 10,
) -> dict:
    phone_first_signup = str(signup_method or "").strip().lower() == "phone"
    if not phone_first_signup:
        return _browser_registration_flow_once(
            page,
            email,
            password,
            otp_callback,
            phone_callback,
            log,
            signup_method=signup_method,
            phone_change_limit=phone_change_limit,
        )

    last_error = ""
    for round_index in range(1, 4):
        log(f"Phone-first signup: starting full registration round {round_index}/3")
        try:
            return _browser_registration_flow_once(
                page,
                email,
                password,
                otp_callback,
                phone_callback,
                log,
                signup_method=signup_method,
                phone_change_limit=phone_change_limit,
            )
        except Exception as exc:
            last_error = str(exc)
            if round_index >= 3:
                break
            log(
                "Phone-first signup: registration round failed, "
                f"restarting from chatgpt.com homepage ({round_index + 1}/3): {last_error[:180]}"
            )
            _reset_phone_callback_for_new_number(phone_callback, last_error)
            _nuke_all_browser_state(page, log)
            try:
                page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
            except Exception:
                pass
            time.sleep(1)
    raise RuntimeError(f"Phone-first signup failed after 3 full rounds: {last_error}")


class ChatGPTBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        phone_callback: Optional[Callable[[], str]] = None,
        log_fn: Callable[[str], None] = print,
        backend_config: Optional[BrowserBackendConfig] = None,
        post_register_in_browser: Optional[Callable[[Any, dict], dict]] = None,
        phone_first_oauth: bool = False,
        bind_email_after_phone_signup: bool = False,
        record_har: bool = False,
        phone_change_limit: int = 10,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.phone_callback = phone_callback
        self.log = log_fn
        self.phone_first_oauth = bool(phone_first_oauth)
        self.bind_email_after_phone_signup = bool(bind_email_after_phone_signup)
        self.record_har = bool(record_har)
        self.phone_change_limit = max(int(phone_change_limit or 1), 1)
        # post_register_in_browser(page, session_info) -> dict|None：
        # 注册拿到 session 后、**浏览器还开着**时回调。短链复用流程用它在
        # 同一个浏览器/同一 page 里打开短链并抓 midtrans_url。返回的 dict 会
        # 合并进 run() 的结果（如 {"midtrans_url": "..."}）。回调异常不影响
        # 注册结果本身（只记日志、不抛）。
        self.post_register_in_browser = post_register_in_browser
        # backend_config 为 None 时默认 Camoufox，跟老调用方一致。
        # BitBrowser 路径需要上层 plugin.py 显式传 backend_config。
        self.backend_config = backend_config or BrowserBackendConfig.camoufox(
            headless=bool(headless)
        )
        if self.backend_config.is_bitbrowser:
            log_fn(
                f"ChatGPT 注册使用 BitBrowser backend "
                f"(profile={self.backend_config.bit_profile_id}, "
                f"window_mode={self.backend_config.window_mode})"
            )

    def _open_browser(self, launch_opts: dict):
        """与业务代码代期使用的 ``with Camoufox(**launch_opts) as browser:`` 接口
        保持兑现：按 ``self.backend_config`` 路由到 Camoufox 或 BitBrowser。
        BitBrowser 路径下 launch_opts 里的 proxy/geoip 会被忽略（profile
        自带代理）。"""
        if self.backend_config.is_camoufox:
            _patch_playwright_firefox_pageerror_location_bug(log_fn=self.log)
        return open_browser_backend(
            launch_opts=launch_opts,
            config=self.backend_config,
            camoufox_class=Camoufox,
            log=self.log,
        )

    def run(self, email: str, password: str) -> dict:
        if self.backend_config.is_bitbrowser:
            # BitBrowser 路径：profile 已配代理/指纹，launch_opts 不传这些。
            launch_opts = {
                "headless": self.backend_config.is_headless,
                "args": [REGISTER_BROWSER_WINDOW_ARG],
            }
        else:
            proxy = _build_proxy_config(self.proxy)
            launch_opts = {"headless": self.headless}
            if proxy:
                launch_opts["proxy"] = proxy
                if not _is_local_proxy(self.proxy):
                    launch_opts["geoip"] = True
            apply_camoufox_register_window_size(launch_opts)

        with self._open_browser(launch_opts) as browser:
            page = browser.new_page()
            set_register_page_viewport(page)
            self.log("启动浏览器上下文注册状态机")
            final_state = _browser_registration_flow(
                page,
                email,
                password,
                self.otp_callback,
                self.phone_callback,
                self.log,
                signup_method="phone" if self.phone_first_oauth else "email",
                phone_change_limit=self.phone_change_limit,
            )
            self.log(f"注册流程完成: page={final_state.get('page_type') or '-'}")

            # 获取 session token 和 cookies
            cookies_dict = _get_cookies(page)
            session_info = _fetch_chatgpt_session_from_page(page, cookies_dict, self.log)
            result = {
                "email": email,
                "password": password,
                "account_id": session_info.get("account_id", ""),
                "access_token": session_info.get("access_token", ""),
                "refresh_token": "",
                "registration_refresh_token": session_info.get("refresh_token", ""),
                "registration_refresh_token_usable": False,
                "refresh_token_source": "",
                "id_token": session_info.get("id_token", ""),
                "session_token": session_info.get("session_token", ""),
                "workspace_id": session_info.get("workspace_id", ""),
                "cookies": session_info.get("cookies", "") or _cookies_to_header(cookies_dict),
                "profile": session_info.get("profile", {}),
                "expires_at": session_info.get("expires_at", ""),
                "session": session_info.get("session", {}),
                "registration_state": final_state,
            }

            if self.phone_first_oauth and self.bind_email_after_phone_signup:
                self.log("Phone-first signup: binding email and completing Codex OAuth")
                oauth_result = _do_codex_oauth(
                    page,
                    cookies_dict,
                    email,
                    password,
                    self.otp_callback,
                    self.phone_callback,
                    self.proxy,
                    self.log,
                )
                if not isinstance(oauth_result, dict) or not (
                    oauth_result.get("access_token") and oauth_result.get("refresh_token")
                ):
                    oauth_result = self._retry_oauth_fresh_browser(email, password) or {}
                if not isinstance(oauth_result, dict) or not (
                    oauth_result.get("access_token") and oauth_result.get("refresh_token")
                ):
                    raise RuntimeError("Phone-first signup OAuth callback did not return usable refresh_token")
                result.update(
                    {
                        "account_id": oauth_result.get("account_id") or result.get("account_id", ""),
                        "access_token": oauth_result.get("access_token", ""),
                        "refresh_token": oauth_result.get("refresh_token", ""),
                        "refresh_token_source": "phone_first_oauth",
                        "id_token": oauth_result.get("id_token", ""),
                        "oauth": oauth_result,
                    }
                )

            # 短链复用流程：注册拿到 session 后、**浏览器还开着**时，在同一个
            # page 里继续打开短链 + 抓 midtrans_url。结果合并进返回值。
            if callable(self.post_register_in_browser):
                try:
                    self.log("注册完成，浏览器保持打开，继续在同一浏览器里走短链付款流程…")
                    extra = self.post_register_in_browser(page, dict(result))
                    if isinstance(extra, dict):
                        result.update(extra)
                except Exception as exc:
                    self.log(f"浏览器内短链后续流程异常（不影响注册结果）: {exc}")
            return result

    def _retry_oauth_fresh_browser(self, email, password):
        """在全新浏览器 context 里做 Codex OAuth（绕过 add_phone session）。"""
        if self.backend_config.is_bitbrowser:
            launch_opts = {
                "headless": self.backend_config.is_headless,
                "args": [REGISTER_BROWSER_WINDOW_ARG],
            }
        else:
            proxy = _build_proxy_config(self.proxy)
            launch_opts = {"headless": self.headless}
            if proxy:
                launch_opts["proxy"] = proxy
            apply_camoufox_register_window_size(launch_opts)
        try:
            with self._open_browser(launch_opts) as browser:
                page = browser.new_page()
                set_register_page_viewport(page)
                self.log("  全新浏览器 OAuth 开始...")
                result = _do_codex_oauth(
                    page, {}, email, password,
                    self.otp_callback, self.phone_callback, self.proxy, self.log,
                )
                return result
        except Exception as e:
            self.log(f"  全新浏览器 OAuth 异常: {e}")
            return None
