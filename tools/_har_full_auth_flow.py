#!/usr/bin/env python3
"""List all auth/openai related non-static requests in chronological order with key headers."""
from __future__ import annotations
import json
from pathlib import Path

HAR = Path("tools/captures/register-20260725-010100-ArlanBrando080676_hotmail.com.har")
OUT = Path("tools/_har_full_auth_flow.txt")

def hdrs(items):
    return {h["name"].lower(): h["value"] for h in items}

def main():
    har = json.loads(HAR.read_text(encoding="utf-8"))
    lines = []
    for i, e in enumerate(har["log"]["entries"]):
        req = e["request"]
        url = req["url"]
        if any(x in url for x in (".css", ".png", ".jpg", ".svg", ".woff", ".ico", "cdn/assets", "datadoghq", "sentry.io", "google-analytics", "doubleclick", "facebook", "hotjar")):
            continue
        host = url.split("/")[2] if "://" in url else ""
        keep_host = any(h in host for h in ("auth.openai.com", "chatgpt.com", "sentinel.openai.com", "ab.chatgpt.com", "api.openai.com"))
        if not keep_host:
            continue
        # skip pure static js except sentinel/challenge
        if url.endswith(".js") and "sentinel" not in url and "challenge" not in url and "sdk" not in url:
            continue
        h = hdrs(req.get("headers", []))
        post = (req.get("postData") or {}).get("text") or ""
        status = e["response"].get("status")
        lines.append(f"\n[{i}] {req['method']} {status} {url[:240]}")
        for k in (
            "user-agent", "accept", "accept-language", "content-type", "origin", "referer",
            "oai-device-id", "oai-language", "oai-client-version", "oai-client-build-number",
            "oai-session-id", "openai-sentinel-token", "openai-sentinel-so-token",
            "x-access-flow-invocation-id", "sec-ch-ua", "sec-ch-ua-platform", "sec-fetch-site",
            "sec-fetch-mode", "sec-fetch-dest", "x-openai-target-path",
        ):
            if k in h:
                v = h[k]
                if "sentinel" in k:
                    try:
                        obj = json.loads(v)
                        lines.append(f"  {k}: keys={list(obj.keys())} flow={obj.get('flow')} id={obj.get('id')} lens={{k:len(str(obj.get(k) or '')) for k in obj}}")
                    except Exception:
                        lines.append(f"  {k}: {v[:120]}")
                else:
                    lines.append(f"  {k}: {v[:180]}")
        if post:
            lines.append(f"  body[{len(post)}]: {post[:300].replace(chr(10),' ')}")
        # set-cookies of interest
        for sc in e["response"].get("headers", []):
            if sc["name"].lower() == "set-cookie":
                name = sc["value"].split("=",1)[0]
                if any(x in name.lower() for x in ("oai", "cf_", "__cf", "auth", "login", "session", "csrf", "usc_")):
                    lines.append(f"  Set-Cookie: {sc['value'][:160]}")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT, "lines", len(lines))

if __name__ == "__main__":
    main()
