#!/usr/bin/env python3
"""Verify protocol sentinel p shapes against headed HAR rules."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from platforms.chatgpt.register import (  # noqa: E402
    LATEST_CHATGPT_CF_JSD_SCRIPT_URL,
    LATEST_CHATGPT_FIREFOX_USER_AGENT,
    LATEST_CHATGPT_OAI_CLIENT_BUILD_NUMBER,
    LATEST_CHATGPT_OAI_CLIENT_VERSION,
    LATEST_CHATGPT_SENTINEL_ENTRY_SDK_URL,
    LATEST_CHATGPT_SENTINEL_SCREEN,
    _SentinelTokenGenerator,
)
from platforms.chatgpt.constants import SENTINEL_SDK_URL  # noqa: E402


def decode_p(p: str):
    raw = p
    suffix = ""
    if raw.endswith("~S"):
        raw = raw[:-2]
        suffix = "~S"
    prefix = ""
    for pref in ("gAAAAAC", "gAAAAAB", "gAAAAA"):
        if raw.startswith(pref):
            prefix = pref
            raw = raw[len(pref):]
            break
    idx = raw.find("Wz")
    if idx < 0:
        idx = 0
    chunk = raw[idx:]
    pad = (-len(chunk)) % 4
    arr = json.loads(base64.b64decode(chunk + "=" * pad).decode("utf-8"))
    return prefix, suffix, arr


def main() -> None:
    ua = LATEST_CHATGPT_FIREFOX_USER_AGENT
    gen = _SentinelTokenGenerator("15a3b48e-c795-4c96-a999-ab526a97901d", ua)

    req_p = gen.generate_requirements_token()
    pref, suf, arr = decode_p(req_p)
    assert pref == "gAAAAAC", pref
    assert suf == "~S", suf
    assert arr[0] == LATEST_CHATGPT_SENTINEL_SCREEN, arr[0]
    assert arr[4] == ua
    assert arr[5] == SENTINEL_SDK_URL, arr[5]
    assert arr[5].startswith("https://sentinel.openai.com/sentinel/")
    assert arr[5].endswith("/sdk.js")
    assert arr[6] is None
    assert "mozGetUserMedia" in str(arr[10])
    print("OK sentinel_req", arr[5], arr[10][:40])

    final_p = gen.generate_token("seed", "0")
    pref, suf, arr = decode_p(final_p)
    assert pref == "gAAAAAB", pref
    assert suf == "~S", suf
    assert arr[5] == LATEST_CHATGPT_SENTINEL_ENTRY_SDK_URL, arr[5]
    assert "plugins" in str(arr[10])
    print("OK final", arr[5], arr[10])

    chat_p = gen.generate_chat_requirements_token()
    pref, suf, arr = decode_p(chat_p)
    assert pref == "gAAAAAC", pref
    assert suf == ""
    assert arr[5] == LATEST_CHATGPT_CF_JSD_SCRIPT_URL, arr[5]
    assert arr[6] == LATEST_CHATGPT_OAI_CLIENT_VERSION, arr[6]
    print("OK chat_prepare", arr[5], arr[6])

    assert LATEST_CHATGPT_OAI_CLIENT_VERSION.startswith("prod-2c08737")
    assert LATEST_CHATGPT_OAI_CLIENT_BUILD_NUMBER == "8578659"
    assert "Macintosh" in LATEST_CHATGPT_FIREFOX_USER_AGENT
    print("ALL SHAPE CHECKS PASSED")


if __name__ == "__main__":
    main()
