import sys
sys.stdout.reconfigure(encoding='utf-8')

path = 'platforms/chatgpt/browser_register.py'
content = open(path, 'r', encoding='utf-8').read()

# The issue is in _fill_input_like_user - the final_value comparison is too strict.
# The phone number may get reformatted (spaces, dashes added).
# Fix: compare without non-digit chars for phone-like values

old_check = '''        final_value = str(locator.input_value() or "").strip()
        if final_value == str(value):
            return True'''

new_check = '''        final_value = str(locator.input_value() or "").strip()
        if final_value == str(value):
            return True
        # For phone numbers: compare digits only (page may reformat with spaces/dashes)
        import re as _re
        if _re.sub(r"[^0-9+]", "", final_value) == _re.sub(r"[^0-9+]", "", str(value)):
            return True'''

if old_check in content:
    content = content.replace(old_check, new_check, 1)
    open(path, 'w', encoding='utf-8', newline='').write(content)
    print("Fill check patched for phone number formatting")
else:
    print("NOT FOUND")

import py_compile
try:
    py_compile.compile(path, doraise=True)
    print("COMPILE: OK")
except:
    print("COMPILE: FAIL")
