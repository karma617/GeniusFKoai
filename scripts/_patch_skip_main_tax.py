from pathlib import Path
p = Path('scripts/pp_complete_extract.py')
t = p.read_text(encoding='utf-8')

old = '''PROMO_TAX_COUNTRY = os.environ.get("BA_PROMO_TAX_COUNTRY", "").upper()
UI_MODE = os.environ.get("BA_UI_MODE", "custom").lower()
CONFIRM_MODE = os.environ.get("BA_CONFIRM_MODE", "pm").lower()  # pm|direct
MAX_ATTEMPTS = int(os.environ.get("BA_MAX_ATTEMPTS", "4"))
STOP_ON_FIRST = os.environ.get("BA_STOP_ON_FIRST", "1") not in {"0", "false", "False"}
DO_FULL = os.environ.get("BA_DO_FULL", "1") not in {"0", "false", "False"}
OUT_JSON = os.environ.get("BA_OUT_JSON", "scripts/pp_complete_results.json")
'''

new = '''PROMO_TAX_COUNTRY = os.environ.get("BA_PROMO_TAX_COUNTRY", "").upper()
UI_MODE = os.environ.get("BA_UI_MODE", "custom").lower()
CONFIRM_MODE = os.environ.get("BA_CONFIRM_MODE", "pm").lower()  # pm|direct
# When 1 and promo tax is set, skip the second main tax update so confirm keeps promo amounts.
SKIP_MAIN_TAX = os.environ.get("BA_SKIP_MAIN_TAX", "0") not in {"0", "false", "False"}
MAX_ATTEMPTS = int(os.environ.get("BA_MAX_ATTEMPTS", "4"))
STOP_ON_FIRST = os.environ.get("BA_STOP_ON_FIRST", "1") not in {"0", "false", "False"}
DO_FULL = os.environ.get("BA_DO_FULL", "1") not in {"0", "false", "False"}
OUT_JSON = os.environ.get("BA_OUT_JSON", "scripts/pp_complete_results.json")
'''
if old not in t:
    raise SystemExit('config block not found')
t = t.replace(old, new, 1)

old2 = '''        if not DO_FULL:
            return res

        # main tax
        try:
            tax = stripe_http.stripe_update_tax_region(s_stripe, cs_id=cs, address=tax_addr)
            res["steps"]["tax"] = 200
            latest = stripe_http.merge_checkout_payload(latest, tax) if hasattr(stripe_http, "merge_checkout_payload") else dict(tax)
            ck = str(latest.get("init_checksum") or ck)
            pmt = latest.get("payment_method_types") or pmt
            amt = stripe_http.extract_expected_amount(latest)
            res["pmt"] = pmt
            res["amt"] = amt
            res["paypal"] = has_paypal(pmt)
            res["zero"] = str(amt) == "0"
            res["amts_tax"] = stripe_http.extract_display_amounts(latest) if hasattr(stripe_http, "extract_display_amounts") else {}
        except Exception as e:
            res["steps"]["tax_err"] = str(e)[:140]

        if not res["paypal"]:
'''

new2 = '''        if not DO_FULL:
            return res

        confirm_addr = dict(tax_addr)
        # main tax (optional; skip to keep promo tax amounts for recovery tests)
        if SKIP_MAIN_TAX:
            res["steps"]["tax"] = "skipped"
            if promo_addr is not None:
                confirm_addr = dict(promo_addr)
            res["amts_tax"] = stripe_http.extract_display_amounts(latest) if hasattr(stripe_http, "extract_display_amounts") else {}
        else:
            try:
                tax = stripe_http.stripe_update_tax_region(s_stripe, cs_id=cs, address=tax_addr)
                res["steps"]["tax"] = 200
                latest = stripe_http.merge_checkout_payload(latest, tax) if hasattr(stripe_http, "merge_checkout_payload") else dict(tax)
                ck = str(latest.get("init_checksum") or ck)
                pmt = latest.get("payment_method_types") or pmt
                amt = stripe_http.extract_expected_amount(latest)
                res["pmt"] = pmt
                res["amt"] = amt
                res["paypal"] = has_paypal(pmt)
                res["zero"] = str(amt) == "0"
                res["amts_tax"] = stripe_http.extract_display_amounts(latest) if hasattr(stripe_http, "extract_display_amounts") else {}
            except Exception as e:
                res["steps"]["tax_err"] = str(e)[:140]

        if not res["paypal"]:
'''
if old2 not in t:
    raise SystemExit('main tax block not found')
t = t.replace(old2, new2, 1)

t = t.replace(
'''            conf_resp = stripe_http.stripe_confirm_paypal_direct(
                s_stripe,
                cs_id=cs,
                init_checksum=ck,
                email=EMAIL,
                address=tax_addr,
                return_url=return_url,
''',
'''            conf_resp = stripe_http.stripe_confirm_paypal_direct(
                s_stripe,
                cs_id=cs,
                init_checksum=ck,
                email=EMAIL,
                address=confirm_addr,
                return_url=return_url,
''',
1)

t = t.replace(
'''        f"promo_tax={PROMO_TAX_COUNTRY or '-'} ui={UI_MODE} confirm={CONFIRM_MODE} full={DO_FULL} max={MAX_ATTEMPTS} ===",
''',
'''        f"promo_tax={PROMO_TAX_COUNTRY or '-'} skip_main_tax={int(SKIP_MAIN_TAX)} ui={UI_MODE} confirm={CONFIRM_MODE} full={DO_FULL} max={MAX_ATTEMPTS} ===",
''',
1)

# also patch pm-mode billing address fields if they hardcode tax_addr
for a,b in [
    ('"billing_details[address][country]": tax_addr.get("country")', '"billing_details[address][country]": confirm_addr.get("country")'),
    ('"billing_details[address][postal_code]": tax_addr.get("postal_code")', '"billing_details[address][postal_code]": confirm_addr.get("postal_code")'),
    ('"billing_details[address][state]": tax_addr.get("state")', '"billing_details[address][state]": confirm_addr.get("state")'),
    ('"billing_details[address][city]": tax_addr.get("city")', '"billing_details[address][city]": confirm_addr.get("city")'),
    ('"billing_details[address][line1]": tax_addr.get("line1")', '"billing_details[address][line1]": confirm_addr.get("line1")'),
    ('"billing_details[address][line2]": tax_addr.get("line2")', '"billing_details[address][line2]": confirm_addr.get("line2")'),
]:
    if a in t:
        t = t.replace(a,b)

p.write_text(t, encoding='utf-8')
print('patched ok', 'SKIP_MAIN_TAX' in t, 'confirm_addr' in t)
PY
