#!/usr/bin/env python3
from pathlib import Path
import re

for path in [
    Path("platforms/chatgpt/constants.py"),
    Path("platforms/chatgpt/sentinel_vm.py"),
    Path("platforms/chatgpt/authflow_experimental/sentinel.py"),
    Path("platforms/chatgpt/authflow_experimental/sentinel_quickjs.py"),
    Path("tools/_register_snip.py"),
]:
    print("=" * 40, path)
    if not path.exists():
        print("missing")
        continue
    text = path.read_text(encoding="utf-8")
    for m in re.finditer(r".{0,80}(SENTINEL|sdk\.js|frame\.html|20260219|snapshot|requirements|generate_|SDK_URL|FRAME_URL|backend-api/sentinel).{0,120}", text, re.I):
        line_no = text[: m.start()].count("\n") + 1
        print(f"L{line_no}: {m.group(0).replace(chr(10),' ')[:220]}")
