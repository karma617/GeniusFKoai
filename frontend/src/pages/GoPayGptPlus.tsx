import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/utils";
import { TaskLogPanel } from "@/components/tasks/TaskLogPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Loader2, RefreshCw, Sparkles, UserPlus, X } from "lucide-react";

/**
 * GoPay 协议付款 ChatGPT Plus
 * ---------------------------------------------------------------
 * 三步流水线（参考 application/gopay_pay_chatgpt.py）：
 *
 *   ① 协议   generate_plus_link(country=ID, currency=IDR) → cashier_url
 *   ② 浏览器  打开 cashier_url，等用户/自动化跳到 app.midtrans.com → midtrans_url
 *   ③ 协议   GoPayPayment.pay(midtrans_url, gopay_account) 14 步 Midtrans API
 *
 * 该页面只负责选 ChatGPT/GoPay 账号 + 启动后台 task，详细日志在 TaskLogPanel 里
 * 实时滚动；后端跑完后会把 ChatGPT 账号标 subscribed。
 */

type AccountRow = {
  id: number;
  email: string;
  password?: string;
  user_id?: string;
  lifecycle_status?: string;
  display_status?: string;
  plan_state?: string;
  created_at?: string;
  cashier_url?: string;
  overview?: any;
  display_summary?: any;
  extra?: any;
};

function getLifecycleStatus(acc: AccountRow): string {
  return (
    acc.display_summary?.status?.lifecycle ||
    acc.lifecycle_status ||
    "registered"
  );
}

function getPlanState(acc: AccountRow): string {
  return (
    acc.display_summary?.status?.plan_state ||
    acc.plan_state ||
    acc.overview?.plan_state ||
    "unknown"
  );
}

const GOPAY_TRIAL_TAGS = new Set([
  "日本试用",
  "菲律宾试用",
  "英国试用",
  "印度尼西亚试用",
  "荷兰试用",
  "印度试用",
]);

function hasGopayTrialTag(acc: AccountRow): boolean {
  const badges: unknown[] = Array.isArray(acc.display_summary?.badges)
    ? acc.display_summary.badges
    : [];
  const chips: unknown[] = Array.isArray(acc.overview?.chips) ? acc.overview.chips : [];
  return [...badges, ...chips].some((tag) => {
    const label = typeof tag === "object" && tag !== null && "label" in tag
      ? tag.label
      : tag;
    return GOPAY_TRIAL_TAGS.has(String(label ?? "").trim());
  });
}

function isEligibleChatGptAccount(acc: AccountRow): boolean {
  const lifecycle = String(getLifecycleStatus(acc)).trim().toLowerCase();
  const planState = String(getPlanState(acc)).trim().toLowerCase();
  return (
    planState === "free" &&
    hasGopayTrialTag(acc) &&
    (lifecycle === "registered" || lifecycle === "rt_uploaded")
  );
}

function getBalanceRp(acc: AccountRow): number {
  const candidates = [
    acc.overview?.balance_rp,
    acc.display_summary?.balance_rp,
    acc.extra?.balance_rp,
  ];
  for (const v of candidates) {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return 0;
}

function getPhone(acc: AccountRow): string {
  return (
    acc.overview?.phone ||
    acc.extra?.phone ||
    acc.email ||
    ""
  );
}

function isGopayPinSet(acc: AccountRow): boolean {
  return acc.overview?.pin_set === true || Boolean(String(acc.password || "").trim());
}

function nonEmptyLines(value: string): string[] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function parseApiSmsPool(value: string): Array<{ phone: string; url: string }> {
  return nonEmptyLines(value).map((line, index) => {
    const separator = line.indexOf("----");
    if (separator < 0) throw new Error(`API 接码第 ${index + 1} 行格式错误，请使用 手机号----接码地址`);
    const phone = line.slice(0, separator).trim();
    const url = line.slice(separator + 4).trim();
    if (!/^\+[1-9]\d{7,14}$/.test(phone)) throw new Error(`API 接码第 ${index + 1} 行手机号无效`);
    try {
      const parsed = new URL(url);
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error();
    } catch {
      throw new Error(`API 接码第 ${index + 1} 行接码地址无效`);
    }
    return { phone, url };
  });
}

export default function GoPayGptPlus() {
  const [chatgptAccounts, setChatgptAccounts] = useState<AccountRow[]>([]);
  const [gopayAccounts, setGopayAccounts] = useState<AccountRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedChatgpt, setSelectedChatgpt] = useState<Set<number>>(new Set());
  const [selectedGopayId, setSelectedGopayId] = useState<number | null>(null);
  const country = "ID";
  const currency = "IDR";
  const [grabTimeout, setGrabTimeout] = useState(300);
  const [midtransOverride, setMidtransOverride] = useState("");
  // 浏览器模式（同 CtfGptPlus），bitbrowser_* 需要 profile id
  const [checkoutMode, setCheckoutMode] = useState("camoufox_headed");
  // GoPay 红包链接（余额不足时领红包补余额）
  const [envelopeUrl, setEnvelopeUrl] = useState("");
  // 并发数
  const [concurrency, setConcurrency] = useState(1);
  // 未选 ChatGPT 账号时先注册的数量
  const [registerCount, setRegisterCount] = useState(1);
  // GoPay 号来源：auto=先池后注册 / pool=只用号池 / register=强制现注册
  const [gopaySource, setGopaySource] = useState<"auto" | "pool" | "register">(
    "auto",
  );
  // 自动注册 GoPay 号用的 PIN
  const [gopayPin, setGopayPin] = useState("147258");
  // 接码渠道：herosms / smspool / smsbower
  const [smsProvider, setSmsProvider] = useState("herosms");
  // 拿号价格上限（USD）。herosms / smspool 都按 USD 计价，默认 0.11。
  // 留空 = 用后端默认值。
  const [maxPrice, setMaxPrice] = useState("0.11");
  const [maxPaymentAmountRp, setMaxPaymentAmountRp] = useState(0);
  // smspool 默认 api key
  const [smspoolApiKey, setSmspoolApiKey] = useState(
    "",
  );
  // smsbower 默认 api key（与 Hero-SMS 同协议，但活跃印尼号源更多）
  const [smsbowerApiKey, setSmsbowerApiKey] = useState(
    "",
  );
  const [fiveSimApiKey, setFiveSimApiKey] = useState("");
  const [apiSmsPool, setApiSmsPool] = useState("");
  const [proxyPool, setProxyPool] = useState("");
  // smsapi（固定手机号 + 查最新短信 API）：用户自己的实体卡/长期号
  const [smsapiPhone, setSmsapiPhone] = useState("");
  const [smsapiUrl, setSmsapiUrl] = useState("");
  // Hero-SMS API key 不存账号 extra（避免泄漏给前端 overview），付款步骤
  // 必须在每次任务提交时透传。默认填一个常用 key，留空则后端回退环境变量。
  const [herosmsApiKey, setHerosmsApiKey] = useState(
    "",
  );
  // 调试抓包开关：开启后抓到 midtrans_url 不关浏览器，停在付款页让人工手动
  // 走完 GoPay 网页付款，全程录 HAR + dump 每页 HTML，不跑协议付款。
  const [capturePayment, setCapturePayment] = useState(false);
  // Hosted 长链：优先使用 checkout 响应 URL，缺失时再走 Stripe init 补链。
  const [useStripeInit, setUseStripeInit] = useState(false);
  // Custom 短链：按响应 processor_entity 拼接 ChatGPT checkout URL。
  const [useShortLink, setUseShortLink] = useState(false);
  // 付款成功后自动换绑：买一个新印尼号把账号换绑过去，老号弃用，之后一直用新号付款
  const [autoRebind, setAutoRebind] = useState(false);
  // 换绑专用接码渠道（独立于注册渠道）：herosms / smsbower
  const [rebindProvider, setRebindProvider] = useState("herosms");
  const [rebindSmsKey, setRebindSmsKey] = useState("");
  const [rebindCountry, setRebindCountry] = useState("");
  const [rebindService, setRebindService] = useState("");

  const [formLoaded, setFormLoaded] = useState(false);

  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("gopay-gptplus-form-v2") || "{}");
      if (Number.isFinite(saved.grabTimeout)) setGrabTimeout(saved.grabTimeout);
      if (typeof saved.checkoutMode === "string") setCheckoutMode(saved.checkoutMode);
      if (typeof saved.envelopeUrl === "string") setEnvelopeUrl(saved.envelopeUrl);
      if (Number.isFinite(saved.concurrency)) setConcurrency(saved.concurrency);
      if (Number.isFinite(saved.registerCount)) setRegisterCount(saved.registerCount);
      if (["auto", "pool", "register"].includes(saved.gopaySource)) setGopaySource(saved.gopaySource);
      if (typeof saved.gopayPin === "string") setGopayPin(saved.gopayPin);
      if (typeof saved.smsProvider === "string") setSmsProvider(saved.smsProvider);
      if (typeof saved.maxPrice === "string") setMaxPrice(saved.maxPrice);
      if (typeof saved.smspoolApiKey === "string") setSmspoolApiKey(saved.smspoolApiKey);
      if (typeof saved.smsbowerApiKey === "string") setSmsbowerApiKey(saved.smsbowerApiKey);
      if (typeof saved.fiveSimApiKey === "string") setFiveSimApiKey(saved.fiveSimApiKey);
      if (typeof saved.apiSmsPool === "string") setApiSmsPool(saved.apiSmsPool);
      if (typeof saved.proxyPool === "string") setProxyPool(saved.proxyPool);
      if (typeof saved.smsapiPhone === "string") setSmsapiPhone(saved.smsapiPhone);
      if (typeof saved.smsapiUrl === "string") setSmsapiUrl(saved.smsapiUrl);
      if (typeof saved.herosmsApiKey === "string") setHerosmsApiKey(saved.herosmsApiKey);
      if (typeof saved.useShortLink === "boolean") setUseShortLink(saved.useShortLink);
      if (typeof saved.useStripeInit === "boolean") {
        setUseStripeInit(saved.useStripeInit && saved.useShortLink !== true);
      }
      if (typeof saved.autoRebind === "boolean") setAutoRebind(saved.autoRebind);
      if (typeof saved.rebindProvider === "string") setRebindProvider(saved.rebindProvider);
      if (typeof saved.rebindSmsKey === "string") setRebindSmsKey(saved.rebindSmsKey);
      if (typeof saved.rebindCountry === "string") setRebindCountry(saved.rebindCountry);
      if (typeof saved.rebindService === "string") setRebindService(saved.rebindService);
    } catch {
      localStorage.removeItem("gopay-gptplus-form-v2");
    } finally {
      setFormLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (!formLoaded) return;
    localStorage.setItem("gopay-gptplus-form-v2", JSON.stringify({
      grabTimeout, checkoutMode, envelopeUrl, concurrency, registerCount,
      gopaySource, gopayPin, smsProvider, maxPrice, smspoolApiKey,
      smsbowerApiKey, fiveSimApiKey, apiSmsPool, proxyPool, smsapiPhone, smsapiUrl,
      herosmsApiKey, useStripeInit, useShortLink, autoRebind,
      rebindProvider, rebindSmsKey, rebindCountry, rebindService,
    }));
  }, [
    formLoaded, grabTimeout, checkoutMode, envelopeUrl, concurrency,
    registerCount, gopaySource, gopayPin, smsProvider, maxPrice,
    smspoolApiKey, smsbowerApiKey, fiveSimApiKey, apiSmsPool, proxyPool, smsapiPhone,
    smsapiUrl, herosmsApiKey, useStripeInit, useShortLink, autoRebind,
    rebindProvider, rebindSmsKey, rebindCountry, rebindService,
  ]);

  const BROWSER_MODE_OPTIONS = [
    { value: "camoufox_headed", label: "Camoufox 前台" },
    { value: "camoufox_headless", label: "Camoufox 后台" },
    { value: "bitbrowser_headed", label: "BitBrowser 前台" },
    { value: "bitbrowser_hidden", label: "BitBrowser 隐藏" },
    { value: "bitbrowser_headless", label: "BitBrowser 后台" },
  ];
  const [taskId, setTaskId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [registeringGopay, setRegisteringGopay] = useState(false);
  const [chatgptSearch, setChatgptSearch] = useState("");

  const reload = async () => {
    setLoading(true);
    try {
      const fetchChatgptAccounts = async (
        status: "registered" | "rt_uploaded",
      ): Promise<AccountRow[]> => {
        const pageSize = 100;
        const buildParams = (page: number) => {
          const params = new URLSearchParams({
            platform: "chatgpt",
            status,
            tag: "试用",
            page: String(page),
            page_size: String(pageSize),
          });
          if (chatgptSearch) params.set("email", chatgptSearch);
          return params;
        };
        const first = await apiFetch(`/accounts?${buildParams(1)}`);
        const items: AccountRow[] = [...(first.items || [])];
        const totalPages = Math.ceil(Number(first.total || items.length) / pageSize);
        if (totalPages > 1) {
          const remaining = await Promise.all(
            Array.from({ length: totalPages - 1 }, (_, index) =>
              apiFetch(`/accounts?${buildParams(index + 2)}`),
            ),
          );
          for (const response of remaining) items.push(...(response.items || []));
        }
        return items;
      };
      const [registeredAccounts, rtUploadedAccounts, gopayRes] = await Promise.all([
        fetchChatgptAccounts("registered"),
        fetchChatgptAccounts("rt_uploaded"),
        apiFetch(`/accounts?platform=gopay&page=1&page_size=100`),
      ]);
      const eligibleById = new Map<number, AccountRow>();
      for (const account of [...registeredAccounts, ...rtUploadedAccounts]) {
        if (account.id && isEligibleChatGptAccount(account)) {
          eligibleById.set(account.id, account);
        }
      }
      const eligibleAccounts = Array.from(eligibleById.values());
      const eligibleIds = new Set(eligibleById.keys());
      setChatgptAccounts(eligibleAccounts);
      setSelectedChatgpt((current) =>
        new Set(Array.from(current).filter((id) => eligibleIds.has(id))),
      );
      setGopayAccounts(gopayRes.items || []);
    } catch (err) {
      console.error("加载账号失败", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const t = setTimeout(reload, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatgptSearch]);

  const usableGopayAccounts = useMemo(
    () => gopayAccounts.filter((acc) => getBalanceRp(acc) >= 1),
    [gopayAccounts],
  );

  const togglePick = (id: number) => {
    setSelectedChatgpt((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const start = async () => {
    if (selectedChatgpt.size === 0 && registerCount < 1) {
      alert("请至少选 1 个 ChatGPT 账号，或设置注册数量 ≥ 1");
      return;
    }
    if (gopaySource === "pool" && !selectedGopayId) {
      alert("「仅用号池」模式请在下方点选一个 GoPay 账号");
      return;
    }
    const taskAccountCount = Math.max(selectedChatgpt.size || registerCount, 1);
    if (selectedGopayId && taskAccountCount > 1) {
      alert("指定单个 GoPay 账号时只能处理 1 个 ChatGPT 账号");
      return;
    }
    const proxies = nonEmptyLines(proxyPool);
    if (proxies.length < taskAccountCount) {
      alert(`代理池只有 ${proxies.length} 条，任务有 ${taskAccountCount} 个账号`);
      return;
    }
    if (new Set(proxies).size !== proxies.length) {
      alert("代理池存在重复代理，每个账号必须使用不同代理");
      return;
    }
    if (smsProvider === "api_sms" && gopaySource !== "pool") {
      try {
        const entries = parseApiSmsPool(apiSmsPool);
        if (entries.length < taskAccountCount) {
          alert(`API 接码号码池只有 ${entries.length} 条，任务有 ${taskAccountCount} 个账号`);
          return;
        }
      } catch (err: any) {
        alert(err?.message || String(err));
        return;
      }
    }
    setStarting(true);
    try {
      const body: any = {
        chatgpt_account_ids: [...selectedChatgpt],
        gopay_account_id:
          gopaySource === "pool" ? selectedGopayId : 0,
        country,
        currency,
        checkout_mode: checkoutMode,
        envelope_url: envelopeUrl.trim(),
        concurrency,
        grab_timeout: grabTimeout,
        midtrans_url_override: midtransOverride.trim() || "",
        herosms_api_key: herosmsApiKey.trim(),
        gopay_source: gopaySource,
        auto_register_gopay: gopaySource !== "pool",
        gopay_pin: gopayPin.trim() || "147258",
        sms_provider: smsProvider,
        smspool_api_key: smspoolApiKey.trim(),
        smsbower_api_key: smsbowerApiKey.trim(),
        five_sim_api_key: fiveSimApiKey.trim(),
        api_sms_pool: apiSmsPool.trim(),
        proxy_pool: proxyPool.trim(),
        smsapi_url: smsapiUrl.trim(),
        smsapi_phone: smsapiPhone.trim(),
        max_price: maxPrice.trim(),
        max_payment_amount_rp: maxPaymentAmountRp,
        capture_payment: capturePayment,
        use_stripe_init: useStripeInit,
        use_short_link: useShortLink,
        link_mode: useStripeInit || useShortLink ? "browser" : "protocol",
        auto_rebind: autoRebind,
        rebind_provider: rebindProvider,
        rebind_sms_key: rebindSmsKey.trim(),
        rebind_country: rebindCountry.trim(),
        rebind_service: rebindService.trim(),
      };
      // 未选 ChatGPT 账号 → 从注册开始
      if (selectedChatgpt.size === 0) {
        body.register_count = registerCount;
      }
      const res = await apiFetch("/tasks/gopay-pay-chatgpt", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setTaskId(res.task_id);
    } catch (err: any) {
      alert(`启动任务失败: ${err?.message || err}`);
    } finally {
      setStarting(false);
    }
  };

  const registerGopayAccount = async () => {
    const pin = gopayPin.trim() || "147258";
    if (!/^\d{6}$/.test(pin)) {
      alert("GoPay PIN 必须是 6 位数字");
      return;
    }
    if (nonEmptyLines(proxyPool).length < 1) {
      alert("注册 GoPay 账号至少需要 1 条固定代理");
      return;
    }
    if (smsProvider === "smsapi" && (!smsapiPhone.trim() || !smsapiUrl.trim())) {
      alert("SmsApi 渠道需要填写固定手机号和查最新短信 API URL");
      return;
    }
    if (smsProvider === "api_sms") {
      try {
        if (parseApiSmsPool(apiSmsPool).length < 1) {
          alert("API 接码号码池至少需要 1 条记录");
          return;
        }
      } catch (err: any) {
        alert(err?.message || String(err));
        return;
      }
    }

    setRegisteringGopay(true);
    try {
      const body = {
        gopay_pin: pin,
        envelope_url: envelopeUrl.trim(),
        sms_provider: smsProvider,
        smspool_api_key: smspoolApiKey.trim(),
        smsbower_api_key: smsbowerApiKey.trim(),
        five_sim_api_key: fiveSimApiKey.trim(),
        api_sms_pool: apiSmsPool.trim(),
        proxy_pool: proxyPool.trim(),
        smsapi_url: smsapiUrl.trim(),
        smsapi_phone: smsapiPhone.trim(),
        herosms_api_key: herosmsApiKey.trim(),
        max_price: maxPrice.trim(),
        auto_rebind: autoRebind,
        rebind_provider: rebindProvider,
        rebind_sms_key: rebindSmsKey.trim(),
        rebind_country: rebindCountry.trim(),
        rebind_service: rebindService.trim(),
      };
      const res = await apiFetch("/tasks/gopay-register-account", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setTaskId(res.task_id);
    } catch (err: any) {
      alert(`启动 GoPay 注册任务失败: ${err?.message || err}`);
    } finally {
      setRegisteringGopay(false);
    }
  };

  const closeTask = () => {
    setTaskId(null);
    reload();
  };

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 overflow-y-auto lg:overflow-hidden">
      <Card className="shrink-0 bg-[var(--bg-pane)]/40 border border-[var(--border)] shadow-sm">
        <div className="flex flex-col gap-3 px-5 py-4 border-b border-[var(--border)]/50 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Sparkles className="h-5 w-5 text-[var(--accent)]" />
            <h1 className="text-lg font-semibold tracking-tight text-[var(--text-primary)]">
              GoPay 生成 GPTPlus
            </h1>
            <Badge variant="secondary" className="ml-2">
              印尼 GoPay 协议付款
            </Badge>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={reload}
              disabled={loading}
              className="h-8"
            >
              {loading ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
              )}
              刷新
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={registerGopayAccount}
              disabled={starting || registeringGopay}
              className="h-8"
            >
              {registeringGopay ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <UserPlus className="mr-1.5 h-3.5 w-3.5" />
              )}
              注册GoPay账户
            </Button>
            <Button
              size="sm"
              onClick={start}
              disabled={starting || registeringGopay || (selectedChatgpt.size === 0 && registerCount < 1)}
              className="h-8 shadow-sm"
            >
              {starting ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <Sparkles className="mr-1.5 h-3.5 w-3.5" />
              )}
              开始 ({selectedChatgpt.size > 0 ? selectedChatgpt.size : `注册${registerCount}`})
            </Button>
          </div>
        </div>
        <div className="px-5 py-3 text-xs text-[var(--text-muted)] grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
          <div>
            <label className="block mb-1">浏览器模式</label>
            <select
              value={checkoutMode}
              onChange={(e) => setCheckoutMode(e.target.value)}
              className="control-surface control-surface-compact w-full"
            >
              {BROWSER_MODE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block mb-1">国家</label>
            <select value={country} disabled className="control-surface control-surface-compact w-full">
              <option value="ID">印尼 (ID)</option>
            </select>
          </div>
          <div>
            <label className="block mb-1">货币</label>
            <select value={currency} disabled className="control-surface control-surface-compact w-full">
              <option value="IDR">IDR</option>
            </select>
          </div>
          <div>
            <label className="block mb-1">并发数</label>
            <input
              type="number"
              min={1}
              max={5}
              value={concurrency}
              onChange={(e) => setConcurrency(Number(e.target.value))}
              className="control-surface control-surface-compact w-full text-center"
            />
          </div>
          <div>
            <label className="block mb-1">浏览器抓 URL 超时（秒）</label>
            <input
              type="number"
              min={60}
              value={grabTimeout}
              onChange={(e) => setGrabTimeout(Number(e.target.value))}
              className="control-surface control-surface-compact w-full"
            />
          </div>
          {checkoutMode.startsWith("bitbrowser") && (
            <div className="md:col-span-2 flex items-end">
              <p className="text-[11px] text-[var(--text-muted)] leading-tight">
                BitBrowser 模式自动从「设置 → BitBrowser」的 Profile 池按并发取号，
                每个线程独占一个 Profile，无需手填 ID。
              </p>
            </div>
          )}
          <div className="md:col-span-4">
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={useStripeInit}
                onChange={(e) => {
                  setUseStripeInit(e.target.checked);
                  if (e.target.checked) setUseShortLink(false);
                }}
                className="h-4 w-4"
              />
              <span className="text-[var(--text)]">
                Stripe Hosted 长链（用 accessToken 直接生成 cashier_url）
              </span>
            </label>
            {useStripeInit && (
              <p className="mt-1 text-[11px] text-[var(--text-muted)] leading-tight">
                使用 hosted checkout + 印尼地区/币种 + Plus 免费优惠创建长链；优先采用 OpenAI 响应中的
                <code>url</code>，缺失时再调用 Stripe <code>payment_pages/init</code> 补成长链。
                后续仍由浏览器选择 GoPay 并抓取 Midtrans URL。
              </p>
            )}
          </div>
          <div className="md:col-span-4">
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={useShortLink}
                onChange={(e) => {
                  setUseShortLink(e.target.checked);
                  if (e.target.checked) setUseStripeInit(false);
                }}
                className="h-4 w-4"
              />
              <span className="text-[var(--text)]">
                ChatGPT Custom 短链（自动使用响应中的 processor_entity）
              </span>
            </label>
            {useShortLink && (
              <p className="mt-1 text-[11px] text-[var(--text-muted)] leading-tight">
                用 custom checkout + 印尼地区/币种 + Plus 免费优惠建单，并同步 taxes 获取支付方式；
                再使用响应中的 <code>processor_entity</code> 和 checkout session 拼成短链。与长链模式二选一。
                <strong>短链是 ChatGPT 托管页、URL 里没有 token，打开时必须带账号登录 cookie</strong>，
                所以会把该 ChatGPT 账号的 cookie 注入抓 midtrans 的浏览器；
                <strong>请用 Camoufox 模式</strong>（BitBrowser 的 Chromium 注入 cookie 会被拒，需 profile 已登录）。
              </p>
            )}
          </div>
          <div className="md:col-span-4">
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={capturePayment}
                onChange={(e) => setCapturePayment(e.target.checked)}
                className="h-4 w-4"
              />
              <span className="text-[var(--text)]">
                调试抓包模式（抓到 midtrans 后不关浏览器，人工手动付款，录 HAR + 每页 HTML）
              </span>
            </label>
            {capturePayment && (
              <p className="mt-1 text-[11px] text-[var(--text-muted)] leading-tight">
                开启后程序不跑协议付款：抓到 midtrans_url 会停在付款页，请手动走完 GoPay 网页付款全流程。
                产物存到工作目录 <code>_gopay_capture/&lt;时间戳&gt;/</code>（HAR + 各页面 HTML）。
                完成后在该目录新建一个名为 <code>STOP</code> 的空文件结束抓包。
                <strong>要拿 HAR 请用 Camoufox 模式</strong>（BitBrowser CDP 录不了 HAR，只有 HTML）。
              </p>
            )}
          </div>
          <div className="md:col-span-4">
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={autoRebind}
                onChange={(e) => setAutoRebind(e.target.checked)}
                className="h-4 w-4"
              />
              <span className="text-[var(--text)]">
                付款成功后自动换绑（买一个新印尼号，把账号换绑过去；老号弃用，之后一直用新印尼号付款）
              </span>
            </label>
            {autoRebind && (
              <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
                <div>
                  <label className="block mb-1 text-[var(--text-muted)]">换绑接码渠道</label>
                  <select
                    value={rebindProvider}
                    onChange={(e) => setRebindProvider(e.target.value)}
                    className="control-surface control-surface-compact w-full"
                  >
                    <option value="herosms">Hero-SMS</option>
                    <option value="smsbower">SMSBower</option>
                  </select>
                </div>
                <div>
                  <label className="block mb-1 text-[var(--text-muted)]">换绑接码 API Key</label>
                  <input
                    type="password"
                    value={rebindSmsKey}
                    onChange={(e) => setRebindSmsKey(e.target.value)}
                    placeholder="独立 key，留空回退环境变量"
                    className="control-surface control-surface-compact w-full"
                  />
                </div>
                <div>
                  <label className="block mb-1 text-[var(--text-muted)]">换绑国家（固定印尼=6）</label>
                  <input
                    type="text"
                    value={rebindCountry}
                    onChange={(e) => setRebindCountry(e.target.value)}
                    placeholder="6"
                    className="control-surface control-surface-compact w-full text-center"
                  />
                </div>
                <div>
                  <label className="block mb-1 text-[var(--text-muted)]">换绑服务（留空=ni）</label>
                  <input
                    type="text"
                    value={rebindService}
                    onChange={(e) => setRebindService(e.target.value)}
                    placeholder="ni"
                    className="control-surface control-surface-compact w-full text-center"
                  />
                </div>
                <div className="md:col-span-4 text-[11px] text-[var(--text-muted)] leading-tight">
                  换绑渠道独立于注册渠道：注册用 smsapi（固定号）时换绑仍会从这里买一次性号。
                  换绑国家固定印尼（+62 / country=6），因为换绑后的新号要继续用于下一轮 GoPay 付款，外国号付不了。
                  流程：付款成功 → 解绑 OpenAI LLC → 把账号换绑到新印尼号 → 老号弃用，之后一直用新号付款。
                </div>
              </div>
            )}
          </div>
          {selectedChatgpt.size === 0 && (
            <div>
              <label className="block mb-1">注册 ChatGPT 数量（未选账号时）</label>
              <input
                type="number"
                min={1}
                max={50}
                value={registerCount}
                onChange={(e) => setRegisterCount(Number(e.target.value))}
                className="control-surface control-surface-compact w-full text-center"
              />
            </div>
          )}
        </div>
        <div className="px-5 pb-4 grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
          <div>
            <label className="block mb-1 text-[var(--text-muted)]">
              Midtrans URL 直连（可选，跳过浏览器抓取）
            </label>
            <input
              type="text"
              value={midtransOverride}
              onChange={(e) => setMidtransOverride(e.target.value)}
              placeholder="https://app.midtrans.com/snap/v4/redirection/..."
              className="control-surface control-surface-compact w-full"
            />
          </div>
          <div className="md:col-span-2">
            <label className="block mb-1 text-[var(--text-muted)]">
              GoPay 红包链接（可选，余额不足时领取补余额）
            </label>
            <input
              type="text"
              value={envelopeUrl}
              onChange={(e) => setEnvelopeUrl(e.target.value)}
              placeholder="https://app.gopay.co.id/NF8p/qps2s1y0"
              className="control-surface control-surface-compact w-full"
            />
          </div>
          <div className="md:col-span-2">
            <label className="block mb-1 text-[var(--text-muted)]">
              任务代理池（每个账号一行）
            </label>
            <textarea
              rows={4}
              value={proxyPool}
              onChange={(e) => setProxyPool(e.target.value)}
              placeholder={"http://user:pass@host:port\nhttp://user:pass@host:port"}
              className="control-surface w-full resize-y font-mono text-xs"
            />
            <div className="mt-1 text-xs text-[var(--text-muted)]">
              已填写 {nonEmptyLines(proxyPool).length} 条；同一账号的注册、cashier、浏览器和 GoPay 付款固定使用同一条代理。
            </div>
          </div>
          <div>
            <label className="block mb-1 text-[var(--text-muted)]">
              GoPay 号来源
            </label>
            <select
              value={gopaySource}
              onChange={(e) =>
                setGopaySource(e.target.value as "auto" | "pool" | "register")
              }
              className="control-surface control-surface-compact w-full"
            >
              <option value="auto">自动（先用号池，没号再注册）</option>
              <option value="pool">仅用号池（没号直接失败）</option>
              <option value="register">强制注册新号（忽略号池）</option>
            </select>
            <div className="mt-1 text-xs font-mono text-[var(--accent)]">
              当前选择 = {gopaySource}
            </div>
          </div>
          <div>
            <label className="block mb-1 text-[var(--text-muted)]">
              自动注册 GoPay PIN（6 位）
            </label>
            <input
              type="text"
              maxLength={6}
              value={gopayPin}
              onChange={(e) => setGopayPin(e.target.value.replace(/\D/g, ""))}
              placeholder="147258"
              className="control-surface control-surface-compact w-full text-center font-mono"
            />
          </div>
          <div>
            <label className="block mb-1 text-[var(--text-muted)]">接码渠道</label>
            <select
              value={smsProvider}
              onChange={(e) => setSmsProvider(e.target.value)}
              className="control-surface control-surface-compact w-full"
            >
              <option value="herosms">Hero-SMS</option>
              <option value="smsbower">SMSBower</option>
              <option value="smspool">SMSPool</option>
              <option value="five_sim">5sim</option>
              <option value="smsapi">SmsApi（单个固定号）</option>
              <option value="api_sms">API接码（号码池）</option>
            </select>
          </div>
          <div>
            <label className="block mb-1 text-[var(--text-muted)]">
              拿号价格上限（USD）
            </label>
            <input
              type="text"
              value={maxPrice}
              onChange={(e) =>
                setMaxPrice(e.target.value.replace(/[^0-9.]/g, ""))
              }
              placeholder="0.11"
              className="control-surface control-surface-compact w-full text-center font-mono"
            />
            <div className="mt-1 text-xs text-[var(--text-muted)]">
              Hero-SMS / SMSPool 都按 USD 计价。留空或 0 = 不限价。
            </div>
          </div>
          <div>
            <label className="block mb-1 text-[var(--text-muted)]">最高支付金额（IDR）</label>
            <input
              type="number"
              min={0}
              step={1}
              value={maxPaymentAmountRp}
              onChange={(e) => setMaxPaymentAmountRp(Math.max(0, Math.floor(Number(e.target.value) || 0)))}
              className="control-surface control-surface-compact w-full text-center font-mono"
            />
            <div className="mt-1 text-xs text-[var(--text-muted)]">
              0 = 允许免费订单和 GoPay 绑定所需的 1 IDR 验证；其他付费必须显式填写上限。
            </div>
          </div>
          {smsProvider === "smspool" && (
            <div>
              <label className="block mb-1 text-[var(--text-muted)]">
                SMSPool API Key
              </label>
              <input
                type="password"
                value={smspoolApiKey}
                onChange={(e) => setSmspoolApiKey(e.target.value)}
                placeholder="SMSPool API key"
                className="control-surface control-surface-compact w-full"
              />
            </div>
          )}
          {smsProvider === "smsbower" && (
            <div>
              <label className="block mb-1 text-[var(--text-muted)]">
                SMSBower API Key
              </label>
              <input
                type="password"
                value={smsbowerApiKey}
                onChange={(e) => setSmsbowerApiKey(e.target.value)}
                placeholder="SMSBower API key"
                className="control-surface control-surface-compact w-full"
              />
            </div>
          )}
          {smsProvider === "five_sim" && (
            <div>
              <label className="block mb-1 text-[var(--text-muted)]">
                5sim API Key
              </label>
              <input
                type="password"
                value={fiveSimApiKey}
                onChange={(e) => setFiveSimApiKey(e.target.value)}
                placeholder="5sim API key"
                className="control-surface control-surface-compact w-full"
              />
            </div>
          )}
          {smsProvider === "api_sms" && (
            <div className="md:col-span-2">
              <label className="block mb-1 text-[var(--text-muted)]">
                API 接码号码池（每个任务账号一行）
              </label>
              <textarea
                rows={4}
                value={apiSmsPool}
                onChange={(e) => setApiSmsPool(e.target.value)}
                placeholder={"+447476554147----https://example.com/api/record?token=xxx"}
                className="control-surface w-full resize-y font-mono text-xs"
              />
              <div className="mt-1 text-xs text-[var(--text-muted)]">
                已填写 {nonEmptyLines(apiSmsPool).length} 条，按任务账号顺序一一分配。
              </div>
            </div>
          )}
          {smsProvider === "smsapi" && (
            <>
              <div>
                <label className="block mb-1 text-[var(--text-muted)]">
                  固定手机号（含国码，如 +6281930860580）
                </label>
                <input
                  type="text"
                  value={smsapiPhone}
                  onChange={(e) => setSmsapiPhone(e.target.value)}
                  placeholder="+6281930860580"
                  className="control-surface control-surface-compact w-full font-mono"
                />
              </div>
              <div>
                <label className="block mb-1 text-[var(--text-muted)]">
                  查最新短信 API URL（含 token）
                </label>
                <input
                  type="password"
                  value={smsapiUrl}
                  onChange={(e) => setSmsapiUrl(e.target.value)}
                  placeholder="https://api.sms8.net/api/record?token=xxxx"
                  className="control-surface control-surface-compact w-full"
                />
                <div className="mt-1 text-xs text-[var(--text-muted)]">
                  自有实体卡 / 长期号 + 该号的「查最新短信」接口。注册/PIN/付款
                  共用同一个号，靠短信时间区分新旧 OTP。
                </div>
              </div>
            </>
          )}
          <div className="md:col-span-2">
            <label className="block mb-1 text-[var(--text-muted)]">
              Hero-SMS API key（付款 OTP 用；留空则后端回退环境变量 OPAI_HEROSMS_API_KEY）
            </label>
            <input
              type="password"
              value={herosmsApiKey}
              onChange={(e) => setHerosmsApiKey(e.target.value)}
              placeholder="herosms 接码平台 API key"
              className="control-surface control-surface-compact w-full"
            />
          </div>
        </div>
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 flex-1 min-h-0 overflow-hidden">
        {/* ChatGPT 账号列表 */}
        <Card className="flex flex-col min-h-0 bg-[var(--bg-pane)]/40 border border-[var(--border)]">
          <div className="px-4 py-3 border-b border-[var(--border)]/50 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-[var(--text-primary)]">
                ChatGPT 账号
              </span>
              <Badge variant="secondary">已选 {selectedChatgpt.size}</Badge>
            </div>
            <input
              type="text"
              value={chatgptSearch}
              onChange={(e) => setChatgptSearch(e.target.value)}
              placeholder="搜索邮箱"
              className="control-surface control-surface-compact"
              style={{ width: 200 }}
            />
          </div>
          <div className="flex-1 overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-[var(--bg-card)]">
                <tr className="text-left text-[var(--text-muted)]">
                  <th className="px-3 py-2 w-8"></th>
                  <th className="px-3 py-2">邮箱</th>
                  <th className="px-3 py-2">套餐</th>
                  <th className="px-3 py-2">cashier_url</th>
                </tr>
              </thead>
              <tbody>
                {chatgptAccounts.map((acc) => {
                  const checked = selectedChatgpt.has(acc.id);
                  const planState = getPlanState(acc);
                  const isSubscribed = planState === "subscribed";
                  const lifecycleStatus = getLifecycleStatus(acc);
                  const cashier = acc.cashier_url || acc.overview?.cashier_url || "";
                  return (
                    <tr
                      key={acc.id}
                      className={`hover:bg-[var(--bg-hover)] ${
                        isSubscribed ? "opacity-60" : ""
                      }`}
                    >
                      <td className="px-3 py-1.5">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => togglePick(acc.id)}
                          className="h-4 w-4 accent-[var(--accent)]"
                        />
                      </td>
                      <td className="px-3 py-1.5 text-[var(--text-primary)]">
                        {acc.email}
                      </td>
                      <td className="px-3 py-1.5">
                        <Badge
                          variant={
                            isSubscribed
                              ? "success"
                              : lifecycleStatus === "invalid"
                                ? "danger"
                                : "secondary"
                          }
                        >
                          {planState}
                        </Badge>
                      </td>
                      <td className="px-3 py-1.5 text-[var(--text-muted)] truncate max-w-[200px]">
                        {cashier ? "✓" : "-"}
                      </td>
                    </tr>
                  );
                })}
                {chatgptAccounts.length === 0 && (
                  <tr>
                    <td
                      colSpan={4}
                      className="px-3 py-6 text-center text-[var(--text-muted)]"
                    >
                      暂无 ChatGPT 账号
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>

        {/* GoPay 账号列表 */}
        <Card className="flex flex-col min-h-0 bg-[var(--bg-pane)]/40 border border-[var(--border)]">
          <div className="px-4 py-3 border-b border-[var(--border)]/50 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-[var(--text-primary)]">
                GoPay 账号（余额 ≥ 1 IDR 才可用）
              </span>
              <Badge variant="secondary">
                可用 {usableGopayAccounts.length}/{gopayAccounts.length}
              </Badge>
            </div>
            <div className="flex items-center gap-2 text-xs text-[var(--text-muted)]">
              {gopaySource === "pool"
                ? "点选下方一个号用于付款"
                : gopaySource === "register"
                  ? "强制注册新号（忽略下方号池）"
                  : "自动挑选（先用号池，没号再注册）"}
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-[var(--bg-card)]">
                <tr className="text-left text-[var(--text-muted)]">
                  <th className="px-3 py-2 w-8"></th>
                  <th className="px-3 py-2">手机号</th>
                  <th className="px-3 py-2">余额 (IDR)</th>
                  <th className="px-3 py-2">状态</th>
                </tr>
              </thead>
              <tbody>
                {gopayAccounts.map((acc) => {
                  const balance = getBalanceRp(acc);
                  const usable = balance >= 1;
                  const phone = getPhone(acc);
                  const pinSet = isGopayPinSet(acc);
                  const lifecycleStatus = getLifecycleStatus(acc);
                  const selected =
                    gopaySource === "pool" && selectedGopayId === acc.id;
                  return (
                    <tr
                      key={acc.id}
                      className={`hover:bg-[var(--bg-hover)] ${
                        !usable ? "opacity-50" : ""
                      } ${selected ? "bg-[var(--accent-soft)]" : ""}`}
                      onClick={() => {
                        if (gopaySource === "pool" && usable) {
                          setSelectedGopayId(acc.id);
                        }
                      }}
                    >
                      <td className="px-3 py-1.5">
                        {gopaySource === "pool" ? (
                          <input
                            type="radio"
                            checked={selected}
                            onChange={() => setSelectedGopayId(acc.id)}
                            disabled={!usable}
                          />
                        ) : null}
                      </td>
                      <td className="px-3 py-1.5 text-[var(--text-primary)] font-mono">
                        {phone}
                      </td>
                      <td className="px-3 py-1.5 text-[var(--text-primary)] font-mono">
                        {balance.toLocaleString()}
                      </td>
                      <td className="px-3 py-1.5">
                        <div className="flex flex-wrap items-center gap-1">
                          <Badge
                            variant={
                              usable
                                ? "success"
                                : lifecycleStatus === "invalid"
                                  ? "danger"
                                  : "secondary"
                            }
                          >
                            {usable ? "可用" : "无余额"}
                          </Badge>
                          {pinSet && <Badge variant="success">PIN已设</Badge>}
                        </div>
                      </td>
                    </tr>
                  );
                })}
                {gopayAccounts.length === 0 && (
                  <tr>
                    <td
                      colSpan={4}
                      className="px-3 py-6 text-center text-[var(--text-muted)]"
                    >
                      暂无 GoPay 账号，请到「账号 / GoPay」注册
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>
      </div>

      {/* 任务执行日志弹窗 */}
      {taskId && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-3 sm:p-4"
          onClick={(e) => e.target === e.currentTarget && closeTask()}
        >
          <div
            className="flex h-[calc(100dvh-1.5rem)] max-h-[85dvh] min-h-0 w-full max-w-[1200px] flex-col overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-2xl sm:h-[85dvh] sm:w-[min(92vw,1200px)]"
          >
            <div className="flex shrink-0 items-center justify-between border-b border-[var(--border)] px-5 py-3">
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">
                GoPay 任务执行日志
              </h3>
              <button
                onClick={closeTask}
                className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-hidden p-3 sm:p-4">
              <TaskLogPanel taskId={taskId} onDone={() => reload()} />
            </div>
            <div className="flex shrink-0 justify-end border-t border-[var(--border)] px-5 py-3">
              <Button variant="outline" size="sm" onClick={closeTask}>
                关闭
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
