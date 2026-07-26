#!/usr/bin/env python3
"""Decode all sentinel p payloads from HAR and compare shapes."""
from __future__ import annotations
import base64
import json
from pathlib import Path

HAR = Path("tools/captures/register-20260725-010100-ArlanBrando080676_hotmail.com.har")
OUT = Path("tools/_har_p_payloads.txt")


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
    # find base64 json array start
    idx = raw.find("Wz")
    if idx < 0:
        idx = 0
    chunk = raw[idx:]
    pad = (-len(chunk)) % 4
    try:
        dec = base64.b64decode(chunk + "=" * pad)
        arr = json.loads(dec.decode("utf-8"))
        return prefix, suffix, arr
    except Exception as ex:
        return prefix, suffix, f"fail:{ex} raw_head={raw[:80]}"


def main():
    har = json.loads(HAR.read_text(encoding="utf-8"))
    lines = []
    for i, e in enumerate(har["log"]["entries"]):
        url = e["request"]["url"]
        post = (e["request"].get("postData") or {}).get("text") or ""
        h = {x["name"].lower(): x["value"] for x in e["request"].get("headers", [])}
        # body p
        if post and ("\"p\"" in post or post.startswith("{")):
            try:
                obj = json.loads(post)
            except Exception:
                obj = None
            if isinstance(obj, dict) and isinstance(obj.get("p"), str) and obj["p"].startswith("gAAAAA"):
                pref, suf, arr = decode_p(obj["p"])
                lines.append(f"\n[{i}] BODY p {e['request']['method']} {url[:160]}")
                lines.append(f"  prefix={pref} suffix={suf}")
                if isinstance(arr, list):
                    for idx, v in enumerate(arr):
                        lines.append(f"  [{idx}] {v!r}")
                else:
                    lines.append(f"  {arr}")
        # header sentinel tokens
        for key in ("openai-sentinel-token", "openai-sentinel-so-token"):
            raw = h.get(key)
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj.get("p"), str):
                pref, suf, arr = decode_p(obj["p"])
                lines.append(f"\n[{i}] HDR {key}.p {url[:160]}")
                lines.append(f"  prefix={pref} suffix={suf} flow={obj.get('flow')} keys={list(obj.keys())}")
                if isinstance(arr, list):
                    for idx, v in enumerate(arr):
                        lines.append(f"  [{idx}] {v!r}")
                else:
                    lines.append(f"  {arr}")
            if "so" in obj:
                lines.append(f"  so_len={len(str(obj.get('so') or ''))} c_len={len(str(obj.get('c') or ''))}")
    # also extract oai-client-version values
    lines.append("\n== oai-client-version values ==")
    seen = set()
    for e in har["log"]["entries"]:
        h = {x["name"].lower(): x["value"] for x in e["request"].get("headers", [])}
        v = h.get("oai-client-version")
        b = h.get("oai-client-build-number")
        if v or b:
            key = (v, b)
            if key not in seen:
                seen.add(key)
                lines.append(f"version={v} build={b} url={e['request']['url'][:120]}")
    # ua values
    lines.append("\n== user-agent values ==")
    uas = set()
    for e in har["log"]["entries"]:
        h = {x["name"].lower(): x["value"] for x in e["request"].get("headers", [])}
        if "user-agent" in h:
            uas.add(h["user-agent"])
    for ua in sorted(uas):
        lines.append(ua)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT, "lines", len(lines))


if __name__ == "__main__":
    main()
