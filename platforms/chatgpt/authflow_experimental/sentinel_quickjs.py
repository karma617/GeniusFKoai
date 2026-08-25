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

import base64
import json
import logging
import os
import queue
import random
import subprocess
import threading
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from platforms.chatgpt.constants import (
    SENTINEL_ENTRY_SDK_URL,
    get_latest_sentinel_frame_url,
    get_latest_sentinel_sdk_url,
)

logger = logging.getLogger(__name__)


SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"

_RETRYABLE_NETWORK_ERROR_MARKERS = (
    "tls connect error",
    "invalid library",
    "curl: (28)",
    "curl: (35)",
    "curl: (56)",
    "connection reset",
    "connection aborted",
    "connection refused",
    "failed to perform",
    "network is unreachable",
    "operation timed out",
    "read timed out",
)


def _is_retryable_network_error(error: object) -> bool:
    message = str(error or "").strip().lower()
    return any(marker in message for marker in _RETRYABLE_NETWORK_ERROR_MARKERS)


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
    """Return cookies visible to the Sentinel iframe, not the flattened session jar."""
    host = "sentinel.openai.com"
    request_path = "/backend-api/sentinel/frame.html"
    parts: list[str] = []
    cookies = getattr(session, "cookies", None)
    jar = getattr(cookies, "jar", None)
    try:
        for cookie in jar or []:
            domain = str(getattr(cookie, "domain", "") or "").lstrip(".").lower()
            if domain and host != domain and not host.endswith(f".{domain}"):
                continue
            cookie_path = str(getattr(cookie, "path", "/") or "/")
            if not request_path.startswith(cookie_path.rstrip("/") or "/"):
                continue
            rest = dict(getattr(cookie, "_rest", {}) or {})
            http_only = str(rest.get("http_only") or rest.get("HttpOnly") or "").strip().lower()
            if http_only in {"1", "true", "yes"}:
                continue
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "")
            if name:
                parts.append(f"{name}={value}")
    except Exception:
        parts = []
    if not any(part.startswith("oai-did=") for part in parts):
        parts.append(f"oai-did={device_id}")
    return "; ".join(parts)

def _runtime_profile(
    user_agent: str,
    sdk_url: str,
    *,
    frame_url: str = "",
    timezone_name: str = "Asia/Tokyo",
    timezone_offset_min: int = -540,
    runtime_state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ua = str(user_agent or "")
    is_mac = "Macintosh" in ua or "Mac OS X" in ua
    now_ms = int(time.time() * 1000)
    state = runtime_state if isinstance(runtime_state, dict) else {}
    if not state.get("runtime_id"):
        state["runtime_id"] = str(uuid.uuid4())
    if not state.get("time_origin"):
        state["time_origin"] = now_ms - random.randint(8000, 40000)
    if not state.get("performance_base"):
        state["performance_base"] = random.randint(3000, 9000)
    if not state.get("started_monotonic"):
        state["started_monotonic"] = time.monotonic()
    elapsed_ms = max(0, int((time.monotonic() - float(state["started_monotonic"])) * 1000))
    performance_now = int(state["performance_base"]) + elapsed_ms
    return {
        "frame_url": str(frame_url or get_latest_sentinel_frame_url()),
        "page_url": "https://auth.openai.com/email-verification",
        "platform": "MacIntel" if is_mac else "Win32",
        "vendor": "" if "Firefox/" in ua else "Google Inc.",
        "hardware_concurrency": 8,
        "screen_width": 1728 if is_mac else 1920,
        "screen_height": 1117 if is_mac else 1080,
        "screen_avail_width": 1728 if is_mac else 1920,
        "screen_avail_height": 1092 if is_mac else 1040,
        "viewport_width": 1800,
        "viewport_height": 839,
        "outer_width": 1800,
        "outer_height": 900,
        "color_depth": 30 if is_mac else 24,
        "pixel_depth": 30 if is_mac else 24,
        "performance_now": performance_now,
        "time_origin": int(state["time_origin"]),
        "timezone": str(timezone_name or "Asia/Tokyo"),
        "timezone_offset_min": int(timezone_offset_min),
        "timezone_display_name": "日本標準時" if str(timezone_name) == "Asia/Tokyo" else "",
        "runtime_id": str(state["runtime_id"]),
    }


def _ensure_sdk_file(
    session: Any,
    timeout_ms: int,
    accept_language: str,
    user_agent: str,
) -> tuple[Path, str, str]:
    """Resolve and download the SDK through the current task session/proxy."""
    sdk_url = get_latest_sentinel_sdk_url(
        session=session,
        accept_language=accept_language,
        user_agent=user_agent,
        timeout=max(10, int(timeout_ms / 1000)),
    )
    resolver_source = str(getattr(session, "_openai_sentinel_sdk_source", "") or "unknown")
    version = sdk_url.rstrip("/").split("/")[-2] if "/sentinel/" in sdk_url else "latest"
    cache_dir = Path(tempfile.gettempdir()) / "openai-sentinel-demo" / version
    cache_dir.mkdir(parents=True, exist_ok=True)
    sdk_file = cache_dir / "sdk.js"
    if sdk_file.exists() and sdk_file.stat().st_size > 0:
        return sdk_file, sdk_url, f"disk_cache:{resolver_source}"

    resp = session.get(
        sdk_url,
        headers={
            "accept": "*/*",
            "accept-language": accept_language or "en-US,en;q=0.5",
            "referer": "https://auth.openai.com/",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
            "user-agent": user_agent or "Mozilla/5.0",
        },
        timeout=max(10, int(timeout_ms / 1000)),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"下载 sdk.js 失败: HTTP {resp.status_code}")
    content = getattr(resp, "content", b"") or (resp.text or "").encode()
    if not content:
        raise RuntimeError("下载 sdk.js 失败: 响应为空")
    temp_file = cache_dir / f"sdk-{uuid.uuid4().hex}.tmp"
    temp_file.write_bytes(content)
    temp_file.replace(sdk_file)
    return sdk_file, sdk_url, f"download:{resolver_source}"


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


_WORKER_WRAPPER_JS = r"""
const fs = require("fs");
const readline = require("readline");
const output = process.stdout;
const exitProcess = process.exit.bind(process);
const timeoutMs = Number(process.env.OPENAI_SENTINEL_VM_TIMEOUT_MS || "10000");
const sdkFile = process.env.OPENAI_SENTINEL_SDK_FILE;
const scriptFile = process.env.OPENAI_SENTINEL_QUICKJS_SCRIPT;
globalThis.__sentinel_worker_mode = true;
globalThis.__sdk_source = fs.readFileSync(sdkFile, "utf8");
eval(fs.readFileSync(scriptFile, "utf8"));

let chain = Promise.resolve();
let idleTimer = null;
const resetIdleTimer = () => {
  if (idleTimer) clearTimeout(idleTimer);
  idleTimer = setTimeout(() => exitProcess(0), 120000);
};
resetIdleTimer();
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  resetIdleTimer();
  chain = chain.then(async () => {
    let request = {};
    try {
      request = JSON.parse(String(line || "{}"));
      const result = await Promise.race([
        globalThis.__sentinel_worker_execute(request.payload || {}),
        new Promise((_, reject) => setTimeout(() => reject(new Error("Sentinel worker timeout")), timeoutMs)),
      ]);
      output.write(JSON.stringify({ id: request.id, ok: true, result }) + "\n");
    } catch (error) {
      output.write(JSON.stringify({
        id: request.id,
        ok: false,
        error: error && error.stack ? String(error.stack) : String(error),
      }) + "\n");
    }
  });
});
""".strip()


class _SentinelNodeWorker:
    def __init__(
        self,
        *,
        sdk_file: Path,
        quickjs_script: Path,
        timezone_name: str,
        timeout_ms: int,
    ) -> None:
        self.sdk_file = Path(sdk_file).resolve()
        self.quickjs_script = Path(quickjs_script).resolve()
        self.timezone_name = str(timezone_name or "Asia/Tokyo")
        self.timeout_ms = int(timeout_ms)
        self._lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._sequence = 0
        self._closed = False
        self._process = subprocess.Popen(
            [_resolve_node_binary(), "-e", _WORKER_WRAPPER_JS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={
                **os.environ,
                "OPENAI_SENTINEL_SDK_FILE": str(self.sdk_file),
                "OPENAI_SENTINEL_QUICKJS_SCRIPT": str(self.quickjs_script),
                "OPENAI_SENTINEL_VM_TIMEOUT_MS": str(min(self.timeout_ms, 30000)),
                "TZ": self.timezone_name,
            },
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    @property
    def is_alive(self) -> bool:
        return not self._closed and self._process.poll() is None

    def matches(self, sdk_file: Path, timezone_name: str) -> bool:
        return (
            self.is_alive
            and self.sdk_file == Path(sdk_file).resolve()
            and self.timezone_name == str(timezone_name or "Asia/Tokyo")
        )

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        if stream is None:
            return
        for line in stream:
            text = str(line or "").strip()
            if not text:
                continue
            try:
                self._responses.put(json.loads(text))
            except Exception:
                self._responses.put({"ok": False, "error": f"Sentinel worker invalid output: {text[:300]}"})

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        for line in stream:
            text = str(line or "").strip()
            if text:
                self._stderr_lines.append(text)
                del self._stderr_lines[:-20]

    def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self.is_alive:
                detail = " | ".join(self._stderr_lines[-3:])
                raise RuntimeError(f"Sentinel worker 已退出: {detail or self._process.returncode}")
            self._sequence += 1
            request_id = self._sequence
            body = dict(payload)
            body["action"] = action
            stream = self._process.stdin
            if stream is None:
                raise RuntimeError("Sentinel worker stdin 不可用")
            stream.write(json.dumps({"id": request_id, "payload": body}, ensure_ascii=False) + "\n")
            stream.flush()
            try:
                response = self._responses.get(timeout=max(10, int(self.timeout_ms / 1000) + 5))
            except queue.Empty as exc:
                self.close()
                raise RuntimeError("Sentinel worker 等待响应超时") from exc
            if int(response.get("id") or request_id) != request_id:
                raise RuntimeError("Sentinel worker 响应序号不一致")
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error") or "Sentinel worker 执行失败")[:600])
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Sentinel worker 输出不是 JSON 对象")
            return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin:
                self._process.stdin.close()
        except Exception:
            pass
        try:
            self._process.terminate()
            self._process.wait(timeout=3)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass


def close_sentinel_runtime(runtime_state: Optional[dict[str, Any]]) -> None:
    if not isinstance(runtime_state, dict):
        return
    worker = runtime_state.pop("_worker", None)
    runtime_state.pop("_worker_key", None)
    if isinstance(worker, _SentinelNodeWorker):
        worker.close()


def _runtime_worker(
    runtime_state: dict[str, Any],
    *,
    sdk_file: Path,
    quickjs_script: Path,
    timezone_name: str,
    timeout_ms: int,
) -> _SentinelNodeWorker:
    worker = runtime_state.get("_worker")
    if isinstance(worker, _SentinelNodeWorker) and worker.matches(sdk_file, timezone_name):
        return worker
    close_sentinel_runtime(runtime_state)
    worker = _SentinelNodeWorker(
        sdk_file=sdk_file,
        quickjs_script=quickjs_script,
        timezone_name=timezone_name,
        timeout_ms=timeout_ms,
    )
    runtime_state["_worker"] = worker
    runtime_state["_worker_key"] = f"{Path(sdk_file).resolve()}|{timezone_name}"
    return worker

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
            "TZ": str(payload.get("timezone") or "Asia/Tokyo"),
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
    frame_url: str,
) -> dict:
    body = {"p": request_p, "id": device_id, "flow": flow}
    resp = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps(body, separators=(",", ":")),
        headers={
            "origin": "https://sentinel.openai.com",
            "referer": frame_url,
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


def _sentinel_p_fields(value: str) -> dict[str, Any]:
    raw = str(value or "").strip()
    if raw.endswith("~S"):
        raw = raw[:-2]
    for prefix in ("gAAAAAC", "gAAAAAB", "gAAAAA"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    marker = raw.find("Wz")
    if marker >= 0:
        raw = raw[marker:]
    try:
        decoded = base64.b64decode(raw + "=" * ((-len(raw)) % 4))
        values = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(values, list):
        return {}
    return {
        "screen": values[0] if len(values) > 0 else None,
        "date": values[1] if len(values) > 1 else None,
        "memory": values[2] if len(values) > 2 else None,
        "script": values[5] if len(values) > 5 else None,
        "collector_id": values[14] if len(values) > 14 else None,
        "time_origin": values[17] if len(values) > 17 else None,
    }


def get_sentinel_requirements_via_quickjs(
    session: Any,
    device_id: str,
    *,
    user_agent: str,
    accept_language: str,
    client_version: str = "",
    script_url: str,
    page_url: str,
    timezone_name: str = "Asia/Tokyo",
    timezone_offset_min: int = -540,
    runtime_state: Optional[dict[str, Any]] = None,
    timeout_ms: int = 30000,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[dict[str, Any]]:
    """Generate a chat-requirements prepare p with the current Sentinel SDK runtime."""
    worker_state = runtime_state if isinstance(runtime_state, dict) else {}
    emit = log or (lambda message: logger.info(message))
    try:
        sdk_file, sdk_url, sdk_source = _ensure_sdk_file(
            session,
            timeout_ms,
            accept_language,
            user_agent,
        )
        frame_url = get_latest_sentinel_frame_url(
            session=session,
            accept_language=accept_language,
            user_agent=user_agent,
            timeout=max(10, int(timeout_ms / 1000)),
        )
        language, languages = _accept_language_parts(accept_language)
        profile = _runtime_profile(
            user_agent,
            script_url,
            frame_url=frame_url,
            timezone_name=timezone_name,
            timezone_offset_min=timezone_offset_min,
            runtime_state=worker_state,
        )
        runtime_payload = {
            "device_id": str(device_id),
            "user_agent": user_agent,
            "accept_language": accept_language,
            "language": language,
            "languages": languages,
            "client_version": client_version,
            "sdk_url": script_url,
            "document_cookie": _session_cookie_header(session, device_id),
            "session_storage": {},
            "local_storage": {},
            **profile,
            "page_url": page_url,
        }
        worker = _runtime_worker(
            worker_state,
            sdk_file=sdk_file,
            quickjs_script=_quickjs_script_path(),
            timezone_name=timezone_name,
            timeout_ms=timeout_ms,
        )
        result = worker.call("requirements", runtime_payload)
        request_p = str(result.get("request_p") or "").strip()
        if not request_p:
            raise RuntimeError("requirements 未返回 request_p")
        fields = _sentinel_p_fields(request_p)
        collector_id = str(fields.get("collector_id") or "")
        previous_collector = str(worker_state.get("collector_id") or "")
        if previous_collector and collector_id and previous_collector != collector_id:
            raise RuntimeError(
                "SDK cross-flow continuity mismatch: "
                f"previous={previous_collector} current={collector_id}"
            )
        if collector_id:
            worker_state["collector_id"] = collector_id
        emit(
            "Sentinel QuickJS requirements 成功 "
            f"(p_len={len(request_p)} collector={collector_id or '-'} "
            f"screen={fields.get('screen')} memory={fields.get('memory')} "
            f"script={fields.get('script') or '-'})"
        )
        return {
            "request_p": request_p,
            "sdk_url": sdk_url,
            "sdk_source": sdk_source,
            "runtime_id": str(profile.get("runtime_id") or ""),
            "time_origin": str(profile.get("time_origin") or ""),
            "collector_id": collector_id,
        }
    except Exception as exc:
        emit(f"Sentinel QuickJS requirements 异常: {exc}")
        return None


def solve_sentinel_challenge_via_quickjs(
    session: Any,
    device_id: str,
    *,
    request_p: str,
    challenge: dict[str, Any],
    flow: str,
    user_agent: str,
    accept_language: str,
    client_version: str = "",
    script_url: str,
    page_url: str,
    timezone_name: str = "Asia/Tokyo",
    timezone_offset_min: int = -540,
    runtime_state: Optional[dict[str, Any]] = None,
    timeout_ms: int = 45000,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[dict[str, str]]:
    """Solve a chat-requirements challenge in the worker that produced request_p."""
    emit = log or (lambda message: logger.info(message))
    worker_state = runtime_state if isinstance(runtime_state, dict) else {}
    request_value = str(request_p or "").strip()
    if not request_value:
        emit("Sentinel QuickJS solve 缺少 request_p")
        return None
    if not isinstance(challenge, dict):
        emit("Sentinel QuickJS solve challenge 不是对象")
        return None
    try:
        sdk_file, sdk_url, sdk_source = _ensure_sdk_file(
            session,
            timeout_ms,
            accept_language,
            user_agent,
        )
        frame_url = get_latest_sentinel_frame_url(
            session=session,
            accept_language=accept_language,
            user_agent=user_agent,
            timeout=max(10, int(timeout_ms / 1000)),
        )
        language, languages = _accept_language_parts(accept_language)
        profile = _runtime_profile(
            user_agent,
            script_url,
            frame_url=frame_url,
            timezone_name=timezone_name,
            timezone_offset_min=timezone_offset_min,
            runtime_state=worker_state,
        )
        runtime_payload = {
            "device_id": str(device_id),
            "user_agent": user_agent,
            "accept_language": accept_language,
            "language": language,
            "languages": languages,
            "client_version": client_version,
            "sdk_url": script_url,
            "document_cookie": _session_cookie_header(session, device_id),
            "session_storage": {},
            "local_storage": {},
            **profile,
            "page_url": page_url,
        }
        worker = _runtime_worker(
            worker_state,
            sdk_file=sdk_file,
            quickjs_script=_quickjs_script_path(),
            timezone_name=timezone_name,
            timeout_ms=timeout_ms,
        )
        solved = worker.call(
            "solve",
            {
                **runtime_payload,
                "flow": flow,
                "request_p": request_value,
                "challenge": challenge,
            },
        )
        final_p = str(solved.get("final_p") or solved.get("p") or "").strip()
        t_value = str(solved.get("t") or "").strip()
        if not final_p or not t_value:
            raise RuntimeError(
                f"solve 返回不完整 final_p={bool(final_p)} t={bool(t_value)}"
            )
        request_fields = _sentinel_p_fields(request_value)
        final_fields = _sentinel_p_fields(final_p)
        request_collector = str(request_fields.get("collector_id") or "")
        final_collector = str(final_fields.get("collector_id") or "")
        if request_collector and final_collector and request_collector != final_collector:
            raise RuntimeError(
                "SDK runtime continuity mismatch: "
                f"requirements={request_collector} solve={final_collector}"
            )
        request_origin = request_fields.get("time_origin")
        final_origin = final_fields.get("time_origin")
        if request_origin and final_origin and request_origin != final_origin:
            raise RuntimeError(
                f"SDK timeOrigin mismatch: requirements={request_origin} solve={final_origin}"
            )
        collector_id = request_collector or final_collector
        previous_collector = str(worker_state.get("collector_id") or "")
        if previous_collector and collector_id and previous_collector != collector_id:
            raise RuntimeError(
                "SDK cross-flow continuity mismatch: "
                f"previous={previous_collector} current={collector_id}"
            )
        if collector_id:
            worker_state["collector_id"] = collector_id
        emit(
            "Sentinel QuickJS solve 成功 "
            f"(p_len={len(final_p)} t_len={len(t_value)} "
            f"collector={collector_id or '-'} screen={final_fields.get('screen')} "
            f"script={final_fields.get('script') or '-'} sdk={sdk_url} "
            f"sdk_source={sdk_source})"
        )
        return {
            "final_p": final_p,
            "t": t_value,
            "so_token": str(solved.get("so_token") or "").strip(),
            "sdk_url": sdk_url,
            "sdk_source": sdk_source,
            "runtime_id": str(profile.get("runtime_id") or ""),
            "time_origin": str(profile.get("time_origin") or ""),
            "collector_id": collector_id,
        }
    except Exception as exc:
        emit(f"Sentinel QuickJS solve 异常: {exc}")
        return None

def get_sentinel_tokens_via_quickjs(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    user_agent: str = "",
    accept_language: str = "en-US,en;q=0.5",
    client_version: str = "",
    timezone_name: str = "Asia/Tokyo",
    timezone_offset_min: int = -540,
    runtime_state: Optional[dict[str, Any]] = None,
    timeout_ms: int = 45000,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[dict[str, str]]:
    """Generate one Sentinel token while preserving the SDK instance across flows."""
    log = log or (lambda m: logger.info(m))
    quickjs_script = _quickjs_script_path()
    if not quickjs_script.exists():
        log(f"Sentinel QuickJS 脚本不存在: {quickjs_script}")
        return None

    did = str(device_id or uuid.uuid4())
    persistent_state = isinstance(runtime_state, dict)
    worker_state = runtime_state if persistent_state else {}
    try:
        sdk_file, sdk_url, sdk_source = _ensure_sdk_file(
            session, timeout_ms, accept_language, user_agent
        )
        frame_url = get_latest_sentinel_frame_url(
            session=session,
            accept_language=accept_language,
            user_agent=user_agent,
            timeout=max(10, int(timeout_ms / 1000)),
        )
        language, languages = _accept_language_parts(accept_language)
        profile = _runtime_profile(
            user_agent,
            sdk_url,
            frame_url=frame_url,
            timezone_name=timezone_name,
            timezone_offset_min=timezone_offset_min,
            runtime_state=worker_state,
        )
        if flow == "oauth_create_account":
            profile["page_url"] = "https://auth.openai.com/about-you"
        runtime_payload = {
            "device_id": did,
            "user_agent": user_agent,
            "accept_language": accept_language,
            "language": language,
            "languages": languages,
            "client_version": client_version,
            "sdk_url": sdk_url,
            "document_cookie": _session_cookie_header(session, did),
            "session_storage": {},
            "local_storage": {},
            **profile,
            "page_url": str(profile.get("page_url") or "https://auth.openai.com/email-verification"),
        }
        worker = _runtime_worker(
            worker_state,
            sdk_file=sdk_file,
            quickjs_script=quickjs_script,
            timezone_name=timezone_name,
            timeout_ms=timeout_ms,
        )
        requirements = worker.call("requirements", runtime_payload)
        request_p = str(requirements.get("request_p") or "").strip()
        if not request_p:
            raise RuntimeError("requirements 未返回 request_p")

        challenge = _fetch_sentinel_challenge(
            session,
            device_id=did,
            flow=flow,
            request_p=request_p,
            timeout_ms=timeout_ms,
            accept_language=accept_language,
            user_agent=user_agent,
            frame_url=frame_url,
        )
        c_value = str(challenge.get("token") or "").strip()
        if not c_value:
            raise RuntimeError("challenge token 为空")

        solve_payload = {
            **runtime_payload,
            "flow": flow,
            "request_p": request_p,
            "challenge": challenge,
            "sdk_url": SENTINEL_ENTRY_SDK_URL,
        }
        solved = worker.call("solve", solve_payload)
        final_p = str(solved.get("final_p") or solved.get("p") or "").strip()
        if not final_p:
            raise RuntimeError("solve 未返回 final_p")
        t_raw = solved.get("t")
        t_value = "" if t_raw is None else str(t_raw).strip()
        if not t_value:
            raise RuntimeError("solve 未返回有效 t")

        request_fields = _sentinel_p_fields(request_p)
        final_fields = _sentinel_p_fields(final_p)
        request_collector = str(request_fields.get("collector_id") or "")
        final_collector = str(final_fields.get("collector_id") or "")
        if request_collector and final_collector and request_collector != final_collector:
            raise RuntimeError(
                "SDK runtime continuity mismatch: "
                f"requirements={request_collector} solve={final_collector}"
            )
        request_origin = request_fields.get("time_origin")
        final_origin = final_fields.get("time_origin")
        if request_origin and final_origin and request_origin != final_origin:
            raise RuntimeError(
                f"SDK timeOrigin mismatch: requirements={request_origin} solve={final_origin}"
            )

        token = json.dumps(
            {"p": final_p, "t": t_value, "c": c_value, "id": did, "flow": flow},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        so_token = str(solved.get("so_token") or "").strip()
        collector_id = request_collector or final_collector
        previous_collector = str(worker_state.get("collector_id") or "")
        if previous_collector and collector_id and previous_collector != collector_id:
            raise RuntimeError(
                "SDK cross-flow continuity mismatch: "
                f"previous={previous_collector} current={collector_id}"
            )
        if collector_id:
            worker_state["collector_id"] = collector_id
        log(
            "Sentinel QuickJS 成功 "
            f"(p_len={len(final_p)} t_len={len(t_value)} c_len={len(c_value)} "
            f"so_len={len(so_token)} collector={collector_id or '-'} "
            f"screen={final_fields.get('screen')} memory={final_fields.get('memory')})"
        )
        return {
            "token": token,
            "so_token": so_token,
            "sdk_url": sdk_url,
            "sdk_source": sdk_source,
            "runtime_id": str(profile.get("runtime_id") or ""),
            "time_origin": str(profile.get("time_origin") or ""),
            "collector_id": collector_id,
        }
    except Exception as exc:
        log(f"Sentinel QuickJS 异常: {exc}")
        if _is_retryable_network_error(exc):
            raise
        return None
    finally:
        if not persistent_state:
            close_sentinel_runtime(worker_state)


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
