"""QuickJS-driven Sentinel token generator.

Adapted from
https://github.com/zc-zhangchen/any-auto-register
platforms/chatgpt/sentinel_browser.py:`_get_sentinel_token_via_quickjs`
+ scripts/js/openai_sentinel_quickjs.js (MIT License).

Why this exists:
  Pure-Python `sentinel.py` computes a synthetic PoW that *passes* OpenAI's
  surface validation (200 OK on `/sentinel/req`, `/authorize/continue`, etc.)
  but the OTP-dispatch service runs the actual sentinel SDK JS server-side
  to verify the token. Our synthetic token fails the deeper check → email
  silent-drop. To pass, we must run OpenAI's real `sdk.js` (downloaded from
  `sentinel.openai.com/sentinel/<ver>/sdk.js`) inside a JS VM and emit the
  same token the real browser would.

Implementation:
  - Spawn `node -e <wrapper>` per token request
  - Wrapper loads OpenAI's sdk.js + `openai_sentinel_quickjs.js` (a thin
    adapter that exposes `requirements`/`solve` actions over stdin/stdout)
  - Two passes: action=requirements → `request_p`, then `/sentinel/req` →
    challenge, then action=solve → `final_p` + `t`
  - Returns the same JSON-string shape `{p, t, c, id, flow}` as our
    pure-Python `build_sentinel_token`, so callers don't need to change

Public API:
  - `get_sentinel_token_via_quickjs(session, device_id, flow, ...) -> str | None`
  - `get_sentinel_tokens_via_quickjs(session, device_id, flow, ...) -> dict | None`
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from platforms.chatgpt.constants import get_latest_sentinel_frame_url, get_latest_sentinel_sdk_url

logger = logging.getLogger(__name__)


SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"


def _resolve_node_binary() -> str:
    return (os.getenv("OPENAI_SENTINEL_NODE_PATH", "") or "").strip() or "node"


def _quickjs_script_path() -> Path:
    return Path(__file__).resolve().parent / "openai_sentinel_quickjs.js"


def _accept_language_parts(accept_language: str) -> tuple[str, list[str]]:
    values = []
    for part in str(accept_language or "").split(","):
        lang = part.split(";", 1)[0].strip()
        if lang:
            values.append(lang)
    if not values:
        values = ["en-US", "en"]
    return values[0], values


def _session_cookie_header(session: Any, device_id: str) -> str:
    parts: list[str] = []
    cookies = getattr(session, "cookies", None)
    try:
        items = cookies.get_dict().items() if hasattr(cookies, "get_dict") else []
        for key, value in items:
            if key and value is not None:
                parts.append(f"{key}={value}")
    except Exception:
        pass
    if not any(part.startswith("oai-did=") for part in parts):
        parts.append(f"oai-did={device_id}")
    return "; ".join(parts)


def _runtime_profile(user_agent: str, sdk_url: str) -> dict[str, Any]:
    ua = str(user_agent or "")
    is_mac = "Macintosh" in ua or "Mac OS X" in ua
    version = sdk_url.rstrip("/").split("/")[-2] if "/sentinel/" in sdk_url else "latest"
    return {
        "frame_url": f"https://chatgpt.com/backend-api/sentinel/frame.html?sv={version}",
        "page_url": "https://auth.openai.com/email-verification",
        "platform": "MacIntel" if is_mac else "Win32",
        "vendor": "" if "Firefox/" in ua else "Google Inc.",
        "hardware_concurrency": 10 if is_mac else 8,
        "screen_width": 2560,
        "screen_height": 1440,
        "screen_avail_width": 2560,
        "screen_avail_height": 1440,
        "viewport_width": 1800,
        "viewport_height": 839,
        "color_depth": 30 if is_mac else 24,
        "pixel_depth": 30 if is_mac else 24,
        "timezone": "Asia/Shanghai",
        "timezone_offset_min": -480,
    }


def _ensure_sdk_file(session: Any, timeout_ms: int, accept_language: str) -> Path:
    """Download OpenAI's actual sdk.js to /tmp cache (one-shot per version)."""
    sdk_url = get_latest_sentinel_sdk_url()
    version = sdk_url.rstrip("/").split("/")[-2] if "/sentinel/" in sdk_url else "latest"
    cache_dir = Path(tempfile.gettempdir()) / "openai-sentinel-demo" / version
    cache_dir.mkdir(parents=True, exist_ok=True)
    sdk_file = cache_dir / "sdk.js"
    if sdk_file.exists() and sdk_file.stat().st_size > 0:
        return sdk_file

    resp = session.get(
        sdk_url,
        headers={
            "accept": "*/*",
            "accept-language": accept_language or "en-US,en;q=0.5",
            "referer": "https://auth.openai.com/",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
        },
        timeout=max(10, int(timeout_ms / 1000)),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"下载 sdk.js 失败: HTTP {resp.status_code}")
    content = getattr(resp, "content", b"") or (resp.text or "").encode()
    if not content:
        raise RuntimeError("下载 sdk.js 失败: 响应为空")
    sdk_file.write_bytes(content)
    return sdk_file


_WRAPPER_JS = """
const fs = require('fs');
const timeoutMs = Number(process.env.OPENAI_SENTINEL_VM_TIMEOUT_MS || '10000');
const sdkFile = process.env.OPENAI_SENTINEL_SDK_FILE;
const scriptFile = process.env.OPENAI_SENTINEL_QUICKJS_SCRIPT;

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    const payload = JSON.parse(input || '{}');
    globalThis.__payload_json = JSON.stringify(payload);
    globalThis.__sdk_source = fs.readFileSync(sdkFile, 'utf8');
    globalThis.__vm_done = false;
    globalThis.__vm_output_json = '';
    globalThis.__vm_error = '';
    const script = fs.readFileSync(scriptFile, 'utf8');
    eval(script);

    const started = Date.now();
    while (!globalThis.__vm_done) {
      if ((Date.now() - started) > timeoutMs) {
        throw new Error('QuickJS script timeout');
      }
      await new Promise((resolve) => setTimeout(resolve, 1));
    }

    if (String(globalThis.__vm_error || '').trim()) {
      throw new Error(String(globalThis.__vm_error));
    }

    process.stdout.write(String(globalThis.__vm_output_json || ''));
  } catch (err) {
    const msg = err && err.stack ? String(err.stack) : String(err);
    process.stderr.write(msg);
    process.exit(1);
  }
});
""".strip()


def _run_quickjs_action(
    *,
    action: str,
    sdk_file: Path,
    quickjs_script: Path,
    payload: dict,
    timeout_ms: int,
) -> dict:
    body = dict(payload)
    body["action"] = action
    proc = subprocess.run(
        [_resolve_node_binary(), "-e", _WRAPPER_JS],
        input=json.dumps(body, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=max(10, int(timeout_ms / 1000) + 5),
        env={
            **os.environ,
            "OPENAI_SENTINEL_SDK_FILE": str(sdk_file),
            "OPENAI_SENTINEL_QUICKJS_SCRIPT": str(quickjs_script),
            "OPENAI_SENTINEL_VM_TIMEOUT_MS": str(min(timeout_ms, 30000)),
        },
    )
    if proc.returncode != 0:
        raise RuntimeError(f"QuickJS 执行失败: {(proc.stderr or proc.stdout or 'unknown').strip()[:300]}")
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("QuickJS 返回空输出")
    data = json.loads(out)
    if not isinstance(data, dict):
        raise RuntimeError("QuickJS 输出不是 JSON 对象")
    return data


def _fetch_sentinel_challenge(
    session: Any,
    *,
    device_id: str,
    flow: str,
    request_p: str,
    timeout_ms: int,
    accept_language: str,
    user_agent: str,
) -> dict:
    body = {"p": request_p, "id": device_id, "flow": flow}
    resp = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps(body, separators=(",", ":")),
        headers={
            "origin": "https://sentinel.openai.com",
            "referer": get_latest_sentinel_frame_url(),
            "content-type": "text/plain;charset=UTF-8",
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": accept_language or "en-US,en;q=0.5",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": user_agent or "Mozilla/5.0",
        },
        timeout=max(10, int(timeout_ms / 1000)),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"/sentinel/req HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Sentinel challenge 响应不是 JSON 对象")
    return payload


def get_sentinel_tokens_via_quickjs(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    user_agent: str = "",
    accept_language: str = "en-US,en;q=0.5",
    timeout_ms: int = 45000,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[dict[str, str]]:
    """Try the QuickJS path. Return token bundle on success, None on any failure.

    Caller is expected to fall back to pure-Python sentinel on None.
    """
    log = log or (lambda m: logger.info(m))
    quickjs_script = _quickjs_script_path()
    if not quickjs_script.exists():
        log(f"Sentinel QuickJS 脚本不存在: {quickjs_script}")
        return None

    did = str(device_id or uuid.uuid4())
    try:
        sdk_file = _ensure_sdk_file(session, timeout_ms, accept_language)
        sdk_url = get_latest_sentinel_sdk_url()
        language, languages = _accept_language_parts(accept_language)
        profile = _runtime_profile(user_agent, sdk_url)
        if flow == "oauth_create_account":
            profile["page_url"] = "https://auth.openai.com/about-you"
        runtime_payload = {
            "device_id": did,
            "user_agent": user_agent,
            "accept_language": accept_language,
            "language": language,
            "languages": languages,
            "sdk_url": sdk_url,
            "document_cookie": _session_cookie_header(session, did),
            "session_storage": {"oai-did": did},
            "local_storage": {"oai-did": did},
            **profile,
        }

        requirements = _run_quickjs_action(
            action="requirements",
            sdk_file=sdk_file,
            quickjs_script=quickjs_script,
            payload=runtime_payload,
            timeout_ms=timeout_ms,
        )
        request_p = str(requirements.get("request_p") or "").strip()
        if not request_p:
            log("Sentinel QuickJS 失败: requirements 未返回 request_p")
            return None

        challenge = _fetch_sentinel_challenge(
            session,
            device_id=did,
            flow=flow,
            request_p=request_p,
            timeout_ms=timeout_ms,
            accept_language=accept_language,
            user_agent=user_agent,
        )
        c_value = str(challenge.get("token") or "").strip()
        if not c_value:
            log("Sentinel QuickJS 失败: challenge token 为空")
            return None

        solved = _run_quickjs_action(
            action="solve",
            sdk_file=sdk_file,
            quickjs_script=quickjs_script,
            payload={
                **runtime_payload,
                "flow": flow,
                "request_p": request_p,
                "challenge": challenge,
            },
            timeout_ms=timeout_ms,
        )
        final_p = str(solved.get("final_p") or solved.get("p") or "").strip()
        if not final_p:
            log("Sentinel QuickJS 失败: solve 未返回 final_p")
            return None

        t_raw = solved.get("t")
        t_value = "" if t_raw is None else str(t_raw).strip()
        if not t_value:
            log("Sentinel QuickJS 失败: solve 未返回有效 t")
            return None

        token = json.dumps(
            {"p": final_p, "t": t_value, "c": c_value, "id": did, "flow": flow},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        so_token = str(solved.get("so_token") or "").strip()
        log(
            "Sentinel QuickJS 成功 "
            f"(p_len={len(final_p)} t_len={len(t_value)} c_len={len(c_value)} "
            f"so_len={len(so_token)})"
        )
        return {"token": token, "so_token": so_token}
    except Exception as e:
        log(f"Sentinel QuickJS 异常: {e}")
        return None


def get_sentinel_token_via_quickjs(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    user_agent: str = "",
    accept_language: str = "en-US,en;q=0.5",
    timeout_ms: int = 45000,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Backward-compatible wrapper returning only `openai-sentinel-token`."""
    bundle = get_sentinel_tokens_via_quickjs(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        accept_language=accept_language,
        timeout_ms=timeout_ms,
        log=log,
    )
    return str(bundle.get("token") or "") if bundle else None
