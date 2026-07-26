#!/usr/bin/env python3
from pathlib import Path

path = Path("tests/test_chatgpt_protocol_otp.py")
text = path.read_text(encoding="utf-8")

# Update json headers test: auth.openai.com HAR does not send oai-device-id.
old = '''def test_latest_chatgpt_json_headers_include_access_flow_id():
'''
if old not in text:
    raise SystemExit("test function not found")

# replace the assertion block near that test
marker = "def test_latest_chatgpt_json_headers_include_access_flow_id():"
start = text.find(marker)
end = text.find("\ndef ", start + 1)
block = text[start:end]
if "assert headers[\"oai-device-id\"] == \"device-id\"" not in block:
    # maybe already changed
    print("WARN: oai-device-id assert not in first test block")
else:
    block2 = block.replace(
        "assert headers[\"oai-device-id\"] == \"device-id\"",
        "assert \"oai-device-id\" not in headers  # headed auth.openai.com JSON omits this header",
    )
    text = text[:start] + block2 + text[end:]

# Append dedicated shape tests if missing
if "def test_sentinel_token_shapes_match_headed_har():" not in text:
    text += '''

def test_sentinel_token_shapes_match_headed_har():
    import base64
    import json
    from platforms.chatgpt.constants import SENTINEL_SDK_URL
    from platforms.chatgpt.register import (
        LATEST_CHATGPT_CF_JSD_SCRIPT_URL,
        LATEST_CHATGPT_FIREFOX_USER_AGENT,
        LATEST_CHATGPT_OAI_CLIENT_VERSION,
        LATEST_CHATGPT_SENTINEL_ENTRY_SDK_URL,
        _SentinelTokenGenerator,
    )

    def _decode(p: str):
        raw = p
        suffix = ""
        if raw.endswith("~S"):
            raw = raw[:-2]
            suffix = "~S"
        for pref in ("gAAAAAC", "gAAAAAB", "gAAAAA"):
            if raw.startswith(pref):
                prefix = pref
                raw = raw[len(pref):]
                break
        else:
            prefix = ""
        idx = raw.find("Wz")
        if idx < 0:
            idx = 0
        chunk = raw[idx:]
        pad = (-len(chunk)) % 4
        arr = json.loads(base64.b64decode(chunk + "=" * pad).decode("utf-8"))
        return prefix, suffix, arr

    gen = _SentinelTokenGenerator("did", LATEST_CHATGPT_FIREFOX_USER_AGENT)
    pref, suf, arr = _decode(gen.generate_requirements_token())
    assert pref == "gAAAAAC"
    assert suf == "~S"
    assert arr[5] == SENTINEL_SDK_URL
    assert "mozGetUserMedia" in str(arr[10])

    pref, suf, arr = _decode(gen.generate_token("seed", "0"))
    assert pref == "gAAAAAB"
    assert suf == "~S"
    assert arr[5] == LATEST_CHATGPT_SENTINEL_ENTRY_SDK_URL
    assert "plugins" in str(arr[10])

    pref, suf, arr = _decode(gen.generate_chat_requirements_token())
    assert pref == "gAAAAAC"
    assert suf == ""
    assert arr[5] == LATEST_CHATGPT_CF_JSD_SCRIPT_URL
    assert arr[6] == LATEST_CHATGPT_OAI_CLIENT_VERSION
    assert "Macintosh" in LATEST_CHATGPT_FIREFOX_USER_AGENT
'''

path.write_text(text, encoding="utf-8")
print("tests patched")
