#!/usr/bin/env python3
"""Apply headed-HAR aligned header/SDK fixes to platforms/chatgpt/register.py."""
from __future__ import annotations

from pathlib import Path

PATH = Path("platforms/chatgpt/register.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"FAIL missing block: {label}")
    return text.replace(old, new, 1)


def main() -> None:
    text = PATH.read_text(encoding="utf-8")

    text = replace_once(
        text,
        (
            "LATEST_CHATGPT_FIREFOX_USER_AGENT = (\n"
            "    \"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) \"\n"
            "    \"Gecko/20100101 Firefox/135.0\"\n"
            ")\n"
            "LATEST_CHATGPT_OAI_CLIENT_VERSION = \"prod-7fc3ff5bcd034a91578eeeb94258b0210e7ff3b2\"\n"
            "LATEST_CHATGPT_OAI_CLIENT_BUILD_NUMBER = \"8494897\"\n"
        ),
        (
            "LATEST_CHATGPT_FIREFOX_USER_AGENT = (\n"
            "    \"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) \"\n"
            "    \"Gecko/20100101 Firefox/135.0\"\n"
            ")\n"
            "# Headed Camoufox HAR 2026-07-25 register capture.\n"
            "LATEST_CHATGPT_OAI_CLIENT_VERSION = \"prod-2c08737cf6aa91754c5bf303734db2dba173c6ce\"\n"
            "LATEST_CHATGPT_OAI_CLIENT_BUILD_NUMBER = \"8578659\"\n"
            "LATEST_CHATGPT_SENTINEL_SCREEN = 2494\n"
            "LATEST_CHATGPT_SENTINEL_CORES = 12\n"
            "LATEST_CHATGPT_CF_JSD_SCRIPT_URL = (\n"
            "    \"https://chatgpt.com/cdn-cgi/challenge-platform/scripts/jsd/api.js?onload=jsdOnload\"\n"
            ")\n"
            "LATEST_CHATGPT_SENTINEL_ENTRY_SDK_URL = \"https://sentinel.openai.com/backend-api/sentinel/sdk.js\"\n"
        ),
        "client constants",
    )

    old_gen = '''class _SentinelTokenGenerator:

    """Dynamic sentinel token generator – mirrors browser_register._SentinelTokenGenerator."""



    def __init__(self, device_id: str, user_agent: str):

        self.device_id = device_id or str(uuid.uuid4())

        self.user_agent = user_agent

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

        # Keep this fingerprint array aligned with headed-browser HAR samples:
        # [screen, date, null, nonce, ua, sdk_url, null, lang, langs, elapsed,
        #  capability probe, react listener, event name, perf, sid, "", cores,
        #  origin_ms, zeros..., flags]
        now_ms = int(time.time() * 1000)
        perf_now = 1000 + random.random() * 49000
        locale_date = time.strftime("%a %b %d %Y %H:%M:%S GMT+0800 (China Standard Time)", time.localtime())
        return [
            4800,
            locale_date,
            None,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            "en-US",
            "en-US,en",
            round(5 + random.random() * 45),
            "languages−en-US,en",
            f"_reactListening{secrets.token_hex(6)}",
            random.choice(["onbeforetoggle", "onbeforeunload", "location"]),
            int(perf_now),
            self.sid,
            "",
            random.choice([8, 12, 16]),
            now_ms - int(perf_now),
            0,
            0,
            0,
            0,
            0,
            1,
            1,
        ]



    def generate_requirements_token(self) -> str:

        cfg = self._config()
        # Headed HAR initial /sentinel/req p points at backend-api/sdk.js entry.
        cfg[5] = "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
        cfg[3] = 1
        cfg[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._b64(cfg)



    def generate_token(self, seed: str, difficulty: str) -> str:

        max_attempts = 500000
        cfg = self._config()
        # Headed HAR final create_account p points at versioned sentinel sdk.js.
        cfg[5] = SENTINEL_SDK_URL
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
'''

    # Tolerate both double-newline and single-newline forms by normalizing match via flexible search.
    # Build from actual file content by locating class markers.
    start = text.find("class _SentinelTokenGenerator:")
    if start < 0:
        raise SystemExit("FAIL: class _SentinelTokenGenerator not found")
    end = text.find("class RegistrationEngine:", start)
    if end < 0:
        raise SystemExit("FAIL: class RegistrationEngine not found after generator")

    new_gen = '''class _SentinelTokenGenerator:
    """Dynamic sentinel token generator aligned with headed Firefox HAR."""

    _MINUS = "\\u2212"  # U+2212 minus used by real Sentinel SDK probe strings

    def __init__(self, device_id: str, user_agent: str, *, client_version: str = ""):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent
        self.client_version = str(client_version or LATEST_CHATGPT_OAI_CLIENT_VERSION)
        self.sid = str(uuid.uuid4())
        self._origin_ms = int(time.time() * 1000) - random.randint(8000, 40000)
        self._is_firefox = "Firefox/" in (user_agent or "")
        self._is_mac = "Macintosh" in (user_agent or "") or "Mac OS X" in (user_agent or "")

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

    def _capability_probe(self, *, stage: str) -> str:
        minus = self._MINUS
        if stage == "chat_prepare":
            return random.choice(
                [
                    f"globalPrivacyControl{minus}true",
                    f"vendorSub{minus}",
                    f"productSub{minus}20100101",
                ]
            )
        if stage == "sentinel_req":
            if self._is_firefox:
                return (
                    f"mozGetUserMedia{minus}function mozGetUserMedia() {{\\n"
                    f"    [native code]\\n}}"
                )
            return f"webkitGetUserMedia{minus}function webkitGetUserMedia() {{ [native code] }}"
        # final enforcement token (create_account)
        if self._is_firefox:
            return f"plugins{minus}[object PluginArray]"
        return f"languages{minus}en-US,en"

    def _event_probe(self, *, stage: str) -> str:
        if stage == "chat_prepare":
            return random.choice(["onmouseenter", "InstallTrigger", "onpointerenter", "onwheel"])
        if stage == "sentinel_req":
            return random.choice(["ondragstart", "onanimationstart", "ontransitionrun", "onlostpointercapture"])
        return random.choice(["matchMedia", "location", "onbeforetoggle", "onbeforeunload"])

    def _react_probe(self, *, stage: str) -> str:
        if stage == "chat_prepare":
            return f"__reactContainer${secrets.token_hex(6)}"
        return f"_reactListening{secrets.token_hex(6)}"

    def _config(self, *, stage: str = "final") -> list:
        # Headed HAR shape:
        # [screen, date, null, nonce, ua, script_url, client_or_null, lang, langs, elapsed,
        #  capability, react, event, perf, sid, "", cores, origin_ms, zeros..., flags]
        now_ms = int(time.time() * 1000)
        if stage == "chat_prepare":
            perf_now = random.randint(3000, 12000)
            elapsed = random.randint(1, 8)
            script_url = LATEST_CHATGPT_CF_JSD_SCRIPT_URL
            client_or_null = self.client_version
        elif stage == "sentinel_req":
            perf_now = random.randint(15000, 35000)
            elapsed = random.randint(20, 90)
            script_url = SENTINEL_SDK_URL  # versioned /sentinel/<ver>/sdk.js
            client_or_null = None
        else:
            perf_now = random.randint(20000, 45000)
            elapsed = random.randint(3, 20)
            script_url = LATEST_CHATGPT_SENTINEL_ENTRY_SDK_URL  # backend-api/sentinel/sdk.js
            client_or_null = None

        locale_date = time.strftime(
            "%a %b %d %Y %H:%M:%S GMT+0800 (China Standard Time)",
            time.localtime(),
        )
        screen = LATEST_CHATGPT_SENTINEL_SCREEN if self._is_mac or self._is_firefox else 4800
        cores = LATEST_CHATGPT_SENTINEL_CORES if self._is_mac or self._is_firefox else random.choice([8, 12, 16])
        return [
            screen,
            locale_date,
            None,
            random.random(),
            self.user_agent,
            script_url,
            client_or_null,
            "en-US",
            "en-US,en",
            elapsed,
            self._capability_probe(stage=stage),
            self._react_probe(stage=stage),
            self._event_probe(stage=stage),
            int(perf_now),
            self.sid,
            "",
            cores,
            self._origin_ms,
            0,
            0,
            0,
            0,
            0,
            1,
            1,
        ]

    def generate_requirements_token(self) -> str:
        """Initial /sentinel/req requirements p from headed HAR (versioned sdk.js, gAAAAAC...~S)."""
        cfg = self._config(stage="sentinel_req")
        cfg[3] = random.randint(1, 4)
        return "gAAAAAC" + self._b64(cfg) + "~S"

    def generate_chat_requirements_token(self) -> str:
        """chatgpt.com chat-requirements/prepare p from headed HAR (jsd script + client version)."""
        cfg = self._config(stage="chat_prepare")
        cfg[3] = 1
        return "gAAAAAC" + self._b64(cfg)

    def generate_token(self, seed: str, difficulty: str) -> str:
        """Final enforcement p used on create_account (backend-api/sdk.js, gAAAAAB...~S)."""
        max_attempts = 500000
        cfg = self._config(stage="final")
        start_ms = int(time.time() * 1000)
        diff = str(difficulty or "0")
        for nonce in range(max_attempts):
            cfg[3] = nonce
            cfg[9] = max(1, round(int(time.time() * 1000) - start_ms))
            encoded = self._b64(cfg)
            digest = self._fnv1a32((seed or "") + encoded)
            if digest[: len(diff)] <= diff:
                return "gAAAAAB" + encoded + "~S"
        return "gAAAAAB" + self._b64(None) + "~S"


'''

    text = text[:start] + new_gen + text[end:]

    # json headers: do not send oai-device-id on auth.openai.com (HAR create_account/email-otp don't).
    text = replace_once(
        text,
        (
            "        if self._device_id:\n"
            "            headers[\"oai-device-id\"] = self._device_id\n"
            "        headers[\"x-access-flow-invocation-id\"] = str(uuid.uuid4())\n"
            "        return headers\n"
        ),
        (
            "        # Headed auth.openai.com JSON (email-otp/create_account) does not send oai-device-id;\n"
            "        # device identity is carried by oai-did cookie + sentinel id field.\n"
            "        headers[\"x-access-flow-invocation-id\"] = str(uuid.uuid4())\n"
            "        return headers\n"
        ),
        "json headers oai-device-id",
    )

    # Use chat-requirements specific token in warmup/prepare call sites.
    # Replace generate_requirements_token() only where chat-requirements prepare bodies are built.
    count = 0
    needle = "prepare_p = generator.generate_requirements_token()"
    replacement = "prepare_p = generator.generate_chat_requirements_token()"
    while needle in text:
        text = text.replace(needle, replacement, 1)
        count += 1
    if count == 0:
        print("WARN: no chat-requirements prepare_p replacements")
    else:
        print(f"OK: replaced prepare_p sites={count}")

    # Ensure generator construction can pass client version where easy.
    text = text.replace(
        "generator = _SentinelTokenGenerator(device_id, ua)",
        "generator = _SentinelTokenGenerator(device_id, ua, client_version=LATEST_CHATGPT_OAI_CLIENT_VERSION)",
    )
    text = text.replace(
        "generator = _SentinelTokenGenerator(did, ua)",
        "generator = _SentinelTokenGenerator(did, ua, client_version=LATEST_CHATGPT_OAI_CLIENT_VERSION)",
    )

    PATH.write_text(text, encoding="utf-8")
    print(f"updated {PATH} size={PATH.stat().st_size}")


if __name__ == "__main__":
    main()
