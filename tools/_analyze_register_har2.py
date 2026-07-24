#!/usr/bin/env python3
"""Deeper ChatGPT register HAR analysis: auth endpoints + headers."""
from __future__ import annotations

import json
import re
from collections import Counter

HAR_PATH = r"tools/captures/register-20260725-010100-ArlanBrando080676_hotmail.com.har"
OUT = r"tools/_har_analysis_out2.txt"


def hdrs(items):
    return {h["name"].lower(): h["value"] for h in items}


def main():
    with open(HAR_PATH, "r", encoding="utf-8") as f:
        har = json.load(f)
    entries = har["log"]["entries"]
    lines = []

    # host frequency for non-static
    hosts = Counter()
    for e in entries:
        url = e["request"]["url"]
        if any(x in url for x in (".css", ".js", ".woff", ".svg", ".png", ".webp", ".jpg", "cdn/assets")):
            continue
        host = url.split("/")[2]
        hosts[host] += 1
    lines.append("== hosts (non-static) ==")
    for h, c in hosts.most_common():
        lines.append(f"{c:3d}  {h}")

    # list all non-static requests with method/url/status
    lines.append("\n== non-static requests ==")
    for i, e in enumerate(entries):
        req = e["request"]
        url = req["url"]
        if any(x in url for x in (".css", ".woff", ".svg", ".png", ".webp", ".jpg", "/cdn/assets/", ".ico")):
            continue
        if url.endswith(".js") and "challenge" not in url and "castle" not in url.lower():
            continue
        status = e["response"].get("status")
        lines.append(f"[{i}] {req['method']} {status} {url[:220]}")

    # auth.openai.com posts in detail
    lines.append("\n== auth.openai.com POST detail ==")
    for i, e in enumerate(entries):
        req = e["request"]
        if "auth.openai.com" not in req["url"]:
            continue
        if req["method"] not in ("POST", "PUT", "PATCH"):
            # still show navigate-ish GETs briefly
            if req["method"] == "GET" and any(k in req["url"] for k in ("/api/", "authorize", "callback", "continue", "create", "register", "signup", "oauth", "session", "user")):
                pass
            else:
                continue
        h = hdrs(req.get("headers", []))
        post = req.get("postData", {}) or {}
        text = post.get("text") or ""
        resp = e.get("response", {})
        rh = hdrs(resp.get("headers", []))
        rtext = ""
        content = resp.get("content", {}) or {}
        rtext = content.get("text") or ""
        if content.get("encoding") == "base64":
            rtext = f"<base64 len={len(rtext)}>"
        lines.append("\n" + "=" * 80)
        lines.append(f"[{i}] {req['method']} {req['url'][:250]}")
        lines.append(f"  status={resp.get('status')} req_body_len={len(text)} resp_body_len={len(rtext) if isinstance(rtext,str) else 0}")
        for k in sorted(h):
            if k.startswith(":"):
                continue
            v = h[k]
            if k == "cookie":
                names = [p.split("=")[0].strip() for p in v.split(";") if "=" in p]
                lines.append(f"  cookie_names={names}")
                # print individual important cookies
                for p in v.split(";"):
                    p = p.strip()
                    if not p or "=" not in p:
                        continue
                    n, val = p.split("=", 1)
                    if any(x in n.lower() for x in ("cf", "castle", "session", "csrf", "oai", "auth", "did", "device", "login")):
                        lines.append(f"    {n}={val[:120]}")
            else:
                lines.append(f"  H {k}: {v[:400]}")
        if text:
            preview = text[:1500].replace("\n", "\\n")
            lines.append(f"  BODY: {preview}")
            for key in ("castle", "conversion", "turnstile", "token", "sentinel", "device", "browser"):
                if key.lower() in text.lower():
                    # extract nearby
                    for m in re.finditer(re.escape(key), text, re.I):
                        start = max(0, m.start() - 40)
                        end = min(len(text), m.end() + 120)
                        lines.append(f"  CTX[{key}]: ...{text[start:end]}...")
                        break
        if rtext and isinstance(rtext, str) and not rtext.startswith("<base64"):
            lines.append(f"  RESP: {rtext[:800].replace(chr(10), '\\\\n')}")
        sc = [x["value"] for x in resp.get("headers", []) if x["name"].lower() == "set-cookie"]
        for c in sc:
            lines.append(f"  Set-Cookie: {c[:250]}")

    # also openai.com / chatgpt registration related API
    lines.append("\n== chatgpt/openai API posts with auth-ish paths ==")
    for i, e in enumerate(entries):
        req = e["request"]
        url = req["url"]
        if req["method"] not in ("POST", "PUT"):
            continue
        if not any(h in url for h in ("auth.openai.com", "chatgpt.com", "api.openai.com", "ab.chatgpt.com")):
            continue
        if any(x in url for x in ("/ces/", "/cdn/", "rgstr", "telemetry", "datadog", "sentry")):
            continue
        h = hdrs(req.get("headers", []))
        post = req.get("postData", {}) or {}
        text = post.get("text") or ""
        status = e["response"].get("status")
        lines.append(f"\n[{i}] {req['method']} {status} {url[:200]}")
        # headers of interest
        for k in ("content-type", "oai-device-id", "oai-language", "openai-sentinel-token", "authorization", "cookie", "referer", "origin", "user-agent", "x-castle-request-token", "castle-request-token"):
            if k in h:
                v = h[k]
                if k == "cookie":
                    names = [p.split("=")[0].strip() for p in v.split(";") if "=" in p]
                    lines.append(f"  cookie_names={names}")
                else:
                    lines.append(f"  {k}: {v[:300]}")
        if text:
            lines.append(f"  body={text[:600].replace(chr(10),'\\\\n')}")

    # search entire har for castle strings
    lines.append("\n== global string search ==")
    raw_hits = Counter()
    patterns = [
        "castle",
        "Castle",
        "conversionId",
        "conversion_id",
        "createRequestToken",
        "castleRequestToken",
        "pk_",
        "sentinel",
        "openai-sentinel",
        "oai-did",
        "device_id",
        "cf_clearance",
        "__cf_bm",
        "turnstile",
    ]
    # only scan request urls/headers/bodies and response texts carefully
    for i, e in enumerate(entries):
        blob_parts = [e["request"]["url"]]
        for h in e["request"].get("headers", []):
            blob_parts.append(f"{h['name']}:{h['value']}")
        post = e["request"].get("postData", {}) or {}
        if post.get("text"):
            blob_parts.append(post["text"][:5000])
        content = e.get("response", {}).get("content", {}) or {}
        if content.get("text") and content.get("encoding") != "base64":
            blob_parts.append((content.get("text") or "")[:3000])
        blob = "\n".join(blob_parts)
        for p in patterns:
            if p in blob:
                raw_hits[p] += 1
    for p, c in raw_hits.most_common():
        lines.append(f"{c:3d}  {p}")

    # dump entries that mention castle anywhere
    lines.append("\n== entries mentioning castle ==")
    for i, e in enumerate(entries):
        req = e["request"]
        parts = [req["url"]]
        for h in req.get("headers", []):
            parts.append(h["value"])
        post = req.get("postData", {}) or {}
        if post.get("text"):
            parts.append(post["text"])
        content = e.get("response", {}).get("content", {}) or {}
        if content.get("text") and content.get("encoding") != "base64":
            parts.append(content["text"][:2000])
        blob = "\n".join(parts)
        if "castle" in blob.lower():
            lines.append(f"[{i}] {req['method']} {req['url'][:180]}")
            # show matching headers
            for h in req.get("headers", []):
                if "castle" in h["name"].lower() or "castle" in h["value"].lower():
                    lines.append(f"  H {h['name']}: {h['value'][:200]}")
            if post.get("text") and "castle" in post["text"].lower():
                lines.append(f"  body has castle: {post['text'][:400]}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {OUT} lines={len(lines)}")


if __name__ == "__main__":
    main()
