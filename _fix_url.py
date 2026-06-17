import sys
sys.stdout.reconfigure(encoding='utf-8')

path = 'platforms/chatgpt/browser_register.py'
content = open(path, 'r', encoding='utf-8').read()

# Update PHONE_FIRST_FORCED_ENTRY_URL to include screen_hint=signup
old = 'PHONE_FIRST_FORCED_ENTRY_URL = f"{OPENAI_AUTH}/log-in-or-create-account?usernameKind=phone_number"'
new = 'PHONE_FIRST_FORCED_ENTRY_URL = f"{OPENAI_AUTH}/create-account?usernameKind=phone_number"'

if old in content:
    content = content.replace(old, new, 1)
    open(path, 'w', encoding='utf-8', newline='').write(content)
    print("URL updated to /create-account")
else:
    print("NOT FOUND")

import py_compile
try:
    py_compile.compile(path, doraise=True)
    print("COMPILE: OK")
except:
    print("COMPILE: FAIL")
