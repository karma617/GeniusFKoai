#!/usr/bin/env python3
"""Analyze Grok register HAR for headers / castle / conversionId patterns."""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from urllib.parse import urlparse

HAR_PATH = r"tools/captures/register-20260725-010100-ArlanBrando080676_hotmail.com.har"
OUT = r"tools/_har_analysis_out.txt"


def hdrs(req):
    return {h["name"].lower(): h["value"] for h in req.get("headers", [])}


def main():
    with open(HAR_PATH, "r", encoding="utf-8") as f:
        har = json.load(f)
    entries = har["log"]["entries"]
    lines = []
    lines.append(f"size={os.path.getsize(HAR_PATH)} entries={len(entries)}")

    bases = Counter()
    for e in entries:
        req = e["request"]
        base = req["url"].split("?")[0]
        bases[f"{req['method']} {base}"] += 1
    lines.append("\n== URL frequency ==")
    for u, c in bases.most_common(60):
        lines.append(f"{c:3d}  {u}")

    keywords = (
        "castle",
        "turnstile",
        "conversion",
        "CreateEmail",
        "VerifyEmail",
        "CreateUser",
        "sign-up",
        "sign_up",
        "auth_mgmt",
        "set-cookie",
        "oauth",
        "challenge",
        "cf-challenge",
        "challenges.cloudflare",
    )
    lines.append("\n== interesting entries ==")
    for i, e in enumerate(entries):
        req = e["request"]
        url = req["url"]
        if not any(k.lower() in url.lower() for k in keywords):
            # also include accounts.x.ai posts
            if "accounts.x.ai" not in url and "auth.x.ai" not in url and "castle" not in url.lower():
                continue
            if req["method"] == "GET" and "static" in url:
                continue
        h = hdrs(req)
        post = req.get("postData", {}) or {}
        text = post.get("text") or ""
        mime = post.get("mimeType") or ""
        resp = e.get("response", {})
        rh = {x["name"].lower(): x["value"] for x in resp.get("headers", [])}
        lines.append("\n" + "=" * 80)
        lines.append(f"[{i}] {req['method']} {url[:200]}")
        lines.append(f"  status={resp.get('status')} mime={mime} body_len={len(text)}")
        # key request headers
        keep = [
            "content-type",
            "origin",
            "referer",
            "user-agent",
            "cookie",
            "x-grpc-web",
            "next-action",
            "next-router-state-tree",
            "accept",
            "accept-language",
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
            "sec-fetch-site",
            "sec-fetch-mode",
            "sec-fetch-dest",
            "priority",
            "x-castle-request-token",
            "castle-request-token",
        ]
        for k in keep:
            if k in h:
                v = h[k]
                if k == "cookie":
                    # list cookie names
                    names = [p.split("=")[0].strip() for p in v.split(";") if "=" in p]
                    lines.append(f"  cookie_names={names}")
                    lines.append(f"  cookie_preview={v[:300]}")
                else:
                    lines.append(f"  {k}: {v[:300]}")
        # body preview
        if text:
            if "proto" in mime or text.startswith("\x00") or "grpc" in mime:
                # hex head
                b = text.encode("latin1", errors="ignore") if isinstance(text, str) else text
                # HAR may store binary as escaped or as base64
                enc = post.get("encoding")
                if enc == "base64":
                    import base64
                    b = base64.b64decode(text)
                lines.append(f"  body_hex_head={b[:80].hex()}")
                lines.append(f"  body_len_bytes={len(b)}")
                # try extract ascii strings
                strs = re.findall(rb"[\x20-\x7e]{6,}", b)
                lines.append(f"  body_strings={[s.decode() for s in strs[:30]]}")
            else:
                preview = text[:800].replace("\n", "\\n")
                lines.append(f"  body_preview={preview}")
                # search castle / conversion
                for key in ("castle", "conversion", "turnstile", "requestToken", "RequestToken"):
                    if key.lower() in text.lower():
                        lines.append(f"  ** contains '{key}'")
        # set-cookie response
        sc = [x["value"] for x in resp.get("headers", []) if x["name"].lower() == "set-cookie"]
        if sc:
            lines.append(f"  set-cookie count={len(sc)}")
            for c in sc[:10]:
                lines.append(f"    {c[:200]}")

    # specifically search post bodies for castleRequestToken / conversionId
    lines.append("\n== body keyword scan ==")
    for i, e in enumerate(entries):
        post = e["request"].get("postData", {}) or {}
        text = post.get("text") or ""
        if not text:
            continue
        low = text.lower()
        hits = [k for k in ("castlerequesttoken", "castle", "conversionid", "conversion_id", "turnstile", "createrequesttoken") if k in low]
        if hits:
            lines.append(f"[{i}] {e['request']['method']} {e['request']['url'][:120]} hits={hits}")
            lines.append(f"  body={text[:500]}")

    out = "\n".join(lines)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"wrote {OUT} lines={len(lines)}")


if __name__ == "__main__":
    main()
