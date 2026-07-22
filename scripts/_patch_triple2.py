from pathlib import Path
p = Path('scripts/pp_triple_proxy_extract.py')
t = p.read_text(encoding='utf-8')
old_setup = '''    s_chk = None
    s_promo = None
    try:
        p1 = fetch_proxy(CHECKOUT_PROXY)
        s_chk = make_session(p1)
        res["steps"]["proxy_checkout"] = p1.split("@")[-1]

        if PROMO_WHEN != "none" and PROMO_PROXY:
            p2 = fetch_proxy(PROMO_PROXY)
            s_promo = make_session(p2)
            res["steps"]["proxy_promo"] = p2.split("@")[-1]
'''
new_setup = '''    s_chk = None
    s_stripe = None
    s_appr = None
    s_promo = None
    sessions = []
    try:
        p1 = fetch_proxy(CHECKOUT_PROXY)
        s_chk = make_session(p1)
        sessions.append(s_chk)
        res["steps"]["proxy_checkout"] = p1.split("@")[-1]

        if STRIPE_PROXY == CHECKOUT_PROXY:
            s_stripe = s_chk
        else:
            p_st = fetch_proxy(STRIPE_PROXY)
            s_stripe = make_session(p_st)
            sessions.append(s_stripe)
            res["steps"]["proxy_stripe"] = p_st.split("@")[-1]

        if APPROVE_PROXY == CHECKOUT_PROXY:
            s_appr = s_chk
        elif APPROVE_PROXY == STRIPE_PROXY:
            s_appr = s_stripe
        else:
            p_ap = fetch_proxy(APPROVE_PROXY)
            s_appr = make_session(p_ap)
            sessions.append(s_appr)
            res["steps"]["proxy_approve"] = p_ap.split("@")[-1]

        if PROMO_WHEN != "none" and PROMO_PROXY:
            if PROMO_PROXY == STRIPE_PROXY:
                s_promo = s_stripe
            elif PROMO_PROXY == CHECKOUT_PROXY:
                s_promo = s_chk
            else:
                p2 = fetch_proxy(PROMO_PROXY)
                s_promo = make_session(p2)
                sessions.append(s_promo)
                res["steps"]["proxy_promo"] = p2.split("@")[-1]
'''
if old_setup not in t:
    raise SystemExit('setup missing')
t = t.replace(old_setup, new_setup)
t = t.replace(
'''        "checkout_proxy": CHECKOUT_PROXY,
        "promo_proxy": PROMO_PROXY,
''',
'''        "checkout_proxy": CHECKOUT_PROXY,
        "stripe_proxy": STRIPE_PROXY,
        "approve_proxy": APPROVE_PROXY,
        "promo_proxy": PROMO_PROXY,
        "tax_country": TAX_COUNTRY,
'''
)
repls = [
('s_chk.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/init"', 's_stripe.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/init"'),
('tr = payment_pages_update(s_chk, cs, pk, bill_addr, ck)', 'tr = payment_pages_update(s_stripe, cs, pk, ADDRS.get(TAX_COUNTRY, bill_addr), ck)'),
('pmr = s_chk.post(f"{STRIPE_API_BASE}/payment_methods"', 'pmr = s_stripe.post(f"{STRIPE_API_BASE}/payment_methods"'),
('cr = s_chk.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/confirm"', 'cr = s_stripe.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/confirm"'),
('rg = s_chk.get(pmr_url', 'rg = s_stripe.get(pmr_url'),
('pr = s_chk.get(f"{STRIPE_API_BASE}/payment_pages/{cs}"', 'pr = s_stripe.get(f"{STRIPE_API_BASE}/payment_pages/{cs}"'),
]
for old,new in repls:
    print(old[:48], t.count(old))
    t = t.replace(old, new)
t = t.replace('s_chk.post(\n                    "https://chatgpt.com/backend-api/sentinel/ping"', 's_appr.post(\n                    "https://chatgpt.com/backend-api/sentinel/ping"')
t = t.replace('ar = s_chk.post(\n                "https://chatgpt.com" + pt,', 'ar = s_appr.post(\n                "https://chatgpt.com" + pt,')
old_fin = '''    finally:
        for s in (s_chk, s_promo):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
'''
new_fin = '''    finally:
        for s in (sessions if "sessions" in locals() else [s_chk, s_promo]):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
'''
print('finally', old_fin in t)
if old_fin in t:
    t = t.replace(old_fin, new_fin)
t = t.replace(
'f"=== dual proxy extract checkout={CHECKOUT_PROXY} promo={PROMO_PROXY} "',
'f"=== triple extract chk={CHECKOUT_PROXY} stripe={STRIPE_PROXY} approve={APPROVE_PROXY} promo={PROMO_PROXY or \'-\'} "'
)
p.write_text(t, encoding='utf-8')
print('done', len(t))
