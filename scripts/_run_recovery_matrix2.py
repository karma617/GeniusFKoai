import json, os, subprocess, time
from pathlib import Path
py = r".\.venv\Scripts\python.exe"
base = {"PYTHONIOENCODING":"utf-8","BA_STOP_ON_FIRST":"1","BA_APPROVE_PROXY":"US"}
jobs = [
 {"name":"US_promoJP_skipTax_direct","env":{"BA_CHECKOUT_PROXY":"US","BA_STRIPE_PROXY":"US","BA_PROMO_PROXY":"JP","BA_PROMO_TAX_COUNTRY":"JP","BA_BILLING_COUNTRY":"US","BA_BILLING_CURRENCY":"USD","BA_TAX_COUNTRY":"US","BA_SKIP_MAIN_TAX":"1","BA_CONFIRM_MODE":"direct","BA_DO_FULL":"1","BA_MAX_ATTEMPTS":"2","BA_OUT_JSON":"scripts/pp_complete_US_promoJP_skipTax_direct.json"}},
 {"name":"BR_create_US_stripe_probe","env":{"BA_CHECKOUT_PROXY":"BR","BA_STRIPE_PROXY":"US","BA_PROMO_PROXY":"","BA_PROMO_TAX_COUNTRY":"","BA_BILLING_COUNTRY":"US","BA_BILLING_CURRENCY":"USD","BA_TAX_COUNTRY":"US","BA_SKIP_MAIN_TAX":"0","BA_CONFIRM_MODE":"pm","BA_DO_FULL":"0","BA_MAX_ATTEMPTS":"3","BA_OUT_JSON":"scripts/pp_complete_BR_create_US_stripe_probe.json"}},
 {"name":"JP_create_US_stripe_probe","env":{"BA_CHECKOUT_PROXY":"JP","BA_STRIPE_PROXY":"US","BA_PROMO_PROXY":"","BA_PROMO_TAX_COUNTRY":"","BA_BILLING_COUNTRY":"US","BA_BILLING_CURRENCY":"USD","BA_TAX_COUNTRY":"US","BA_SKIP_MAIN_TAX":"0","BA_CONFIRM_MODE":"pm","BA_DO_FULL":"0","BA_MAX_ATTEMPTS":"3","BA_OUT_JSON":"scripts/pp_complete_JP_create_US_stripe_probe.json"}},
 {"name":"BR_create_BR_bill_probe","env":{"BA_CHECKOUT_PROXY":"BR","BA_STRIPE_PROXY":"US","BA_PROMO_PROXY":"","BA_PROMO_TAX_COUNTRY":"","BA_BILLING_COUNTRY":"BR","BA_BILLING_CURRENCY":"BRL","BA_TAX_COUNTRY":"BR","BA_SKIP_MAIN_TAX":"0","BA_CONFIRM_MODE":"pm","BA_DO_FULL":"0","BA_MAX_ATTEMPTS":"3","BA_OUT_JSON":"scripts/pp_complete_BR_create_BR_bill_probe.json"}},
 {"name":"JP_create_JP_bill_probe","env":{"BA_CHECKOUT_PROXY":"JP","BA_STRIPE_PROXY":"US","BA_PROMO_PROXY":"","BA_PROMO_TAX_COUNTRY":"","BA_BILLING_COUNTRY":"JP","BA_BILLING_CURRENCY":"JPY","BA_TAX_COUNTRY":"JP","BA_SKIP_MAIN_TAX":"0","BA_CONFIRM_MODE":"pm","BA_DO_FULL":"0","BA_MAX_ATTEMPTS":"3","BA_OUT_JSON":"scripts/pp_complete_JP_create_JP_bill_probe.json"}},
]
summary=[]
for job in jobs:
    name=job["name"]
    env=os.environ.copy(); env.update(base); env.update({k:str(v) for k,v in job["env"].items()})
    print(f"\n===== RUN {name} =====", flush=True)
    p=subprocess.run([py,"scripts/pp_complete_extract.py"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    text=(p.stdout or "") + ("\n"+p.stderr if p.stderr else "")
    Path(f"scripts/pp_complete_{name}.txt").write_text(text, encoding="utf-8")
    print(text[-1800:], flush=True)
    row={"name":name,"rc":p.returncode}
    jpath=Path(job["env"]["BA_OUT_JSON"])
    if jpath.exists():
        data=json.loads(jpath.read_text(encoding="utf-8"))
        items=data if isinstance(data,list) else [data]
        best=items[0] if items else {}
        for it in items:
            if isinstance(it,dict) and it.get("ba_ok"):
                best=it; break
        if isinstance(best,dict):
            for k in ["ba_ok","ba","amt","zero","paypal","pmt","approve_result","err","trial","promo_campaign","expected_amount","amt_after_promo","amts","amts_tax","steps"]:
                if k in best: row[k]=best.get(k)
    summary.append(row)
    time.sleep(0.4)
Path("scripts/pp_recovery_summary2.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n===== SUMMARY2 =====")
print(json.dumps(summary, ensure_ascii=False, indent=2))