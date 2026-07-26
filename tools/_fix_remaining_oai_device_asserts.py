#!/usr/bin/env python3
from pathlib import Path

path = Path("tests/test_chatgpt_protocol_otp.py")
text = path.read_text(encoding="utf-8")
# Replace remaining oai-device-id asserts that target latest_chatgpt_json_headers / create_account auth headers.
# Keep chatgpt client header asserts that still expect oai-device-id.
lines = text.splitlines(keepends=True)
out = []
i = 0
changed = 0
while i < len(lines):
    line = lines[i]
    if 'assert headers["oai-device-id"] == "device-id"' in line:
        # look back for context
        ctx = "".join(out[-40:])
        if (
            "json_headers" in ctx
            or "create_account" in ctx
            or "about-you" in ctx
            or "_latest_chatgpt_json_headers" in ctx
            or "auth.openai.com" in ctx
        ):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f'{indent}assert "oai-device-id" not in headers  # headed auth.openai.com omits this header\n')
            changed += 1
            i += 1
            continue
    out.append(line)
    i += 1
path.write_text("".join(out), encoding="utf-8")
print("changed", changed)

# show remaining oai-device-id asserts
text = path.read_text(encoding="utf-8")
for n, line in enumerate(text.splitlines(), 1):
    if "oai-device-id" in line:
        print(f"L{n}: {line.strip()}")
