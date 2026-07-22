from pathlib import Path
import re
p = Path('scripts/pp_complete_extract.py')
t = p.read_text(encoding='utf-8')
# After conf_resp assignment in both branches, ensure we store top keys and try recursive find
# Replace poll loop with hammer-style poll
old = '''        # poll stripe page for redirect
        for _ in range(10):
            time.sleep(1.4)
            try:
                poll = stripe_http.stripe_poll(s_stripe, cs_id=cs)
            except Exception:
                # fallback get
                pr = s_stripe.get(
                    f"https://api.stripe.com/v1/payment_pages/{cs}",
                    params={"key": pk, "_stripe_version": STRIPE_INIT_VERSION},
                    headers={"Accept": "application/json", "Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/"},
                    timeout=8,
                )
                poll = pr.json() if pr.status_code == 200 else {}
            redir = find_redirect(poll if isinstance(poll, dict) else {})
            if redir:
                ok, ba, url = follow_ba(s_stripe, redir)
                if ok:
                    res["ba_ok"] = True
                    res["ba"] = ba
                    res["ba_url"] = url
                else:
                    res["err"] = "poll redirect no ba: " + url[:80]
                return res
            if isinstance(poll, dict):
                psa = poll.get("submission_attempt") or {}
                if isinstance(psa, dict) and psa.get("state") == "failed":
                    pe = psa.get("error") or {}
                    res["err"] = "failed:" + str(pe.get("code") if isinstance(pe, dict) else pe)
                    return res
        if not res.get("err"):
            res["err"] = "timeout approved but no redirect"
        return res
'''
new = '''        # poll using custom-checkout payment_pages GET (same as proven hammer)
        poll_params = {
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": "es_" + uuid.uuid4().hex[:11],
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": pk,
            "_stripe_version": STRIPE_INIT_VERSION,
        }
        ph = {"Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/", "Accept": "application/json"}
        for i in range(12):
            time.sleep(1.5)
            pr = s_stripe.get(
                f"https://api.stripe.com/v1/payment_pages/{cs}",
                params=poll_params,
                headers=ph,
                timeout=10,
            )
            if pr.status_code != 200:
                res["steps"]["poll_http"] = pr.status_code
                continue
            poll = pr.json()
            redir = find_redirect(poll)
            if redir:
                ok, ba, url = follow_ba(s_stripe, redir)
                if ok:
                    res["ba_ok"] = True
                    res["ba"] = ba
                    res["ba_url"] = url
                else:
                    res["err"] = "poll redirect no ba: " + url[:80]
                return res
            psa = poll.get("submission_attempt") or {}
            if isinstance(psa, dict):
                st = psa.get("state")
                res["steps"]["poll_sa"] = st
                if st == "failed":
                    pe = psa.get("error") or {}
                    res["err"] = "failed:" + str(pe.get("code") if isinstance(pe, dict) else pe)
                    return res
        if not res.get("err"):
            res["err"] = "timeout approved but no redirect"
        return res
'''
if old not in t:
    raise SystemExit('poll block missing')
t = t.replace(old, new)
# also capture confirm response keys when no immediate redirect
t = t.replace(
'''        redir = find_redirect(conf_resp if isinstance(conf_resp, dict) else {})
        if redir:
''',
'''        if isinstance(conf_resp, dict):
            res["steps"]["confirm_keys"] = list(conf_resp.keys())[:20]
            psa0 = conf_resp.get("submission_attempt") or {}
            if isinstance(psa0, dict):
                res["steps"]["confirm_sa"] = psa0.get("state")
        redir = find_redirect(conf_resp if isinstance(conf_resp, dict) else {})
        if redir:
'''
)
p.write_text(t, encoding='utf-8')
print('poll patched')
