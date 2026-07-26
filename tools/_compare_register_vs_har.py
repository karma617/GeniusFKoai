#!/usr/bin/env python3
"""Compare protocol register headers/constants against browser HAR."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

HAR = Path("tools/captures/register-20260725-010100-ArlanBrando080676_hotmail.com.har")
REG = Path("platforms/chatgpt/register.py")
OUT = Path("tools/_compare_register_vs_har_out.txt")


def hdrs(items):
    return {h["name"].lower(): h["value"] for h in items}


def main() -> None:
    har = json.loads(HAR.read_text(encoding="utf-8"))
    entries = har["log"]["entries"]
    reg = REG.read_text(encoding="utf-8")
    lines: list[str] = []

    interesting = []
    for i, e in enumerate(entries):
        url = e["request"]["url"]
        method = e["request"]["method"]
        if any(
            x in url
            for x in (
                "create_account",
                "email-otp",
                "sentinel/req",
                "api/auth/signin/openai",
                "api/accounts/authorize",
                "about-you",
                "callback/openai",
                "api/auth/session",
                "user/register",
                "password",
                "authorize/continue",
            )
        ):
            if any(x in url for x in (".js", ".css", ".png", ".svg", ".woff")) and "sentinel" not in url:
                continue
            interesting.append((i, e))
            lines.append(f"[{i}] {method} {e['response'].get('status')} {url[:220]}")

    # Extract create_account headers fully and decode sentinel p payload
    lines.append("\n== create_account full interesting headers ==")
    for i, e in interesting:
        if "create_account" not in e["request"]["url"]:
            continue
        h = hdrs(e["request"].get("headers", []))
        for k in sorted(h):
            if k.startswith(":"):
                continue
            v = h[k]
            if k == "cookie":
                names = [p.split("=")[0].strip() for p in v.split(";") if "=" in p]
                lines.append(f"cookie_names={names}")
            else:
                lines.append(f"{k}: {v[:400]}")
        post = (e["request"].get("postData") or {}).get("text") or ""
        lines.append(f"body={post}")
        for key in ("openai-sentinel-token", "openai-sentinel-so-token"):
            raw = h.get(key, "")
            if not raw:
                continue
            obj = json.loads(raw)
            lines.append(f"{key} keys={list(obj.keys())}")
            for kk, vv in obj.items():
                if isinstance(vv, str) and kk in ("p", "t", "c", "so"):
                    # try base64-ish decode of p after gAAAAA
                    lines.append(f"  {kk}: len={len(vv)} head={vv[:80]}")
                    if kk == "p" and vv.startswith("gAAAAA"):
                        import base64

                        b64 = vv[6:]
                        # pad
                        pad = (-len(b64)) % 4
                        try:
                            decoded = base64.b64decode(b64 + "=" * pad + "==")
                            # may not be pure b64; try ignore
                        except Exception:
                            decoded = b""
                        # often payload after prefix is not pure; try extract printable from raw
                        try:
                            # OpenAI sentinel p is gAAAAA + base64(json array like)
                            # The HAR shows p starts with gAAAAAB then base64 of timestamp json
                            raw2 = vv
                            # Find JSON-ish after decoding via latin
                            # Alternative: the string after gAAAAA looks base64 of utf8 starting with WzI0...
                            # From HAR analysis: head=gAAAAABWzI0OTQsIlNhdC... which is gAAAAA + B + base64?
                            # Actually looking: gAAAAABWzI0OTQsIlNhdCBKdWwg...
                            # base64 of '["Sat Jul...' starts with WzI0OT... so prefix is gAAAAAB then Wz...
                            # Wait: gAAAAAB + WzI0OTQs...
                            # decode from Wz...
                            idx = raw2.find("Wz")
                            if idx > 0:
                                chunk = raw2[idx:]
                                # strip trailing ~S if present
                                chunk = chunk.split("~")[0]
                                pad = (-len(chunk)) % 4
                                try:
                                    dec = base64.b64decode(chunk + "=" * pad)
                                    lines.append(f"  p_decoded={dec[:500]!r}")
                                except Exception as ex:
                                    lines.append(f"  p_decode_fail={ex}")
                        except Exception as ex:
                            lines.append(f"  p_parse_err={ex}")

    # sentinel/req body decode
    lines.append("\n== sentinel/req bodies ==")
    for i, e in interesting:
        if "sentinel/req" not in e["request"]["url"]:
            continue
        post = (e["request"].get("postData") or {}).get("text") or ""
        h = hdrs(e["request"].get("headers", []))
        lines.append(f"[{i}] referer={h.get('referer','')}")
        lines.append(f"body={post[:800]}")
        try:
            obj = json.loads(post)
            p = obj.get("p", "")
            idx = p.find("Wz")
            if idx > 0:
                import base64

                chunk = p[idx:].split("~")[0]
                pad = (-len(chunk)) % 4
                dec = base64.b64decode(chunk + "=" * pad)
                lines.append(f"p_decoded={dec[:800]!r}")
        except Exception as ex:
            lines.append(f"decode_err={ex}")

    # constants present in register.py
    patterns = [
        r"LATEST_CHATGPT_FIREFOX_USER_AGENT\s*=\s*[\"']([^\"']+)",
        r"PLATFORM_REFERENCE_SEC_CH_UA\s*=\s*[\"']([^\"']+)",
        r"oauth_create_account",
        r"openai-sentinel-so-token",
        r"x-access-flow-invocation-id",
        r"oaicom-stable-id",
        r"oai-asli",
        r"cf_clearance",
        r"Mozilla/5.0",
        r"sec-ch-ua",
        r"Firefox/135",
        r"Chrome/",
        r"screen_hint",
        r"login_or_signup",
        r"passwordless",
        r"auth_session_logging_id",
        r"sentinel/frame",
        r"20260219f9f6",
        r"sdk\.js",
    ]
    lines.append("\n== register.py constant/string presence ==")
    for pat in patterns:
        m = re.search(pat, reg)
        if m:
            g = m.group(0)
            if m.lastindex:
                g = m.group(0) + " => " + m.group(1)[:120]
            lines.append(f"FOUND {pat}: {g[:200]}")
        else:
            lines.append(f"MISSING {pat}")

    # Find header builder functions
    lines.append("\n== header builder defs ==")
    for m in re.finditer(r"def (_\w*header\w*|_platform\w*|_latest\w*|_check_sentinel\w*|_quickjs\w*|_create_account\w*)", reg):
        lines.append(f"L{reg[:m.start()].count(chr(10))+1}: {m.group(0)}")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} lines={len(lines)}")


if __name__ == "__main__":
    main()
