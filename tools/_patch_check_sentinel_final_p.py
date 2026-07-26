#!/usr/bin/env python3
from pathlib import Path

path = Path("platforms/chatgpt/register.py")
text = path.read_text(encoding="utf-8")
old = '''                if pow_meta.get("required") and pow_meta.get("seed"):

                    sent_p = generator.generate_token(

                        str(pow_meta.get("seed") or ""),

                        str(pow_meta.get("difficulty") or "0"),

                    )

                    self._log(f"Sentinel PoW solved: flow={flow}")
'''
new = '''                # Headed HAR create_account always uses final enforcement p
                # (gAAAAAB + backend-api/sentinel/sdk.js), not the initial requirements p.
                if pow_meta.get("required") and pow_meta.get("seed"):
                    sent_p = generator.generate_token(
                        str(pow_meta.get("seed") or ""),
                        str(pow_meta.get("difficulty") or "0"),
                    )
                    self._log(f"Sentinel PoW solved: flow={flow}")
                else:
                    sent_p = generator.generate_token(
                        str(pow_meta.get("seed") or ""),
                        str(pow_meta.get("difficulty") or "0"),
                    )
'''
if old not in text:
    # try compact form after our rewrite
    old2 = '''                if pow_meta.get("required") and pow_meta.get("seed"):
                    sent_p = generator.generate_token(
                        str(pow_meta.get("seed") or ""),
                        str(pow_meta.get("difficulty") or "0"),
                    )
                    self._log(f"Sentinel PoW solved: flow={flow}")
'''
    if old2 not in text:
        raise SystemExit("pow block not found")
    text = text.replace(old2, new, 1)
else:
    text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
print("patched final p")
