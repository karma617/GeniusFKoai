from pathlib import Path
p = Path("scripts/pp_dual_proxy_extract.py")
t = p.read_text(encoding="utf-8")
start = t.find("            ctx = {")
marker = '            if res["approve_result"] != "approved":'
end = t.find(marker)
if start < 0 or end < 0:
    raise SystemExit(f"markers {start} {end}")
end2 = t.find("                return res", end)
end2 = t.find("\n", end2) + 1
new = r'''            ctx = {
                "stripe_js_id": str(uuid.uuid4()),
                "elements_session_id": "es_" + uuid.uuid4().hex[:11],
                "elements_session_config_id": cid or str(uuid.uuid4()),
                "config_id": cid,
                "init_checksum": ck,
                "locale": "en",
                "runtime_version": STRIPE_RUNTIME_VERSION,
            }
            pmb = {
                "billing_details[name]": bill_addr["name"],
                "billing_details[email]": bill_addr["email"],
                "billing_details[address][country]": bill_addr["country"],
                "billing_details[address][line1]": bill_addr["line1"],
                "billing_details[address][city]": bill_addr["city"],
                "billing_details[address][postal_code]": bill_addr["postal_code"],
                "type": "paypal",
                "payment_user_agent": "stripe.js/" + STRIPE_RUNTIME_VERSION + "; stripe-js-v3/" + STRIPE_RUNTIME_VERSION + "; payment-element; deferred-intent",
                "referrer": "https://chatgpt.com",
                "time_on_page": "35000",
                "client_attribution_metadata[checkout_session_id]": cs,
                "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
                "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
                "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
                "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
                "client_attribution_metadata[merchant_integration_source]": "elements",
                "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
                "client_attribution_metadata[merchant_integration_version]": "2021",
                "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
                "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
                "guid": uuid.uuid4().hex,
                "muid": uuid.uuid4().hex,
                "sid": uuid.uuid4().hex,
                "key": pk,
                "_stripe_version": STRIPE_INIT_VERSION,
            }
            if bill_addr.get("state"):
                pmb["billing_details[address][state]"] = bill_addr["state"]
            pmr = s_chk.post(f"{STRIPE_API_BASE}/payment_methods", data=pmb, headers=sh, timeout=30)
            res["steps"]["pm"] = pmr.status_code
            if pmr.status_code != 200:
                res["err"] = "pm " + str(pmr.status_code)
                res["body"] = pmr.text[:180]
                return res
            pmid = str(pmr.json().get("id") or "")
            if not pmid.startswith("pm_"):
                res["err"] = "bad pm"
                return res

            surl = "https://chatgpt.com/checkout/verify?stripe_session_id=" + cs + "&processor_entity=" + ent + "&plan_type=plus"
            rurl_base = "https://pay.openai.com/c/pay/" + cs + "?success_return_url=" + surl
            conf = {
                "guid": uuid.uuid4().hex,
                "muid": uuid.uuid4().hex,
                "sid": uuid.uuid4().hex,
                "payment_method": pmid,
                "init_checksum": ck,
                "version": STRIPE_RUNTIME_VERSION,
                "expected_amount": str(res.get("amt") or "0"),
                "expected_payment_method_type": "paypal",
                "return_url": rurl_base,
                "elements_session_client[session_id]": ctx["elements_session_id"],
                "elements_session_client[locale]": "en",
                "elements_session_client[referrer_host]": "chatgpt.com",
                "elements_session_client[is_aggregation_expected]": "false",
                "elements_session_client[elements_init_source]": "custom_checkout",
                "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
                "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
                "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
                "elements_options_client[saved_payment_method][enable_save]": "never",
                "elements_options_client[saved_payment_method][enable_redisplay]": "never",
                "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
                "client_attribution_metadata[checkout_session_id]": cs,
                "client_attribution_metadata[checkout_config_id]": ctx.get("config_id", ""),
                "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
                "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
                "client_attribution_metadata[merchant_integration_source]": "checkout",
                "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
                "client_attribution_metadata[merchant_integration_version]": "custom",
                "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
                "client_attribution_metadata[payment_method_selection_flow]": "automatic",
                "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
                "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
                "consent[terms_of_service]": "accepted",
                "key": pk,
                "_stripe_version": STRIPE_INIT_VERSION,
            }
            cr = s_chk.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/confirm", data=conf, headers=sh, timeout=30)
            res["steps"]["confirm"] = cr.status_code
            if cr.status_code != 200:
                res["err"] = "confirm " + str(cr.status_code)
                res["body"] = cr.text[:180]
                return res
            cd = cr.json()
            pmr_url = find_redirect(cd)
            if pmr_url:
                rg = s_chk.get(pmr_url, allow_redirects=True, timeout=20, headers={"Referer": "https://pay.openai.com/"})
                fin = str(getattr(rg, "url", ""))
                bm = BA_RE.search(fin)
                if bm:
                    res["ba"] = bm.group(1)
                    res["ba_url"] = fin[:220]
                    res["ba_ok"] = True
                    res["approve_result"] = "confirm_redirect"
                else:
                    res["err"] = "no ba: " + fin[:80]
                return res

            try:
                s_chk.post(
                    "https://chatgpt.com/backend-api/sentinel/ping",
                    json={},
                    headers={
                        "x-openai-target-path": "/backend-api/sentinel/ping",
                        "x-openai-target-route": "/backend-api/sentinel/ping",
                    },
                    timeout=4,
                )
            except Exception:
                pass
            pt = "/backend-api/payments/checkout/approve"
            ah = {
                "Authorization": "Bearer " + TOKEN,
                "Content-Type": "application/json",
                "oai-language": "zh-CN",
                "Referer": f"https://chatgpt.com/checkout/{ent}/{cs}",
                "x-openai-target-path": pt,
                "x-openai-target-route": pt,
            }
            if COOKIE:
                ah["cookie"] = COOKIE
            ar = s_chk.post(
                "https://chatgpt.com" + pt,
                json={"checkout_session_id": cs, "processor_entity": ent},
                headers=ah,
                timeout=20,
            )
            res["steps"]["approve_http"] = ar.status_code
            res["steps"]["approve_path"] = pt
            try:
                ab = ar.json()
                res["approve_result"] = ab.get("result", "unknown_" + str(ar.status_code))
            except Exception:
                res["approve_result"] = "http_" + str(ar.status_code)
            if ar.status_code != 200:
                res["err"] = "approve " + str(ar.status_code)
                return res
            if res["approve_result"] != "approved":
                return res
'''
p.write_text(t[:start] + new + t[end2:], encoding="utf-8")
print("patched", start, end2)
PY
