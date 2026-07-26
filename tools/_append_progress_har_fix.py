#!/usr/bin/env python3
from pathlib import Path

p = Path("progress.md")
text = p.read_text(encoding="utf-8") if p.exists() else ""
marker = "协议注册对齐有头 HAR 头信息/Sentinel SDK"
if marker in text:
    print("progress already has entry")
    raise SystemExit(0)

entry = """

## 2026-07-25 - Task: 协议注册对齐有头 HAR 头信息/Sentinel SDK

### 目标
- 对照 `tools/captures/register-20260725-010100-ArlanBrando080676_hotmail.com.har` 修正协议注册请求头与 Sentinel SDK 参数，降低协议号秒死/风控概率。

### 关键差异（HAR vs 旧协议）
1. User-Agent：有头为 Macintosh Firefox/135，协议旧为 Windows Firefox/135。
2. oai-client-version/build：有头 prod-2c08737... / 8578659，协议旧值过期。
3. Sentinel p 载荷：
   - /sentinel/req 使用 versioned .../sentinel/20260219f9f6/sdk.js + mozGetUserMedia，前缀 gAAAAAC...~S
   - create_account final p 使用 .../backend-api/sentinel/sdk.js + plugins-[object PluginArray]，前缀 gAAAAAB...~S
   - 旧实现把两种 SDK URL 写反，且 capability probe 不像真实 Firefox。
4. chat-requirements/prepare 的 p 使用 CF jsd script + client version。
5. auth.openai.com JSON（email-otp/create_account）不发送 oai-device-id；设备身份走 oai-did cookie + sentinel id。

### 改动
- platforms/chatgpt/register.py：UA/client version、_SentinelTokenGenerator 三阶段 token、final p、auth JSON headers。
- tests/test_chatgpt_protocol_otp.py：同步断言与 HAR shape 回归。

### 验证
- python -m py_compile platforms/chatgpt/register.py
- python tools/_verify_sentinel_shapes.py ALL PASSED
- pytest tests/test_chatgpt_protocol_otp.py -> 53 passed

### 回滚点
- 恢复 platforms/chatgpt/register.py 与 tests/test_chatgpt_protocol_otp.py 本轮 diff 即可。
"""
p.write_text(text.rstrip() + entry, encoding="utf-8")
print("progress appended")
