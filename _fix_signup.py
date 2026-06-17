import sys
sys.stdout.reconfigure(encoding='utf-8')

path = 'platforms/chatgpt/browser_register.py'
content = open(path, 'r', encoding='utf-8').read()

# Add a click on "Sign up" if visible before attempting phone input
# This handles the case where the forced-entry URL shows a login page
old_submit = '''def _submit_phone_identity_via_page(page, phone_number: str, log) -> dict:
    input_selector = _find_visible_phone_input_selector(page)
    use_generic_input = False
    if not input_selector:
        _click_phone_entry_if_available(page, log)
        input_selector = _find_visible_phone_input_selector(page)'''

new_submit = '''def _submit_phone_identity_via_page(page, phone_number: str, log) -> dict:
    # If we land on a login page ("Welcome back"), click "Sign up" first
    _click_signup_link_if_on_login(page, log)

    input_selector = _find_visible_phone_input_selector(page)
    use_generic_input = False
    if not input_selector:
        _click_phone_entry_if_available(page, log)
        input_selector = _find_visible_phone_input_selector(page)'''

if old_submit in content:
    content = content.replace(old_submit, new_submit, 1)
    print("Added _click_signup_link_if_on_login call")
else:
    print("NOT FOUND - submit")

# Now add the helper function before _submit_phone_identity_via_page
old_func_start = 'def _submit_phone_identity_via_page(page, phone_number: str, log) -> dict:'
insert_before = '''def _click_signup_link_if_on_login(page, log) -> None:
    """If the page shows login state, click Sign up link to switch to create-account."""
    try:
        signup_selectors = [
            'a:has-text("Sign up")',
            'a:has-text("sign up")',
            'a:has-text("Create account")',
            'a:has-text("create account")',
        ]
        for sel in signup_selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1000):
                    loc.click(timeout=2000)
                    log(f"Phone-first signup: clicked signup link: {sel}")
                    time.sleep(1.5)
                    return
            except Exception:
                continue
    except Exception:
        pass


'''

# Find the function definition and insert before it
idx = content.find(old_func_start)
if idx > 0:
    content = content[:idx] + insert_before + content[idx:]
    print("Added _click_signup_link_if_on_login function")
else:
    print("NOT FOUND - function insertion point")

open(path, 'w', encoding='utf-8', newline='').write(content)

import py_compile
try:
    py_compile.compile(path, doraise=True)
    print("COMPILE: OK")
except Exception as e:
    print(f"COMPILE: FAIL - {str(e)[:200]}")
