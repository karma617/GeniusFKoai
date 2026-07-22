from pathlib import Path
p = Path("scripts/pp_complete_extract.py")
t = p.read_text(encoding="utf-8")
if "chatgpt_account_id" in t and "Chatgpt-Account-Id" in t:
    print("already has account header")
else:
    old = '''def auth_headers(extra=None):
    h = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "oai-language": "zh-CN",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    if COOKIE:
        h["cookie"] = COOKIE
    if extra:
        h.update(extra)
    return h
'''
    new = '''def _jwt_account_id(token: str) -> str:
    try:
        import base64, json as _json
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(part.encode()))
        auth = claims.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or "")
    except Exception:
        return ""


ACCOUNT_ID = _jwt_account_id(TOKEN)


def auth_headers(extra=None):
    h = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "oai-language": "zh-CN",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    if ACCOUNT_ID:
        h["Chatgpt-Account-Id"] = ACCOUNT_ID
    if COOKIE:
        h["cookie"] = COOKIE
    if extra:
        h.update(extra)
    return h
'''
    if old not in t:
        raise SystemExit("auth_headers block not found")
    t = t.replace(old, new, 1)
    # docstring note for BR+JP
    if "BR+JP note" not in t:
        t = t.replace(
            'Confirm mode:\n  BA_CONFIRM_MODE = pm|direct\n    pm     : create payment_method then confirm (hammer path)\n    direct : stripe_confirm_paypal_direct (payment_protocol path)\n"""',
            'Confirm mode:\n  BA_CONFIRM_MODE = pm|direct\n    pm     : create payment_method then confirm (hammer path)\n    direct : stripe_confirm_paypal_direct (payment_protocol path)\n\nBR+JP note:\n  Create via BR/JP currently returns amt=0 + payment_method_types=[card,link]\n  (no paypal) on trial-eligible geo; BA requires paypal at create time.\n  Recovered BA path on this stack: US create (+ optional BR/JP promo tax +\n  BA_SKIP_MAIN_TAX=1 + BA_CONFIRM_MODE=direct).\n"""',
            1,
        )
    p.write_text(t, encoding="utf-8")
    print("patched account header + brjp note")
import ast
ast.parse(p.read_text(encoding="utf-8"))
print("syntax ok")