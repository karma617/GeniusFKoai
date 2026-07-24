#!/usr/bin/env python3
"""Extract full headers for key ChatGPT register steps from browser HAR."""
from __future__ import annotations

import json
import re

HAR = r"tools/captures/register-20260725-010100-ArlanBrando080676_hotmail.com.har"
OUT = r"tools/_har_analysis_out3.txt"

TARGETS = (
    "api/auth/signin/openai",
    "api/accounts/authorize",
    "email-otp/validate",
    "create_account",
    "sentinel/req",
    "api/auth/callback/openai",
    "api/auth/session",
    "email-verification",
    "about-you",
)


def main():
    with open(HAR, encoding="utf-8") as f:
        har = json.load(f)
    entries = har["log"]["entries"]
    lines = []

    for i, e in enumerate(entries):
        req = e["request"]
        url = req["url"]
        if not any(t in url for t in TARGETS):
            continue
        if any(x in url for x in (".js", ".css", "challenge-platform")) and "sentinel" not in url:
            continue
        h = {x["name"].lower(): x["value"] for x in req.get("headers", [])}
        post = (req.get("postData") or {}).get("text") or ""
        resp = e.get("response", {})
        rcontent = (resp.get("content") or {}).get("text") or ""
        if (resp.get("content") or {}).get("encoding") == "base64":
            rcontent = f"<base64 {len(rcontent)}>"
        lines.append("\n" + "=" * 88)
        lines.append(f"[{i}] {req['method']} {resp.get('status')} {url[:240]}")
        for k in sorted(h):
            if k.startswith(":"):
                continue
            v = h[k]
            if k == "cookie":
                names = [p.split("=")[0].strip() for p in v.split(";") if "=" in p]
                lines.append(f"  cookie_names={names}")
                for p in v.split(";"):
                    p = p.strip()
                    if "=" not in p:
                        continue
                    n, val = p.split("=", 1)
                    lines.append(f"    C {n}={val[:160]}")
            else:
                lines.append(f"  {k}: {v[:500]}")
        if post:
            lines.append(f"  BODY({len(post)}): {post[:1200]}")
        if rcontent and isinstance(rcontent, str):
            lines.append(f"  RESP({len(rcontent)}): {rcontent[:1000]}")
        sc = [x["value"] for x in resp.get("headers", []) if x["name"].lower() == "set-cookie"]
        for c in sc:
            lines.append(f"  Set-Cookie: {c[:300]}")

    # decode create_account sentinel token structure
    lines.append("\n== sentinel token structure on create_account ==")
    for i, e in enumerate(entries):
        if "create_account" not in e["request"]["url"]:
            continue
        h = {x["name"].lower(): x["value"] for x in e["request"].get("headers", [])}
        for key in ("openai-sentinel-token", "openai-sentinel-so-token"):
            raw = h.get(key, "")
            lines.append(f"\n{key} len={len(raw)}")
            try:
                obj = json.loads(raw)
                lines.append(f"  keys={list(obj.keys())}")
                for k, v in obj.items():
                    if isinstance(v, str):
                        lines.append(f"  {k}: str len={len(v)} head={v[:120]}")
                    else:
                        lines.append(f"  {k}: {type(v).__name__}={v}")
            except Exception as ex:
                lines.append(f"  parse fail: {ex}; raw_head={raw[:200]}")

    # also check authorize/continue if any
    lines.append("\n== urls containing authorize/continue/register/password ==")
    for i, e in enumerate(entries):
        url = e["request"]["url"]
        if any(x in url for x in ("authorize/continue", "user/register", "password", "passwordless", "email-otp")):
            lines.append(f"[{i}] {e['request']['method']} {e['response'].get('status')} {url[:200]}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("wrote", OUT, "lines", len(lines))


if __name__ == "__main__":
    main()
