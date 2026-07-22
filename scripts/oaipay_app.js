const $ = (id) => document.getElementById(id);
const SHOW_JP_DATA_PANEL = true; // JP 数据面板是否可用（后端接口始终存在）；实际显示由 syncModeUi 按模式控制
const ENABLE_MODE2_CHIME = true; // 模式2 完成或失败时是否播放提示音
const ENABLE_MODE5_CHIME = true; // 模式5 完成或失败时是否播放提示音

function isUpiMode(mode) { return mode === 5; }
function isIdealMode(mode) { return mode === 6; }
function isPixMode(mode) { return mode === 7; }
function isCdkMode(mode) { return mode === 5 || mode === 7; }
function isJpFlowMode(mode) { return mode === 2; }

function applyJpPanelVisibility() {
  const mainLayout = $("mainLayout");
  const jpPanel = $("jpPanel");
  if (!mainLayout || !jpPanel) return;
  // 初始隐藏，由 syncModeUi 根据选中模式控制
  const mode = parseInt(($("extractMode") || {}).value) || 3;
  const showJp = SHOW_JP_DATA_PANEL && isJpFlowMode(mode);
  mainLayout.classList.toggle("jp-hidden", !showJp);
  jpPanel.hidden = !showJp;
}

applyJpPanelVisibility();

const generateBtn = $("generate");
const stopBtn = $("stopGenerate");
const spinner = $("spinner");
const generateText = $("generateText");
const copyBtn = $("copy");
const openLink = $("openLink");
const statusBar = $("statusBar");
let _abortController = null;
const progressBar = $("progressBar");
const progressBarFill = $("progressBarFill");
const progressText = $("progressText");
const errorDetail = $("errorDetail");
const errorContent = $("errorContent");
const resultCard = $("resultCard");
let currentLongUrl = "";
let lastNonPpMode = "3";
let suppressAutoModeSwitch = false;
let _hideProgressTimer = null;
let _upiCountdownTimer = null;
let _upiStatusTimer = null;
let _upiExpiresAt = 0; // 二维码绝对过期时间戳（秒），以此计算剩余，规避展示延迟
let _tokenInfoTimer = null;
let _tokenInfoSeq = 0;
let _tokenInfoAbort = null;
let _lastTokenInfoContext = null;
let _tokenInfoValidKey = "";
let _mode5CdkTimer = null;
let _mode5CdkAbort = null;
let _mode5CdkSeq = 0;
let _mode5CdkValidCode = "";
let _mode5CdkAvailable = 0;
let _isGenerating = false;
let _captchaId = "";
let _captchaProvider = "local";
let _turnstileToken = "";
let _turnstileWidgetId = null;
const EXTRACTION_STATS_MODES = new Set([2, 5, 6, 7]);
const EXTRACTION_STATS_MODE_NAMES = {
  2: "PayPal 长连接",
  5: "UPI 二维码",
  6: "iDEAL 二维码",
  7: "Pix 二维码",
};

const EXTRACTOR_MODE_TITLES = {
  1: "手机号链提取器",
  2: "PayPal 长连接提取器",
  3: "Team Codex 低价链提取器",
  4: "PayPal 协议处理器",
  5: "UPI 二维码提取器",
  6: "iDEAL 二维码提取器",
  7: "Pix 二维码提取器",
};
let _selectedStatsMode = null;
let _extractionStatsPayload = null;
let _statsRefreshTimer = null;
let _statsClockTimer = null;
let _statsNextRefreshAt = 0;
let _statsLastUpdatedAt = 0;
let _statsLoadFailed = false;
let _statsLoadPromise = null;

function extractionStatsTimeLabel(epochSeconds) {
  const date = new Date(Number(epochSeconds || 0) * 1000);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function extractionStatsClockLabel(epochMilliseconds) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(epochMilliseconds));
}

function extractionStatsBucketTitle(bucket) {
  const bucketMinutes = Number((_extractionStatsPayload || {}).bucketMinutes || 5);
  const start = new Date(Number(bucket.start || 0) * 1000);
  const end = new Date(start.getTime() + bucketMinutes * 60 * 1000);
  const clock = (date) => new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
  const sameDate = start.toDateString() === end.toDateString();
  const range = sameDate
    ? `${clock(start)}-${clock(end)}`
    : `${extractionStatsTimeLabel(bucket.start)}-${extractionStatsTimeLabel(Number(bucket.start) + bucketMinutes * 60)}`;
  return `${range} 成功 ${Number(bucket.successes || 0)} 次`;
}

function updateExtractionStatsClock() {
  const updated = $("extractStatsUpdated");
  if (!updated) return;
  const now = Date.now();
  const remaining = Math.max(0, Math.ceil((_statsNextRefreshAt - now) / 1000));
  if (_statsLoadFailed) {
    updated.textContent = `刷新失败 · ${remaining} 秒后重试`;
  } else if (_statsLastUpdatedAt) {
    updated.textContent = `更新于 ${extractionStatsClockLabel(_statsLastUpdatedAt)} · ${remaining} 秒后刷新`;
  } else {
    updated.textContent = "等待刷新...";
  }
}

function renderExtractionStats() {
  const panel = document.querySelector(".extract-stats-panel");
  const grid = $("extractStatsGrid");
  const title = $("extractStatsTitle");
  if (!panel || !grid || !title || !EXTRACTION_STATS_MODES.has(_selectedStatsMode)) return;
  title.textContent = `${EXTRACTION_STATS_MODE_NAMES[_selectedStatsMode]}的最近 12 小时提取状态`;

  const data = _extractionStatsPayload && _extractionStatsPayload.modes
    ? _extractionStatsPayload.modes[String(_selectedStatsMode)]
    : null;
  grid.replaceChildren();
  if (!data) {
    panel.classList.add("is-error");
    return;
  }

  panel.classList.remove("is-error");
  (data.buckets || []).forEach((bucket, index, buckets) => {
    const cell = document.createElement("span");
    cell.className = "extract-stats-cell";
    if (bucket.attempts) cell.classList.add("has-activity");
    if (bucket.hasSuccess) cell.classList.add("is-success");
    if (index === buckets.length - 1) cell.classList.add("is-current");
    cell.title = extractionStatsBucketTitle(bucket);
    cell.setAttribute("aria-label", cell.title);
    grid.appendChild(cell);
  });
}

function loadExtractionStats() {
  if (_statsLoadPromise) return _statsLoadPromise;
  _statsLoadPromise = (async () => {
    try {
      const response = await fetch("/api/extraction-stats", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      _extractionStatsPayload = await response.json();
      _statsLastUpdatedAt = Date.now();
      _statsLoadFailed = false;
    } catch (error) {
      console.warn("Failed to load extraction stats", error);
      _statsLoadFailed = true;
    }
    renderExtractionStats();
    updateExtractionStatsClock();
  })().finally(() => {
    _statsLoadPromise = null;
  });
  return _statsLoadPromise;
}

function scheduleExtractionStatsRefresh() {
  if (_statsRefreshTimer) window.clearTimeout(_statsRefreshTimer);
  const delay = 60 * 1000 - (Date.now() % (60 * 1000)) + 500;
  _statsNextRefreshAt = Date.now() + delay;
  updateExtractionStatsClock();
  _statsRefreshTimer = window.setTimeout(async () => {
    const mode = Number(($('extractMode') || {}).value);
    if (EXTRACTION_STATS_MODES.has(mode)) await loadExtractionStats();
    scheduleExtractionStatsRefresh();
  }, delay);
}

function syncExtractionStatsMode(mode) {
  const panel = document.querySelector(".extract-stats-panel");
  if (!panel) return;
  const tracked = EXTRACTION_STATS_MODES.has(mode);
  panel.hidden = !tracked;
  panel.classList.toggle("is-single-column", tracked && !isJpFlowMode(mode));
  if (!tracked) return;
  const changed = _selectedStatsMode !== mode;
  _selectedStatsMode = mode;
  renderExtractionStats();
  updateExtractionStatsClock();
  if (changed && !_extractionStatsPayload) loadExtractionStats();
}

function initializeExtractionStats() {
  scheduleExtractionStatsRefresh();
  _statsClockTimer = window.setInterval(updateExtractionStatsClock, 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    const mode = Number(($('extractMode') || {}).value);
    if (EXTRACTION_STATS_MODES.has(mode)) loadExtractionStats();
    scheduleExtractionStatsRefresh();
  });
}

async function loadCaptcha() {
  if (_captchaProvider !== "local") return;
  _captchaId = "";
  $("captchaAnswer").value = "";
  $("captchaImage").removeAttribute("src");
  try {
    const resp = await fetch("/api/captcha", { method: "POST", cache: "no-store" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    _captchaId = data.id;
    $("captchaImage").src = `${data.imageUrl}?t=${Date.now()}`;
  } catch (err) {
    setStatus(`验证码加载失败：${err.message}`, "error");
  }
}

function loadTurnstileScript() {
  return new Promise((resolve, reject) => {
    if (window.turnstile) { resolve(); return; }
    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.async = true;
    script.defer = true;
    script.onload = resolve;
    script.onerror = () => reject(new Error("Cloudflare Turnstile 加载失败"));
    document.head.appendChild(script);
  });
}

async function initializeCaptcha() {
  try {
    const resp = await fetch("/api/captcha/config", { cache: "no-store" });
    const config = await resp.json();
    if (!resp.ok) throw new Error(config.detail || `HTTP ${resp.status}`);
    _captchaProvider = config.provider || "local";
    if (_captchaProvider === "turnstile") {
      $("localCaptcha").hidden = true;
      $("turnstileCaptcha").hidden = false;
      await loadTurnstileScript();
      _turnstileWidgetId = window.turnstile.render("#turnstileCaptcha", {
        sitekey: config.siteKey,
        theme: "auto",
        action: "generate_long_link",
        callback: (token) => { _turnstileToken = token; },
        "expired-callback": () => { _turnstileToken = ""; },
        "error-callback": () => { _turnstileToken = ""; },
      });
    } else {
      $("localCaptcha").hidden = false;
      $("turnstileCaptcha").hidden = true;
      await loadCaptcha();
    }
  } catch (err) {
    setStatus(`人机验证加载失败：${err.message}`, "error");
  }
}

function resetCaptcha() {
  if (_captchaProvider === "turnstile") {
    _turnstileToken = "";
    if (window.turnstile && _turnstileWidgetId !== null) window.turnstile.reset(_turnstileWidgetId);
  } else {
    loadCaptcha();
  }
}
const UPI_QR_EXPIRES_SECONDS = 300;
const IDEAL_QR_EXPIRES_SECONDS = 900;
const PIX_QR_EXPIRES_SECONDS = 3600;
const QR_STATUS_POLL_INTERVAL_MS = 5000;
const PROXY_POOL_STORAGE_PREFIX = "openai_pay_proxy_pool_";
const EXTRACT_MODE_STORAGE_KEY = "openai_pay_extract_mode";

function readMode6ProxyMode() {
  return "fixed";
}

function proxyPoolConfigs(mode = parseInt($("extractMode").value) || 3) {
  if (mode === 7) return [
    { key: "checkout", storageKey: "mode7_main_br", title: "代理池 - BR", desc: "全流程从此池取用；建议至少填写两条不同出口IP的巴西代理，不要只放一个代理" },
  ];
  if (mode === 6) return [
    { key: "checkout", storageKey: "mode6_checkout_nl", legacyStorageKeys: ["mode6_provider_nl"], title: "代理池1 - NL", desc: "用于 checkout、Stripe、iDEAL provider 和首次 approve；单次尝试复用同一条" },
    { key: "promotion", storageKey: "mode6_promotion_vn", title: "代理池2 - VN", desc: "仅用于同一个 checkout 的 promotion update" },
  ];
  if (mode === 5) return [
    { key: "checkout", storageKey: "mode5_main_in", title: "代理池1 - IN", desc: "请填写印度代理" },
    { key: "promotion", storageKey: "mode5_update_poll_tr", title: "代理池2 - JP / BR / TR", desc: "请填写日本、巴西或土耳其代理" },
  ];
  if (mode === 2) return [
    { key: "checkout", storageKey: `mode${mode}_provider_us`, title: "代理池 - US", desc: "请填写代理池" },
    { key: "promotion", storageKey: `mode${mode}_update_poll_tr`, title: "代理池 - TR", desc: "请填写土耳其代理" },
  ];
  if (mode === 4) return [{ key: "checkout", storageKey: "region_jp", legacyStorageKeys: ["mode4_checkout_jp"], title: "代理池1 - JP", desc: "模式4只需要 JP 代理池" }];
  return [{ key: "checkout", storageKey: `mode${mode}_checkout`, title: "代理池1", desc: "当前模式只需要一个代理池" }];
}

function proxyPoolStorageKey(cfgOrKey) {
  if (typeof cfgOrKey === "object" && cfgOrKey) return PROXY_POOL_STORAGE_PREFIX + (cfgOrKey.storageKey || cfgOrKey.key);
  const mode = parseInt($("extractMode").value) || 3;
  const cfg = proxyPoolConfigs(mode).find((item) => item.key === cfgOrKey);
  return PROXY_POOL_STORAGE_PREFIX + ((cfg && (cfg.storageKey || cfg.key)) || cfgOrKey);
}

function storedProxyPoolValue(cfg) {
  const value = localStorage.getItem(proxyPoolStorageKey(cfg));
  if (value !== null) return value;
  for (const legacyKey of (cfg.legacyStorageKeys || [])) {
    const legacyValue = localStorage.getItem(PROXY_POOL_STORAGE_PREFIX + legacyKey);
    if (legacyValue !== null) return legacyValue;
  }
  return localStorage.getItem(PROXY_POOL_STORAGE_PREFIX + cfg.key) || "";
}

function ensureProxyPoolUi() {
  if ($("proxyPoolsPanel")) return $("proxyPoolsPanel");
  const panel = document.createElement("div");
  panel.id = "proxyPoolsPanel";
  panel.className = "proxy-pools";
  const oldProxy = $("proxy");
  const oldPool = $("proxyPool");
  const anchor = oldProxy ? oldProxy.closest(".field") : (oldPool ? oldPool.closest(".field") : null);
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(panel, anchor);
  if (oldProxy && oldProxy.closest(".field")) oldProxy.closest(".field").style.display = "none";
  if (oldPool && oldPool.closest(".field")) oldPool.closest(".field").style.display = "none";
  return panel;
}

function readProxyPools() {
  const pools = {};
  for (const cfg of proxyPoolConfigs()) {
    const el = $("proxyPool_" + cfg.key);
    pools[cfg.key] = el ? el.value.trim() : "";
  }
  return pools;
}

function firstProxyPoolText() {
  const pools = readProxyPools();
  return pools.checkout || "";
}

function proxyPoolsValid() {
  return proxyPoolConfigs().every((cfg) => {
    const el = $("proxyPool_" + cfg.key);
    return Boolean(el && el.value.trim());
  });
}

function updateProxyPoolStatus() {
  const status = $("proxyPoolStatus");
  if (!status) return;
  const countText = proxyPoolConfigs().map((cfg) => {
    const el = $("proxyPool_" + cfg.key);
    const count = el ? el.value.split(/\r?\n|,/).map((x) => x.trim()).filter(Boolean).length : 0;
    return `${cfg.title}: ${count} 条`;
  }).join(" / ");
  status.textContent = `${countText}。不带协议头默认 http://，支持 http:// 和 socks5://。`;
  updateGenerateButtonState();
  updateTokenMeta();
}

function restoreExtractMode() {
  const select = $("extractMode");
  if (!select) return;
  const saved = localStorage.getItem(EXTRACT_MODE_STORAGE_KEY);
  if (saved && Array.from(select.options).some((option) => option.value === saved && !option.disabled)) {
    select.value = saved;
  }
}

function renderProxyPools() {
  const panel = ensureProxyPoolUi();
  if (!panel) return;
  panel.innerHTML = "";
  for (const cfg of proxyPoolConfigs()) {
    const field = document.createElement("div");
    field.className = "field proxy-pool-field";
    field.innerHTML = `
      <div class="input-label-row">
        <label for="proxyPool_${cfg.key}">${cfg.title}</label>
        <button class="btn-secondary proxy-test-btn" type="button" data-proxy-key="${cfg.key}" title="随机从当前代理池抽取一条进行连通性测试">随机测试</button>
      </div>
      <textarea id="proxyPool_${cfg.key}" class="proxy-pool-input" spellcheck="false" placeholder="hostname:port:username:password&#10;socks5://username:password@host:port&#10;username:password@hostname:port&#10;hostname:port@username:password"></textarea>
      <div class="field-help">${cfg.desc}；每行一个代理，不带协议头默认 http://。</div>
      <div class="proxy-test-result" id="proxyTest_${cfg.key}"></div>
    `;
    panel.appendChild(field);
    const textarea = $("proxyPool_" + cfg.key);
    textarea.value = storedProxyPoolValue(cfg);
    textarea.addEventListener("input", () => {
      localStorage.setItem(proxyPoolStorageKey(cfg), textarea.value.trim());
      updateProxyPoolStatus();
    });
  }
  const status = document.createElement("div");
  status.id = "proxyPoolStatus";
  status.className = "field-help proxy-pool-status";
  panel.appendChild(status);
  panel.querySelectorAll(".proxy-test-btn").forEach((btn) => btn.addEventListener("click", () => testProxyPool(btn.dataset.proxyKey)));
  updateProxyPoolStatus();
}

async function testProxyPool(key) {
  const textarea = $("proxyPool_" + key);
  const result = $("proxyTest_" + key);
  if (!textarea || !result) return;
  const proxyPool = textarea.value.trim();
  if (!proxyPool) {
    result.textContent = "请先填写代理池";
    result.className = "proxy-test-result error";
    return;
  }
  result.textContent = "随机抽取代理并测试中...";
  result.className = "proxy-test-result";
  try {
    const resp = await fetch("/api/test-proxy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proxies: proxyPool, label: key }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.ok === false) {
      result.textContent = `随机抽中第 ${data.index || "-"} 条，测试失败：${data.detail || data.error || `HTTP ${resp.status}`}`;
      result.className = "proxy-test-result error";
      return;
    }
    result.textContent = `随机抽中第 ${data.index} 条，测试成功：${data.country || data.country_code || "未知国家"} / ${data.ip || "未知IP"}`;
    result.className = "proxy-test-result success";
  } catch (err) {
    result.textContent = `测试失败：${err instanceof Error ? err.message : String(err)}`;
    result.className = "proxy-test-result error";
  }
}

function normalizeNonPpMode(value) {
  return (value === "1" || value === "4") ? "3" : (value || "3");
}

function setUpiQrState(state) {
  // state: "active" | "paid" | "expired" | "failed"
  const shell = $("upiQrShell");
  const overlay = $("upiQrStatusOverlay");
  const iconEl = $("upiQrOverlayIcon");
  const textEl = $("upiQrOverlayText");
  const countdownEl = $("upiCountdown");
  const terminal = state === "paid" || state === "expired" || state === "failed";

  if (shell) shell.classList.toggle("is-terminal", terminal);
  if (overlay) {
    overlay.classList.toggle("show", terminal);
    overlay.classList.toggle("variant-paid", state === "paid");
    overlay.classList.toggle("variant-expired", state === "expired");
    overlay.classList.toggle("variant-failed", state === "failed");
    overlay.setAttribute("aria-hidden", terminal ? "false" : "true");
  }
  const map = {
    paid: { icon: "✓", text: "支付成功" },
    expired: { icon: "!", text: "二维码已过期" },
    failed: { icon: "✕", text: "支付失败" },
  };
  if (terminal) {
    if (iconEl) iconEl.textContent = map[state].icon;
    if (textEl) textEl.textContent = map[state].text;
    // 终态：不再显示有效期倒计时，状态直接体现在二维码上
    if (countdownEl) countdownEl.style.display = "none";
  }
}

function clearUpiCountdownTimer() {
  if (_upiCountdownTimer) { clearInterval(_upiCountdownTimer); _upiCountdownTimer = null; }
}

function stopUpiStatusPolling() {
  if (_upiStatusTimer) { clearInterval(_upiStatusTimer); _upiStatusTimer = null; }
}

function stopUpiCountdown() {
  clearUpiCountdownTimer();
  stopUpiStatusPolling();
  const el = $("upiCountdown"); if (el) { el.style.display = "none"; el.style.color = "var(--text-muted)"; }
  const label = $("upiCountdownLabel"); if (label) label.textContent = "剩余有效期：";
  setUpiQrState("active");
}

function upiExpiresAtAbsolute(rawExpiresAt) {
  return qrExpiresAtAbsolute(rawExpiresAt, UPI_QR_EXPIRES_SECONDS);
}

function idealExpiresAtAbsolute(rawExpiresAt) {
  return qrExpiresAtAbsolute(rawExpiresAt, IDEAL_QR_EXPIRES_SECONDS);
}

function pixExpiresAtAbsolute(rawExpiresAt) {
  return qrExpiresAtAbsolute(rawExpiresAt, PIX_QR_EXPIRES_SECONDS);
}

function qrExpiresAtAbsolute(rawExpiresAt, fallbackSeconds) {
  // 返回绝对过期时间戳（秒）。后端给了就用真实值；否则以"当前+fallback"兜底。
  const ts = parseInt(rawExpiresAt, 10);
  if (ts && !isNaN(ts) && ts > 1000000000) return ts;
  return Math.floor(Date.now() / 1000) + fallbackSeconds;
}

function resyncUpiExpiry(rawExpiresAt) {
  // 状态轮询拿到真实过期时间时，校正本地倒计时（绝对时间戳，无需重启计时器）
  const ts = parseInt(rawExpiresAt, 10);
  if (ts && !isNaN(ts) && ts > 1000000000 && Math.abs(ts - _upiExpiresAt) > 1) {
    _upiExpiresAt = ts;
  }
}

function startUpiCountdown(absExpiresAt) {
  clearUpiCountdownTimer();
  _upiExpiresAt = absExpiresAt;
  const countdownEl = $("upiCountdown");
  const valueEl = $("upiCountdownValue");
  if (!countdownEl || !valueEl) return;
  countdownEl.style.display = "";
  setUpiQrState("active");

  function update() {
    // 已进入终态则停止倒计时
    const shell = $("upiQrShell");
    if (shell && shell.classList.contains("is-terminal")) { clearUpiCountdownTimer(); return; }
    const remaining = Math.floor(_upiExpiresAt - Date.now() / 1000);
    if (remaining <= 0) {
      clearUpiCountdownTimer();
      stopUpiStatusPolling();
      setUpiQrState("expired");
      return;
    }
    const m = Math.floor(remaining / 60);
    const s = remaining % 60;
    valueEl.textContent = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    countdownEl.style.color = remaining <= 60 ? "var(--danger, #e53e3e)" : "var(--text-muted)";
  }
  update();
  _upiCountdownTimer = setInterval(update, 1000);
}

function applyUpiStatus(status) {
  if (status !== "paid" && status !== "expired" && status !== "failed") return; // pending/unknown 继续等待
  clearUpiCountdownTimer();
  stopUpiStatusPolling();
  setUpiQrState(status);
}

function applyIdealStatus(status, view) {
  const label = $("upiCountdownLabel");
  if (view === "INITIAL_VIEW") {
    if (label) label.textContent = "剩余有效期：";
    setUpiQrState("active");
    return;
  }
  if (view === "WAIT_FOR_CONFIRMATION_VIEW") {
    if (label) label.textContent = "等待确认，剩余有效期：";
    setStatus("等待 iDEAL 银行确认");
    setUpiQrState("active");
    return;
  }
  if (status !== "paid" && status !== "expired" && status !== "failed") return;
  clearUpiCountdownTimer();
  stopUpiStatusPolling();
  setUpiQrState(status);
  if (status === "paid") setStatus("支付成功", "success");
  else if (status === "expired") setStatus("二维码已过期", "error");
  else if (status === "failed") setStatus("支付失败", "error");
}

function startUpiStatusPolling(ctx) {
  stopUpiStatusPolling();
  if (!ctx || !ctx.intentId || !ctx.intentSecret) return; // 没有 intent 引用无法轮询真实状态
  let inFlight = false;
  const poll = async () => {
    if (document.visibilityState !== "visible") return;
    if (inFlight) return;
    inFlight = true;
    try {
      const resp = await fetch("/api/upi-status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          csId: ctx.csId || "",
          stripePublishableKey: ctx.pk || "",
          intentId: ctx.intentId,
          intentSecret: ctx.intentSecret,
          intentKind: ctx.intentKind || "",
          proxy: "",
          proxyPools: ctx.proxyPools || readProxyPools(),
        }),
      });
      if (!resp.ok) {
        if (resp.status === 400 || resp.status === 422) {
          const error = await resp.json().catch(() => ({}));
          stopUpiStatusPolling();
          setStatus(`UPI 状态轮询失败：${error.detail || `HTTP ${resp.status}`}`, "error");
        }
        return;
      }
      const d = await resp.json().catch(() => ({}));
      if (!d) return;
      // 先用 Stripe 返回的真实过期时间校正倒计时，再处理终态
      if (d.expires_at) resyncUpiExpiry(d.expires_at);
      if (d.status) applyUpiStatus(d.status);
    } catch { /* 网络抖动忽略，下一轮继续 */ }
    finally { inFlight = false; }
  };
  poll();
  _upiStatusTimer = setInterval(poll, QR_STATUS_POLL_INTERVAL_MS);
}

function startIdealStatusPolling(ctx) {
  stopUpiStatusPolling();
  if (!ctx || !ctx.statusUrl) return;
  let inFlight = false;
  const poll = async () => {
    if (document.visibilityState !== "visible") return;
    if (inFlight) return;
    inFlight = true;
    try {
      const resp = await fetch("/api/ideal-status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          statusUrl: ctx.statusUrl || "",
          transactionUrl: ctx.transactionUrl || "",
          proxy: "",
          proxyPools: ctx.proxyPools || readProxyPools(),
          mode6ProxyMode: ctx.mode6ProxyMode || readMode6ProxyMode(),
        }),
      });
      if (!resp.ok) return;
      const d = await resp.json().catch(() => ({}));
      if (!d) return;
      applyIdealStatus(d.status || "unknown", d.view || "");
    } catch { /* iDEAL SSE 偶发失败则下轮继续，倒计时负责兜底过期 */ }
    finally { inFlight = false; }
  };
  poll();
  _upiStatusTimer = setInterval(poll, QR_STATUS_POLL_INTERVAL_MS);
}

function setStatus(text, type = "") { statusBar.textContent = text; statusBar.className = "status-bar" + (type ? " " + type : ""); }

// 提示音：success 上扬三连音，error 低沉双音。用 Web Audio 合成，无需音频文件。
function playChime(kind) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const now = ctx.currentTime;
    const isOk = kind === "success";
    const notes = isOk ? [523.25, 659.25, 783.99] : [392.0, 261.63];
    const step = isOk ? 0.12 : 0.2;
    const dur = isOk ? 0.16 : 0.26;
    notes.forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = isOk ? "sine" : "triangle";
      osc.frequency.value = freq;
      const start = now + i * step;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.28, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + dur);
      osc.connect(gain); gain.connect(ctx.destination);
      osc.start(start); osc.stop(start + dur + 0.03);
    });
    setTimeout(() => { try { ctx.close(); } catch (e) {} }, 2000);
  } catch (e) { /* 音频不可用则静默忽略 */ }
}

function shouldPlayChimeForMode(mode) {
  return (mode === 2 && ENABLE_MODE2_CHIME) || (mode === 5 && ENABLE_MODE5_CHIME) || mode === 6 || mode === 7;
}

function showError(detail) { errorContent.textContent = detail; errorDetail.classList.add("show"); }
function hideError() { errorDetail.classList.remove("show"); errorContent.textContent = ""; }

function setLoading(loading) {
  _isGenerating = loading;
  updateGenerateButtonState();
  spinner.classList.toggle("active", loading);
  generateText.textContent = loading ? "提取中..." : "提取链接";
  stopBtn.style.display = loading ? "" : "none";
  if (loading) {
    if (_hideProgressTimer) { clearTimeout(_hideProgressTimer); _hideProgressTimer = null; }
    progressBar.classList.add("active");
    progressText.classList.add("active");
  } else {
    _hideProgressTimer = setTimeout(() => { _hideProgressTimer = null; progressBar.classList.remove("active"); progressText.classList.remove("active"); progressBarFill.style.width = "0%"; progressText.textContent = ""; }, 1500);
  }
}

function setProgress(step, total, desc) {
  if (step > 0) { const pct = Math.round((step / total) * 100); progressBarFill.style.width = pct + "%"; progressText.textContent = `步骤 ${step}/${total}：${desc}`; }
  else { progressBarFill.style.width = "0%"; progressText.textContent = desc; }
}

function base64UrlDecode(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  return decodeURIComponent(atob(padded).split("").map((c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2)).join(""));
}

function findToken(value) {
  if (!value) return "";
  if (typeof value === "string") return value.trim();
  if (Array.isArray(value)) { for (const item of value) { const t = findToken(item); if (t) return t; } }
  if (typeof value === "object") {
    for (const key of ["accessToken", "access_token", "token"]) { if (typeof value[key] === "string" && value[key].trim()) return value[key].trim(); }
    for (const item of Object.values(value)) { const t = findToken(item); if (t) return t; }
  }
  return "";
}

function looksLikePpApproveUrl(value) {
  const text = String(value || "").trim();
  if (!/^https?:\/\//i.test(text)) return false;
  try { const url = new URL(text); const host = url.hostname.toLowerCase(); return (host.endsWith("paypal.com") || host === "paypal.com") && (url.searchParams.has("ba_token") || url.pathname.toLowerCase().includes("/agreements/approve")); }
  catch { return false; }
}

function currentInputKind() { const raw = $("accessToken").value.trim(); if (!raw) return "empty"; return looksLikePpApproveUrl(raw) ? "pp-url" : "access-token"; }
function readPpApprovalUrlInput() { const raw = $("accessToken").value.trim(); return looksLikePpApproveUrl(raw) ? raw : ""; }

function readAccessTokenInput() {
  const raw = $("accessToken").value.trim();
  if (!raw) return "";
  if (looksLikePpApproveUrl(raw)) return "";
  if (raw.startsWith("{") || raw.startsWith("[")) { try { return findToken(JSON.parse(raw)) || raw; } catch { return raw; } }
  return raw;
}

function currentTokenInfoKey(token = readAccessTokenInput()) {
  return [
    token,
    JSON.stringify(readProxyPools()),
    isIdealMode(parseInt(($("extractMode") || {}).value) || 0) ? readMode6ProxyMode() : "",
    $("deviceId") ? $("deviceId").value.trim() : "",
    $("userAgent") ? $("userAgent").value.trim() : "",
  ].join("\n");
}

function setTokenInfoValid(valid, token = readAccessTokenInput()) {
  _tokenInfoValidKey = valid ? currentTokenInfoKey(token) : "";
  updateGenerateButtonState();
}

function isCurrentTokenInfoValid() {
  const token = readAccessTokenInput();
  return Boolean(token && token.includes(".") && _tokenInfoValidKey && currentTokenInfoKey(token) === _tokenInfoValidKey);
}

function isPlusAccountType(value) {
  return String(value || "").trim().toLowerCase() === "plus";
}

function isPaidAccountType(value) {
  return ["plus", "team", "enterprise"].includes(String(value || "").trim().toLowerCase());
}

function isCurrentPlusAccount() {
  return isCurrentTokenInfoValid() && isPlusAccountType(_lastTokenInfoContext && _lastTokenInfoContext.accountType);
}

function isModeBlockedByAccountType(mode) {
  return isCurrentPlusAccount() && (mode === 1 || mode === 2 || mode === 5 || mode === 6 || mode === 7);
}

function isCurrentAccountHasEmail() {
  return Boolean(_lastTokenInfoContext && _lastTokenInfoContext.email);
}

function canGenerateForCurrentInput() {
  const mode = parseInt($("extractMode").value) || 2;
  if (mode === 1) return false;
  const kind = currentInputKind();
  if (kind === "pp-url") return mode === 4;
  if (!proxyPoolsValid()) return false;
  if (!isCurrentTokenInfoValid()) return false;
  if (isModeBlockedByAccountType(mode)) return false;
  if (isCdkMode(mode)) {
    const cdk = String((($("mode5Cdk") || {}).value || "")).trim().toUpperCase();
    if (!cdk || cdk !== _mode5CdkValidCode || _mode5CdkAvailable <= 0) return false;
  }
  // 模式2 要求账号绑定了邮箱
  if (mode === 2 && !isCurrentAccountHasEmail()) return false;
  return true;
}

function updateGenerateButtonState() {
  generateBtn.disabled = _isGenerating || !canGenerateForCurrentInput();
}

function setMode5CdkStatus(text, state = "") {
  const element = $("mode5CdkStatus");
  if (!element) return;
  element.textContent = text;
  element.className = "field-help cdk-status" + (state ? ` is-${state}` : "");
}

function scheduleMode5CdkValidation({ immediate = false } = {}) {
  const input = $("mode5Cdk");
  const code = String((input || {}).value || "").trim().toUpperCase();
  if (_mode5CdkTimer) { clearTimeout(_mode5CdkTimer); _mode5CdkTimer = null; }
  if (_mode5CdkAbort) { _mode5CdkAbort.abort(); _mode5CdkAbort = null; }
  const seq = ++_mode5CdkSeq;
  _mode5CdkValidCode = "";
  _mode5CdkAvailable = 0;
  updateGenerateButtonState();
  if (!code) { setMode5CdkStatus("请输入CDK进行验证"); return; }
  if (!/^M5-(?:[A-Z0-9]{5}-){3}[A-Z0-9]{5}$/.test(code)) {
    setMode5CdkStatus("CDK格式不正确", "invalid");
    return;
  }
  setMode5CdkStatus("正在验证CDK…", "checking");
  const check = async () => {
    const controller = new AbortController();
    _mode5CdkAbort = controller;
    try {
      const response = await fetch("/api/mode5-cdk-status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
        signal: controller.signal,
        body: JSON.stringify({ cdk: code }),
      });
      const data = await response.json().catch(() => ({}));
      if (seq !== _mode5CdkSeq) return;
      if (!response.ok || !data.valid) throw new Error(data.detail || `HTTP ${response.status}`);
      _mode5CdkValidCode = code;
      _mode5CdkAvailable = Number(data.available || 0);
      if (data.unlimited) {
        const dailyLimit = Number(data.daily_limit || 2000);
        const dailySuccesses = Number(data.daily_successes || 0);
        const dailyAvailable = Number(data.available || 0);
        const dailyExhausted = dailyAvailable <= 0;
        setMode5CdkStatus(
          `CDK有效 · 不限总次数 · 今日成功 ${dailySuccesses}/${dailyLimit} · 今日剩余 ${dailyAvailable}${dailyExhausted ? "（已达上限）" : ""}`,
          dailyExhausted ? "invalid" : "valid",
        );
      } else {
        const exhausted = Number(data.remaining || 0) <= 0;
        const available = Number(data.available || 0);
        const pending = Number(data.pending || 0);
        setMode5CdkStatus(
          `CDK有效 · 总次数 ${Number(data.total || 0)} · 剩余次数 ${Number(data.remaining || 0)} · 当前可用 ${available}${pending ? ` · 运行中占用 ${pending}` : ""}${exhausted ? "（已用完）" : ""}`,
          exhausted || available <= 0 ? "invalid" : "valid",
        );
      }
    } catch (error) {
      if (error.name === "AbortError" || seq !== _mode5CdkSeq) return;
      _mode5CdkValidCode = "";
      _mode5CdkAvailable = 0;
      setMode5CdkStatus(`CDK验证失败：${error.message}`, "invalid");
    } finally {
      if (seq === _mode5CdkSeq) {
        _mode5CdkAbort = null;
        updateGenerateButtonState();
      }
    }
  };
  if (immediate) check();
  else _mode5CdkTimer = setTimeout(check, 450);
}

function cancelTokenInfoLookup() {
  if (_tokenInfoTimer) { clearTimeout(_tokenInfoTimer); _tokenInfoTimer = null; }
  if (_tokenInfoAbort) { _tokenInfoAbort.abort(); _tokenInfoAbort = null; }
  _tokenInfoSeq++;
  setTokenInfoValid(false);
  setTokenRefreshVisible(false);
  setTokenRefreshLoading(false);
}

function setTokenRefreshVisible(visible) {
  const btn = $("refreshTokenInfo");
  if (!btn) return;
  btn.hidden = !visible;
}

function setTokenRefreshLoading(loading) {
  const btn = $("refreshTokenInfo");
  if (!btn) return;
  btn.disabled = loading;
  btn.classList.toggle("loading", loading);
}

function tokenInfoBaseText(email, phone = "") {
  if (email) return `关联邮箱：${email}`;
  if (phone) return `手机号：${phone}`;
  return "Token 已识别（未检测到邮箱/手机号）";
}

function fallbackIdentityFromToken(token) {
  try {
    const payload = JSON.parse(base64UrlDecode(token.split(".")[1] || ""));
    const profile = payload["https://api.openai.com/profile"] || {};
    return {
      email: profile.email || payload.email || "",
      phone: profile.phone_number || profile.phoneNumber || profile.phone || payload.phone_number || payload.phoneNumber || payload.phone || "",
    };
  } catch {
    return { email: "", phone: "" };
  }
}

function findEmailInValue(value) {
  if (!value) return "";
  if (typeof value === "string") {
    const match = value.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i);
    return match ? match[0] : "";
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findEmailInValue(item);
      if (found) return found;
    }
    return "";
  }
  if (typeof value === "object") {
    for (const key of ["email", "preferred_email", "user_email"]) {
      const found = findEmailInValue(value[key]);
      if (found) return found;
    }
    for (const key of ["user", "profile", "https://api.openai.com/profile"]) {
      const found = findEmailInValue(value[key]);
      if (found) return found;
    }
    for (const item of Object.values(value)) {
      const found = findEmailInValue(item);
      if (found) return found;
    }
  }
  return "";
}

function findPhoneInValue(value) {
  if (!value) return "";
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findPhoneInValue(item);
      if (found) return found;
    }
    return "";
  }
  if (typeof value === "object") {
    for (const key of ["phone", "phone_number", "phoneNumber", "mobile", "mobile_phone", "mobilePhone", "tel"]) {
      const phone = value[key];
      if (typeof phone === "string" && phone.trim()) return phone.trim();
      if (typeof phone === "number") return String(phone);
    }
    for (const key of ["user", "profile", "https://api.openai.com/profile"]) {
      const found = findPhoneInValue(value[key]);
      if (found) return found;
    }
    for (const item of Object.values(value)) {
      const found = findPhoneInValue(item);
      if (found) return found;
    }
  }
  return "";
}

function fallbackIdentityFromInput(token = readAccessTokenInput()) {
  const tokenIdentity = fallbackIdentityFromToken(token);
  const raw = $("accessToken").value.trim();
  let inputEmail = "";
  let inputPhone = "";
  if (raw.startsWith("{") || raw.startsWith("[")) {
    try {
      const parsed = JSON.parse(raw);
      inputEmail = findEmailInValue(parsed);
      inputPhone = findPhoneInValue(parsed);
    } catch {
      inputEmail = findEmailInValue(raw);
    }
  }
  return {
    email: tokenIdentity.email || inputEmail || "",
    phone: tokenIdentity.phone || inputPhone || "",
  };
}

function renderTokenInfo(data, fallbackEmail, fallbackPhone = "", token = readAccessTokenInput()) {
  const email = data.email || fallbackEmail || "";
  const phone = data.phone || fallbackPhone || "";
  const accountType = data.account_type || "未知";
  const parts = [tokenInfoBaseText(email, phone), `账号类型：${accountType}`];
  if (isPaidAccountType(accountType)) {
    parts.push(`会员到期时间：${data.plan_expires_at_display || "未获取到"}`);
  } else if (data.token_expires_at_display || data.expires_at_display) {
    parts.push(`Access Token 过期时间：${data.token_expires_at_display || data.expires_at_display}`);
  }
  $("tokenMeta").textContent = parts.join(" · ");
  _lastTokenInfoContext = { token, email, phone, accountType };
  setTokenInfoValid(true, token);
  setTokenRefreshVisible(Boolean(email || phone));
}

function scheduleTokenInfoLookup(token, fallbackEmail, fallbackPhone = "", options = {}) {
  if (!token || !token.includes(".")) return;
  if (!firstProxyPoolText()) {
    $("tokenMeta").textContent = "请先填写代理池1；Access Token 信息查询会使用代理池1中的代理";
    setTokenInfoValid(false, token);
    setTokenRefreshVisible(false);
    return;
  }
  if (_tokenInfoTimer) clearTimeout(_tokenInfoTimer);
  const delay = Number.isFinite(options.delay) ? options.delay : 700;
  const hasSimpleIdentity = Boolean(fallbackEmail || fallbackPhone);
  if (!options.keepButton) setTokenRefreshVisible(hasSimpleIdentity);
  setTokenInfoValid(false, token);
  const seq = ++_tokenInfoSeq;
  _tokenInfoTimer = setTimeout(async () => {
    _tokenInfoTimer = null;
    if (_tokenInfoAbort) _tokenInfoAbort.abort();
    _tokenInfoAbort = new AbortController();
    try {
      const resp = await fetch("/api/token-info", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: _tokenInfoAbort.signal,
        body: JSON.stringify({
          accessToken: token,
          proxy: "",
          proxyPools: readProxyPools(),
          mode6ProxyMode: isIdealMode(parseInt(($("extractMode") || {}).value) || 0) ? readMode6ProxyMode() : "",
          device_id: $("deviceId") ? $("deviceId").value.trim() : "",
          user_agent: $("userAgent") ? $("userAgent").value.trim() : "",
        }),
      });
      if (seq !== _tokenInfoSeq) return;
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        $("tokenMeta").textContent = `${tokenInfoBaseText(fallbackEmail, fallbackPhone)} · 账号类型识别失败：${data.detail || `HTTP ${resp.status}`}`;
        setTokenInfoValid(false, token);
        setTokenRefreshVisible(hasSimpleIdentity);
        return;
      }
      renderTokenInfo(data, fallbackEmail, fallbackPhone, token);
    } catch (err) {
      if (err && err.name === "AbortError") return;
      if (seq !== _tokenInfoSeq) return;
      $("tokenMeta").textContent = `${tokenInfoBaseText(fallbackEmail, fallbackPhone)} · 账号类型识别失败：${err instanceof Error ? err.message : String(err)}`;
      setTokenInfoValid(false, token);
      setTokenRefreshVisible(hasSimpleIdentity);
    } finally {
      if (seq === _tokenInfoSeq) _tokenInfoAbort = null;
      if (seq === _tokenInfoSeq) setTokenRefreshLoading(false);
    }
  }, delay);
}

function updateTokenMeta() {
  const kind = currentInputKind();
  const modeSelect = $("extractMode");
  // 仍禁用的模式选项：模式1、模式4（模式2 已启用）
  const setDisabledOptions = () => { for (const opt of modeSelect.options) { opt.disabled = (opt.value === "1" || opt.value === "4"); } };
  if (kind === "pp-url") {
    cancelTokenInfoLookup();
    $("tokenMeta").textContent = "已识别 PayPal approve URL（自动切换模式4）";
    if (modeSelect.value !== "4") { lastNonPpMode = normalizeNonPpMode(modeSelect.value || lastNonPpMode); suppressAutoModeSwitch = true; modeSelect.value = "4"; suppressAutoModeSwitch = false; syncModeUi(); }
    modeSelect.disabled = true;
    return;
  }
  if (kind === "access-token" && modeSelect.value === "4") { suppressAutoModeSwitch = true; modeSelect.value = normalizeNonPpMode(lastNonPpMode); suppressAutoModeSwitch = false; syncModeUi(); }
  if (kind === "access-token") { setDisabledOptions(); }
  const token = readAccessTokenInput();
  if (!token.includes(".")) { cancelTokenInfoLookup(); $("tokenMeta").textContent = ""; if (kind === "access-token") { setDisabledOptions(); } return; }
  try {
    JSON.parse(base64UrlDecode(token.split(".")[1] || ""));
    const { email, phone } = fallbackIdentityFromInput(token);
    if (email) {
      $("tokenMeta").textContent = `${tokenInfoBaseText(email, phone)} · 账号类型识别中...`;
      if (modeSelect.value === "1") { modeSelect.value = "3"; syncModeUi(); }
      setDisabledOptions();
      modeSelect.disabled = false;
      scheduleTokenInfoLookup(token, email, phone);
    } else {
      $("tokenMeta").textContent = `${tokenInfoBaseText("", phone)} · 账号类型识别中...`;
      if (modeSelect.value === "1") { modeSelect.value = "3"; syncModeUi(); }
      setDisabledOptions();
      modeSelect.disabled = false;
      scheduleTokenInfoLookup(token, "", phone);
    }
  } catch {
    cancelTokenInfoLookup();
    $("tokenMeta").textContent = "";
    setDisabledOptions();
    modeSelect.disabled = false;
  }
}

function loadProxy() {
  renderProxyPools();
}
function setLinkEnabled(enabled) { copyBtn.disabled = !enabled; openLink.classList.toggle("disabled", !enabled); openLink.href = enabled ? currentLongUrl : "#"; }

function showPpOtpModal() { $("ppOtpModalInput").value = ""; $("ppOtpModal").classList.add("show"); $("ppOtpModal").setAttribute("aria-hidden", "false"); setTimeout(() => $("ppOtpModalInput").focus(), 0); }
function hidePpOtpModal() { $("ppOtpModal").classList.remove("show"); $("ppOtpModal").setAttribute("aria-hidden", "true"); }

async function generate() {
  const accessToken = readAccessTokenInput();
  const approvalUrl = readPpApprovalUrlInput();
  const initMode = parseInt($("extractMode").value) || 2;
  if (!proxyPoolsValid()) { setStatus("请先填写当前模式需要的代理池", "error"); return; }
  if (initMode !== 4 && !accessToken) { setStatus("请输入 Access Token", "error"); return; }
  if (initMode !== 4 && !isCurrentTokenInfoValid()) { setStatus("请先等待账号信息识别成功", "error"); return; }
  if (initMode !== 4 && isModeBlockedByAccountType(initMode)) { setStatus("Plus 账号不能使用当前提取模式", "error"); return; }
  if (isCdkMode(initMode)) {
    const cdk = $("mode5Cdk").value.trim().toUpperCase();
    if (!cdk) { setStatus("请输入模式5 CDK", "error"); $("mode5Cdk").focus(); return; }
    if (cdk !== _mode5CdkValidCode || _mode5CdkAvailable <= 0) { setStatus("请先等待CDK验证通过并确认剩余次数", "error"); $("mode5Cdk").focus(); return; }
  }
  if (initMode === 2 && !isCurrentAccountHasEmail()) { setStatus("模式2 要求账号已绑定邮箱", "error"); return; }
  if (initMode === 4 && !approvalUrl) { setStatus("请输入 PayPal approve URL", "error"); return; }
  if (initMode === 4 && !$("ppOtp").value.trim() && !$("ppPhoneNumber").value.trim()) { setStatus("请输入手机号：日本本地号码，不要带 +81/81 前缀", "error"); return; }

  const captchaAnswer = $("captchaAnswer").value.trim();
  if (!isCdkMode(initMode)) {
    if (_captchaProvider === "turnstile") {
      if (!_turnstileToken) { setStatus("请先完成人机验证", "error"); return; }
    } else if (!_captchaId || captchaAnswer.length !== 5) {
        setStatus("请输入图片中的 5 位验证码", "error");
        $("captchaAnswer").focus();
        return;
      }
    }

  setLoading(true); setLinkEnabled(false); copyBtn.textContent = "复制链接"; resultCard.classList.remove("show"); hideError(); setStatus("");
  stopUpiCountdown();

  window._ppCaptchaToken = "";
  const currentMode = parseInt($("extractMode").value) || 2;
  const totalSteps = currentMode === 4 ? 7 : (currentMode === 3 ? 4 : 7);
  setProgress(1, totalSteps, "准备中...");

  try {
    _abortController = new AbortController();
    const resp = await fetch("/api/long-link-stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Captcha-Id": isCdkMode(currentMode) ? "" : _captchaId,
        "X-Captcha-Answer": isCdkMode(currentMode) ? "" : captchaAnswer,
        "X-Turnstile-Token": isCdkMode(currentMode) ? "" : _turnstileToken,
      },
      signal: _abortController.signal,
      body: JSON.stringify({
        accessToken, link_type: isUpiMode(currentMode) ? "upi" : (isIdealMode(currentMode) ? "ideal" : (isPixMode(currentMode) ? "pix" : (currentMode === 3 ? "team_codex" : "paypal"))),
        proxy: "", proxyPools: readProxyPools(),
        billing_country: $("billingCountry").value,
        checkout_ui_mode: (isIdealMode(currentMode) || isPixMode(currentMode)) ? "custom" : "hosted", payment_locale: $("paymentLocale").value,
        stripe_publishable_key: $("stripeKey").value.trim(), payment_email: $("paymentEmail").value.trim(),
        device_id: $("deviceId").value.trim(), user_agent: $("userAgent").value.trim(),
        approvalUrl, ppPhoneNumber: $("ppPhoneNumber").value.trim(),
        ppOtp: $("ppOtp").value.trim(), captchaToken: window._ppCaptchaToken || "", mode: currentMode,
        cdk: isCdkMode(currentMode) ? $("mode5Cdk").value.trim().toUpperCase() : "",
        mode6ProxyMode: isIdealMode(currentMode) ? readMode6ProxyMode() : "",
      }),
    });
    if (!isCdkMode(currentMode)) resetCaptcha();
    if (!resp.ok) { const errData = await resp.json().catch(() => ({})); throw new Error(errData.detail || `HTTP ${resp.status}`); }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalResult = null;
    let finalError = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = JSON.parse(line.slice(6));
        if (payload.type === "queued") {
          const pct = payload.memoryRatio ? Math.round(payload.memoryRatio * 100) : 0;
          let reason = "当前全局并发任务已满";
          if (payload.reason === "memory") reason = `服务器内存压力较高${pct ? `（${pct}%）` : ""}`;
          else if (payload.reason === "ip_concurrency") reason = `当前 IP 运行中 ${payload.activeForIp || 0}/${payload.maxForIp || "-"}`;
          setProgress(0, totalSteps, `排队中：前方 ${Math.max(0, (payload.position || 1) - 1)} 个任务，全局运行中 ${payload.active || 0}/${payload.maxActive || "-"}。${reason}`);
          setStatus("任务已进入队列，请保持页面打开", "success");
        }
        else if (payload.type === "started") {
          setProgress(1, totalSteps, "已开始执行...");
          setStatus("任务已开始", "success");
        }
        else if (payload.type === "progress") setProgress(payload.step, payload.total, payload.desc);
        else if (payload.type === "done") finalResult = payload.result;
        else if (payload.type === "error") finalError = payload.detail;
      }
    }
    if (finalError) throw new Error(finalError);
    if (!finalResult) throw new Error("未收到结果");
    const data = finalResult;
    currentLongUrl = data.long_url || "";
    $("longUrl").textContent = currentLongUrl;
    $("stripeRedirectHint").style.display = currentMode === 3 ? "" : "none";
    let summaryAmount = String(data.checkout_amount || '').trim();
    const summaryCurrency = String(data.currency || '').trim();
    if (summaryAmount && summaryCurrency && summaryAmount.toUpperCase().endsWith(` ${summaryCurrency.toUpperCase()}`)) {
      summaryAmount = summaryAmount.slice(0, -(summaryCurrency.length + 1)).trim();
    }
    $("summary").textContent = `${data.cs_id} · ${data.link_type}${summaryAmount ? ' · 金额: ' + summaryAmount : ''}`;
    const extractEl = $("extractStatus");
    if (data.fallback) {
      const waitingOtp = currentMode === 4 && String(data.provider_error || "").includes("OTP sent");
      extractEl.textContent = waitingOtp ? "等待验证码：收到后填写 PP OTP 再提交" : `失败：${data.provider_error || "provider redirect 提取失败"}`;
      extractEl.className = waitingOtp ? "result-value" : "result-value error-value";
      if (waitingOtp) showPpOtpModal();
    } else { extractEl.textContent = "成功"; extractEl.className = "result-value success-value"; }

    // UPI/iDEAL/Pix QR code 显示
    const isUpi = isUpiMode(currentMode);
    const isIdeal = isIdealMode(currentMode);
    const isPix = isPixMode(currentMode);
    const isQrMode = isUpi || isIdeal || isPix;
    const qrRow = $("upiQrRow");
    const qrShell = $("upiQrShell");
    const qrImg = $("upiQrImage");
    const qrFallback = $("upiQrFallback");
    stopUpiCountdown();
    if (isQrMode) {
      qrRow.style.display = "";
      const qrData = data.provider_redirect_url || "";
      if (qrData && qrData.startsWith("data:image")) {
        qrImg.src = qrData;
        qrShell.style.display = "";
        qrFallback.style.display = "none";
        if (isUpi) {
          startUpiCountdown(upiExpiresAtAbsolute(data.upi_expires_at));
          startUpiStatusPolling({
            csId: data.cs_id,
            pk: data.stripe_publishable_key || "",
            intentId: data.upi_intent_id || "",
            intentSecret: data.upi_intent_secret || "",
            intentKind: data.upi_intent_kind || "",
            proxy: "",
            proxyPools: readProxyPools(),
          });
        } else if (isIdeal) {
          startUpiCountdown(idealExpiresAtAbsolute(data.ideal_expires_at));
          startIdealStatusPolling({
            statusUrl: data.ideal_status_url || "",
            transactionUrl: data.ideal_transaction_url || data.long_url || "",
            proxyPools: readProxyPools(),
            mode6ProxyMode: readMode6ProxyMode(),
          });
        } else if (isPix) {
          startUpiCountdown(pixExpiresAtAbsolute(data.pix_expires_at));
        }
      } else {
        qrShell.style.display = "none";
        qrImg.removeAttribute("src");
        if (qrFallback) qrFallback.textContent = isIdeal ? "iDEAL 二维码未提取到，请打开链接地址查看" : (isPix ? "Pix 二维码未提取到，请打开链接地址查看" : "QR code 未提取到，请打开链接地址查看");
        qrFallback.style.display = "";
      }
    } else {
      qrRow.style.display = "none";
      qrShell.style.display = "none";
      qrImg.removeAttribute("src");
      qrFallback.style.display = "none";
    }

    $("providerUrl").textContent = (isQrMode && data.provider_redirect_url && data.provider_redirect_url.startsWith("data:")) ? ((isIdeal || isPix) ? (data.long_url || "(base64 QR image)") : "(base64 QR image)") : (data.provider_redirect_url || "-");
    $("stripeRedirectUrl").textContent = data.stripe_redirect_url || "-";
    $("stripeUrl").textContent = data.stripe_hosted_url || "-";
    resultCard.classList.add("show");
    setLinkEnabled(Boolean(currentLongUrl));
    setProgress(totalSteps, totalSteps, "完成");
    setStatus("提取完成", "success");
    if (shouldPlayChimeForMode(currentMode)) playChime("success");
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (err.name === "AbortError") { setStatus("已停止", "error"); }
    else { setStatus("提取失败", "error"); showError(msg); if (shouldPlayChimeForMode(currentMode)) playChime("error"); }
    currentLongUrl = "";
  } finally {
    _abortController = null;
    setLoading(false);
    if (isCdkMode(currentMode)) scheduleMode5CdkValidation({ immediate: true });
    if (EXTRACTION_STATS_MODES.has(currentMode)) window.setTimeout(loadExtractionStats, 350);
  }
}

async function copyLink() {
  if (!currentLongUrl) return;
  await navigator.clipboard.writeText(currentLongUrl);
  copyBtn.textContent = "已复制 ✓";
  clearTimeout(copyBtn._resetTimer);
  copyBtn._resetTimer = setTimeout(() => { copyBtn.textContent = "复制链接"; }, 1500);
}

function refreshTokenInfo() {
  const token = readAccessTokenInput();
  if (!token || !token.includes(".")) { setTokenRefreshVisible(false); return; }
  const identity = fallbackIdentityFromInput(token);
  const email = (_lastTokenInfoContext && _lastTokenInfoContext.token === token && _lastTokenInfoContext.email) || identity.email || "";
  const phone = (_lastTokenInfoContext && _lastTokenInfoContext.token === token && _lastTokenInfoContext.phone) || identity.phone || "";
  setTokenRefreshVisible(true);
  setTokenRefreshLoading(true);
  $("tokenMeta").textContent = `${tokenInfoBaseText(email, phone)} · 账号类型刷新中...`;
  scheduleTokenInfoLookup(token, email, phone, { delay: 0, keepButton: true });
}

restoreExtractMode();
loadProxy();
initializeExtractionStats();

function syncModeUi() {
  const mode = parseInt($("extractMode").value);
  const extractorTitle = EXTRACTOR_MODE_TITLES[mode] || "PP 链提取器";
  const extractorTitleElement = $("extractorTitle");
  if (extractorTitleElement) extractorTitleElement.textContent = extractorTitle;
  document.title = extractorTitle;
  syncExtractionStatsMode(mode);
  $("billingCountry").disabled = (mode !== 3);
  if (isUpiMode(mode)) { $("billingCountry").value = "IN"; }
  else if (isIdealMode(mode)) { $("billingCountry").value = "NL"; }
  else if (isPixMode(mode)) { $("billingCountry").value = "BR"; }
  else if (mode !== 3) $("billingCountry").value = "US";
  if (isIdealMode(mode) && $("paymentLocale") && $("paymentLocale").value === "en") $("paymentLocale").value = "nl";
  if (isPixMode(mode) && $("paymentLocale")) $("paymentLocale").value = "pt-BR";
  $("ppPanel").style.display = (mode === 4) ? "" : "none";
  const isPp = mode === 4;
  const isUpi = isUpiMode(mode);
  const isIdeal = isIdealMode(mode);
  const isPix = isPixMode(mode);
  const mode5CdkField = $("mode5CdkField");
  if (mode5CdkField) mode5CdkField.hidden = !isCdkMode(mode);
  if (isCdkMode(mode) && $("mode5Cdk") && $("mode5Cdk").value.trim()) scheduleMode5CdkValidation({ immediate: true });
  const captchaBox = $("captchaBox");
  if (captchaBox) captchaBox.hidden = isCdkMode(mode);
  $("accessTokenLabel").textContent = isPp ? "PayPal approve URL" : "Access Token";
  $("accessToken").placeholder = isPp ? "粘贴 PayPal approve URL，例如 https://www.paypal.com/agreements/approve?ba_token=..." : "粘贴 Access Token 或完整 session JSON...";
  $("inputModeHint").textContent = isPp ? "模式4" : (isUpi ? "UPI" : (isIdeal ? "iDEAL" : (isPix ? "Pix" : (mode === 2 ? "PayPal" : ""))));
  if (mode === 1) { setStatus("模式1 暂不可用", "error"); }
  else { if (/^模式1 暂不可用$/.test(statusBar.textContent)) setStatus(""); }
  // JP 资料面板在 JP 流程模式显示
  const jpPanel = $("jpPanel");
  const mainLayout = $("mainLayout");
  if (jpPanel && mainLayout && SHOW_JP_DATA_PANEL) {
    const showJp = isJpFlowMode(mode);
    jpPanel.hidden = !showJp;
    mainLayout.classList.toggle("jp-hidden", !showJp);
  }
  updateGenerateButtonState();
}

$("accessToken").addEventListener("input", updateTokenMeta);
if ($("mode5Cdk")) $("mode5Cdk").addEventListener("input", function () {
  this.value = this.value.toUpperCase().replace(/\s+/g, "");
  scheduleMode5CdkValidation();
});
$("refreshTokenInfo").addEventListener("click", refreshTokenInfo);
if ($("deviceId")) $("deviceId").addEventListener("input", updateTokenMeta);
if ($("userAgent")) $("userAgent").addEventListener("input", updateTokenMeta);
$("extractMode").addEventListener("change", function () { localStorage.setItem(EXTRACT_MODE_STORAGE_KEY, this.value); if (!suppressAutoModeSwitch && this.value !== "4") { lastNonPpMode = normalizeNonPpMode(this.value); } syncModeUi(); renderProxyPools(); });
syncModeUi();
updateTokenMeta();

function submitPpOtpModal() { const otp = $("ppOtpModalInput").value.trim(); if (!otp) { $("ppOtpModalInput").focus(); return; } $("ppOtp").value = otp; hidePpOtpModal(); generate(); }
$("closePpOtpModal").addEventListener("click", hidePpOtpModal);
$("cancelPpOtpModal").addEventListener("click", hidePpOtpModal);
$("submitPpOtpModal").addEventListener("click", submitPpOtpModal);
$("ppOtpModalInput").addEventListener("keydown", (e) => { if (e.key === "Enter") submitPpOtpModal(); if (e.key === "Escape") hidePpOtpModal(); });
$("ppOtpModal").addEventListener("click", (e) => { if (e.target === $("ppOtpModal")) hidePpOtpModal(); });
$("toggleAdvanced").addEventListener("click", function () { this.classList.toggle("open"); $("advancedPanel").classList.toggle("show"); });
generateBtn.addEventListener("click", () => { $("ppOtp").value = ""; generate(); });
$("refreshCaptcha").addEventListener("click", loadCaptcha);
$("captchaImage").addEventListener("click", loadCaptcha);
$("captchaAnswer").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !_isGenerating) generate();
});
initializeCaptcha();
stopBtn.addEventListener("click", () => { if (_abortController) { _abortController.abort(); _abortController = null; } });
window.addEventListener("beforeunload", () => { if (_abortController) _abortController.abort(); });
copyBtn.addEventListener("click", copyLink);

// Theme toggle
function getPreferredTheme() { const saved = localStorage.getItem("theme"); if (saved) return saved; return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"; }
function applyTheme(theme) { document.documentElement.setAttribute("data-theme", theme); localStorage.setItem("theme", theme); }
applyTheme(getPreferredTheme());
$("themeToggle").addEventListener("click", () => { const current = document.documentElement.getAttribute("data-theme"); applyTheme(current === "dark" ? "light" : "dark"); });

// ===== JP Data Generator Logic =====
const JP_LOCATIONS = [
  // 東京都
  { city: 'Chiyoda-ku', cityJa: '千代田区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['100-0001', '100-0002', '100-0004', '100-0005', '100-0011', '100-0013', '101-0021', '101-0032', '101-0051', '102-0073', '102-0082', '102-0093'] },
  { city: 'Minato-ku', cityJa: '港区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['105-0001', '105-0011', '105-0014', '106-0031', '106-0032', '106-0041', '106-0044', '106-0045', '106-0046', '106-0047', '107-0051', '107-0052', '107-0061', '108-0014', '108-0023'] },
  { city: 'Shibuya-ku', cityJa: '渋谷区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['150-0001', '150-0002', '150-0011', '150-0012', '150-0013', '150-0021', '150-0031', '150-0041', '150-0042', '150-0043', '150-0044', '150-0045', '150-0046'] },
  { city: 'Shinjuku-ku', cityJa: '新宿区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['160-0001', '160-0011', '160-0021', '160-0022', '160-0023', '160-0024', '161-0031', '162-0801', '162-0814', '162-0825', '162-0843'] },
  { city: 'Setagaya-ku', cityJa: '世田谷区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['154-0001', '154-0011', '154-0023', '155-0031', '155-0033', '157-0061', '158-0091', '158-0094', '156-0043', '156-0044'] },
  { city: 'Meguro-ku', cityJa: '目黒区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['152-0001', '152-0011', '152-0021', '152-0031', '153-0041', '153-0051', '153-0061', '153-0062', '153-0063'] },
  { city: 'Toshima-ku', cityJa: '豊島区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['170-0001', '170-0011', '170-0013', '170-0021', '170-0031', '171-0021', '171-0022', '171-0031', '171-0041', '171-0051'] },
  { city: 'Nakano-ku', cityJa: '中野区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['164-0001', '164-0011', '164-0012', '164-0013', '164-0014', '165-0021', '165-0022', '165-0023', '165-0024', '165-0025'] },
  { city: 'Suginami-ku', cityJa: '杉並区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['166-0001', '166-0002', '166-0003', '166-0004', '166-0011', '166-0012', '166-0013', '166-0014', '166-0015', '167-0021'] },
  { city: 'Nerima-ku', cityJa: '練馬区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['176-0001', '176-0002', '176-0003', '176-0004', '176-0005', '176-0006', '176-0011', '176-0012', '176-0021', '177-0031'] },
  { city: 'Ota-ku', cityJa: '大田区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['143-0001', '143-0011', '143-0013', '143-0014', '143-0015', '143-0016', '143-0021', '143-0022', '143-0023', '143-0024'] },
  { city: 'Edogawa-ku', cityJa: '江戸川区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['132-0001', '132-0011', '132-0021', '132-0022', '132-0023', '132-0024', '132-0025', '133-0041', '133-0043', '133-0044'] },
  { city: 'Koto-ku', cityJa: '江東区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['135-0001', '135-0002', '135-0003', '135-0004', '135-0011', '135-0016', '135-0021', '135-0022', '135-0023', '135-0024'] },
  { city: 'Taito-ku', cityJa: '台東区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['110-0001', '110-0002', '110-0003', '110-0004', '110-0005', '110-0008', '110-0011', '110-0012', '110-0013', '110-0015'] },
  { city: 'Bunkyo-ku', cityJa: '文京区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['112-0001', '112-0002', '112-0003', '112-0004', '112-0005', '112-0006', '112-0011', '112-0012', '112-0013', '112-0014'] },
  { city: 'Shinagawa-ku', cityJa: '品川区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['140-0001', '140-0002', '140-0003', '140-0004', '140-0005', '140-0011', '140-0013', '140-0014', '140-0015', '141-0001'] },
  { city: 'Itabashi-ku', cityJa: '板橋区', prefecture: 'Tokyo', prefectureJa: '東京都', zips: ['173-0001', '173-0003', '173-0004', '173-0005', '173-0006', '173-0011', '173-0012', '173-0013', '173-0014', '173-0015'] },
  // 神奈川県
  { city: 'Yokohama', cityJa: '横浜市', prefecture: 'Kanagawa', prefectureJa: '神奈川県', zips: ['220-0001', '220-0011', '220-0012', '221-0801', '221-0802', '221-0822', '231-0001', '231-0011', '231-0023', '231-0031', '231-0045', '232-0001'] },
  { city: 'Kawasaki', cityJa: '川崎市', prefecture: 'Kanagawa', prefectureJa: '神奈川県', zips: ['210-0001', '210-0006', '210-0011', '210-0012', '212-0011', '212-0013', '212-0023', '213-0001', '213-0011', '215-0004'] },
  { city: 'Sagamihara', cityJa: '相模原市', prefecture: 'Kanagawa', prefectureJa: '神奈川県', zips: ['252-0001', '252-0011', '252-0131', '252-0141', '252-0206', '252-0211', '252-0221', '252-0231', '252-0241', '252-0302'] },
  { city: 'Fujisawa', cityJa: '藤沢市', prefecture: 'Kanagawa', prefectureJa: '神奈川県', zips: ['251-0001', '251-0011', '251-0014', '251-0015', '251-0021', '251-0023', '251-0024', '251-0025', '251-0026', '251-0028'] },
  // 埼玉県
  { city: 'Saitama', cityJa: 'さいたま市', prefecture: 'Saitama', prefectureJa: '埼玉県', zips: ['330-0001', '330-0011', '330-0021', '330-0031', '330-0041', '330-0051', '336-0001', '336-0011', '336-0021', '336-0031'] },
  { city: 'Kawaguchi', cityJa: '川口市', prefecture: 'Saitama', prefectureJa: '埼玉県', zips: ['332-0001', '332-0003', '332-0006', '332-0011', '332-0012', '332-0014', '332-0015', '332-0016', '332-0017', '332-0021'] },
  { city: 'Kawagoe', cityJa: '川越市', prefecture: 'Saitama', prefectureJa: '埼玉県', zips: ['350-0001', '350-0011', '350-0014', '350-0015', '350-0021', '350-0022', '350-0023', '350-0024', '350-0025', '350-0026'] },
  // 千葉県
  { city: 'Chiba', cityJa: '千葉市', prefecture: 'Chiba', prefectureJa: '千葉県', zips: ['260-0001', '260-0011', '260-0021', '260-0031', '260-0041', '261-0001', '261-0011', '261-0021', '263-0001', '263-0011'] },
  { city: 'Funabashi', cityJa: '船橋市', prefecture: 'Chiba', prefectureJa: '千葉県', zips: ['273-0001', '273-0002', '273-0003', '273-0005', '273-0011', '273-0012', '273-0013', '273-0014', '273-0015', '273-0021'] },
  { city: 'Kashiwa', cityJa: '柏市', prefecture: 'Chiba', prefectureJa: '千葉県', zips: ['277-0001', '277-0003', '277-0004', '277-0005', '277-0011', '277-0014', '277-0021', '277-0022', '277-0024', '277-0025'] },
  // 大阪府
  { city: 'Osaka', cityJa: '大阪市', prefecture: 'Osaka', prefectureJa: '大阪府', zips: ['530-0001', '530-0011', '530-0021', '531-0061', '531-0072', '541-0041', '541-0051', '542-0081', '542-0082', '550-0001', '550-0011', '550-0014'] },
  { city: 'Sakai', cityJa: '堺市', prefecture: 'Osaka', prefectureJa: '大阪府', zips: ['590-0001', '590-0011', '590-0014', '590-0021', '590-0023', '590-0024', '590-0025', '590-0026', '590-0028', '590-0048'] },
  { city: 'Higashiosaka', cityJa: '東大阪市', prefecture: 'Osaka', prefectureJa: '大阪府', zips: ['577-0001', '577-0002', '577-0003', '577-0004', '577-0005', '577-0006', '577-0011', '577-0012', '577-0013', '577-0801'] },
  { city: 'Suita', cityJa: '吹田市', prefecture: 'Osaka', prefectureJa: '大阪府', zips: ['564-0001', '564-0002', '564-0003', '564-0004', '564-0011', '564-0012', '564-0013', '564-0014', '564-0015', '564-0016'] },
  // 京都府
  { city: 'Kyoto', cityJa: '京都市', prefecture: 'Kyoto', prefectureJa: '京都府', zips: ['600-8001', '600-8011', '600-8021', '600-8031', '604-0001', '604-0011', '604-0021', '604-0091', '604-8005', '605-0001', '605-0073', '605-0801'] },
  // 兵庫県
  { city: 'Kobe', cityJa: '神戸市', prefecture: 'Hyogo', prefectureJa: '兵庫県', zips: ['650-0001', '650-0011', '650-0021', '650-0031', '650-0041', '651-0001', '651-0011', '651-0078', '651-0086', '651-0094'] },
  { city: 'Nishinomiya', cityJa: '西宮市', prefecture: 'Hyogo', prefectureJa: '兵庫県', zips: ['662-0001', '662-0011', '662-0021', '662-0822', '662-0832', '662-0834', '662-0911', '662-0912', '662-0921', '662-0927'] },
  { city: 'Amagasaki', cityJa: '尼崎市', prefecture: 'Hyogo', prefectureJa: '兵庫県', zips: ['660-0001', '660-0051', '660-0052', '660-0053', '660-0054', '660-0055', '660-0801', '660-0802', '660-0803', '660-0804'] },
  // 愛知県
  { city: 'Nagoya', cityJa: '名古屋市', prefecture: 'Aichi', prefectureJa: '愛知県', zips: ['450-0001', '450-0002', '450-0011', '450-0021', '450-0031', '451-0011', '451-0021', '451-0031', '460-0001', '460-0008', '460-0011'] },
  { city: 'Toyota', cityJa: '豊田市', prefecture: 'Aichi', prefectureJa: '愛知県', zips: ['471-0001', '471-0011', '471-0013', '471-0014', '471-0015', '471-0016', '471-0017', '471-0021', '471-0023', '471-0024'] },
  // 北海道
  { city: 'Sapporo', cityJa: '札幌市', prefecture: 'Hokkaido', prefectureJa: '北海道', zips: ['060-0001', '060-0011', '060-0021', '060-0031', '060-0041', '060-0051', '060-0061', '060-0807', '060-0808', '064-0801'] },
  { city: 'Asahikawa', cityJa: '旭川市', prefecture: 'Hokkaido', prefectureJa: '北海道', zips: ['070-0001', '070-0011', '070-0021', '070-0022', '070-0023', '070-0024', '070-0025', '070-0026', '070-0027', '070-0028'] },
  // 福岡県
  { city: 'Fukuoka', cityJa: '福岡市', prefecture: 'Fukuoka', prefectureJa: '福岡県', zips: ['810-0001', '810-0011', '810-0021', '810-0031', '810-0041', '812-0011', '812-0013', '812-0018', '812-0023', '813-0001'] },
  { city: 'Kitakyushu', cityJa: '北九州市', prefecture: 'Fukuoka', prefectureJa: '福岡県', zips: ['800-0001', '800-0011', '800-0021', '800-0022', '800-0023', '800-0024', '800-0025', '800-0026', '802-0001', '802-0003'] },
  // 宮城県
  { city: 'Sendai', cityJa: '仙台市', prefecture: 'Miyagi', prefectureJa: '宮城県', zips: ['980-0001', '980-0011', '980-0013', '980-0021', '980-0031', '980-0801', '980-0811', '980-0821', '983-0001', '984-0011'] },
  // 広島県
  { city: 'Hiroshima', cityJa: '広島市', prefecture: 'Hiroshima', prefectureJa: '広島県', zips: ['730-0001', '730-0011', '730-0013', '730-0021', '730-0031', '730-0041', '730-0051', '732-0011', '732-0021', '732-0822'] },
  // 静岡県
  { city: 'Shizuoka', cityJa: '静岡市', prefecture: 'Shizuoka', prefectureJa: '静岡県', zips: ['420-0001', '420-0011', '420-0021', '420-0022', '420-0031', '420-0032', '420-0033', '420-0034', '420-0035', '420-0036'] },
  { city: 'Hamamatsu', cityJa: '浜松市', prefecture: 'Shizuoka', prefectureJa: '静岡県', zips: ['430-0001', '430-0011', '430-0012', '430-0021', '430-0022', '430-0023', '430-0024', '430-0025', '430-0026', '430-0027'] },
  // 新潟県
  { city: 'Niigata', cityJa: '新潟市', prefecture: 'Niigata', prefectureJa: '新潟県', zips: ['950-0001', '950-0011', '950-0012', '950-0021', '950-0022', '950-0031', '950-0032', '950-0065', '950-0071', '950-0072'] },
  // 岡山県
  { city: 'Okayama', cityJa: '岡山市', prefecture: 'Okayama', prefectureJa: '岡山県', zips: ['700-0001', '700-0011', '700-0021', '700-0022', '700-0023', '700-0024', '700-0025', '700-0026', '700-0031', '700-0032'] },
  // 熊本県
  { city: 'Kumamoto', cityJa: '熊本市', prefecture: 'Kumamoto', prefectureJa: '熊本県', zips: ['860-0001', '860-0002', '860-0003', '860-0004', '860-0005', '860-0006', '860-0007', '860-0008', '860-0011', '860-0012'] },
  // 長野県
  { city: 'Nagano', cityJa: '長野市', prefecture: 'Nagano', prefectureJa: '長野県', zips: ['380-0801', '380-0802', '380-0803', '380-0811', '380-0812', '380-0813', '380-0821', '380-0822', '380-0823', '380-0824'] },
  // 石川県
  { city: 'Kanazawa', cityJa: '金沢市', prefecture: 'Ishikawa', prefectureJa: '石川県', zips: ['920-0001', '920-0011', '920-0021', '920-0022', '920-0023', '920-0024', '920-0025', '920-0031', '920-0032', '920-0033'] },
];

const JP_TOWN_NAMES = [
  { en: 'Marunouchi', ja: '丸の内' }, { en: 'Otemachi', ja: '大手町' }, { en: 'Yurakucho', ja: '有楽町' },
  { en: 'Ginza', ja: '銀座' }, { en: 'Roppongi', ja: '六本木' }, { en: 'Akasaka', ja: '赤坂' },
  { en: 'Aoyama', ja: '青山' }, { en: 'Omotesando', ja: '表参道' }, { en: 'Harajuku', ja: '原宿' },
  { en: 'Ebisu', ja: '恵比寿' }, { en: 'Daikanyama', ja: '代官山' }, { en: 'Nakameguro', ja: '中目黒' },
  { en: 'Jiyugaoka', ja: '自由が丘' }, { en: 'Shimokitazawa', ja: '下北沢' },
  { en: 'Yotsuya', ja: '四谷' }, { en: 'Ichigaya', ja: '市ヶ谷' }, { en: 'Iidabashi', ja: '飯田橋' },
  { en: 'Kagurazaka', ja: '神楽坂' }, { en: 'Ikebukuro', ja: '池袋' }, { en: 'Mejiro', ja: '目白' },
  { en: 'Nakano', ja: '中野' }, { en: 'Koenji', ja: '高円寺' }, { en: 'Asagaya', ja: '阿佐ヶ谷' },
  { en: 'Ogikubo', ja: '荻窪' }, { en: 'Kichijoji', ja: '吉祥寺' }, { en: 'Mitaka', ja: '三鷹' },
  { en: 'Sangenjaya', ja: '三軒茶屋' }, { en: 'Gotanda', ja: '五反田' }, { en: 'Osaki', ja: '大崎' },
  { en: 'Tamachi', ja: '田町' }, { en: 'Hamamatsucho', ja: '浜松町' },
  { en: 'Toranomon', ja: '虎ノ門' }, { en: 'Kasumigaseki', ja: '霞が関' }, { en: 'Nagatacho', ja: '永田町' },
  { en: 'Kojimachi', ja: '麹町' }, { en: 'Hirakawacho', ja: '平河町' },
  { en: 'Honmachi', ja: '本町' }, { en: 'Umeda', ja: '梅田' }, { en: 'Namba', ja: '難波' },
  { en: 'Tennoji', ja: '天王寺' }, { en: 'Shinsaibashi', ja: '心斎橋' }, { en: 'Kitashinchi', ja: '北新地' },
  { en: 'Sannomiya', ja: '三宮' }, { en: 'Motomachi', ja: '元町' }, { en: 'Kitano', ja: '北野' },
  { en: 'Karasuma', ja: '烏丸' }, { en: 'Kawaramachi', ja: '河原町' }, { en: 'Kiyamachi', ja: '木屋町' },
  { en: 'Sakae', ja: '栄' }, { en: 'Fushimi', ja: '伏見' }, { en: 'Osu', ja: '大須' },
  { en: 'Kanayama', ja: '金山' }, { en: 'Hakata', ja: '博多' }, { en: 'Tenjin', ja: '天神' },
  { en: 'Daimyo', ja: '大名' }, { en: 'Yakuin', ja: '薬院' },
  { en: 'Susukino', ja: 'すすきの' }, { en: 'Odori', ja: '大通' },
  { en: 'Kotodai', ja: '勾当台' }, { en: 'Aoba', ja: '青葉' },
  { en: 'Takadanobaba', ja: '高田馬場' }, { en: 'Waseda', ja: '早稲田' },
  { en: 'Ochanomizu', ja: 'お茶の水' }, { en: 'Jinbocho', ja: '神保町' },
  { en: 'Kanda', ja: '神田' }, { en: 'Nihonbashi', ja: '日本橋' },
  { en: 'Tsukiji', ja: '築地' }, { en: 'Tsukishima', ja: '月島' },
  { en: 'Toyosu', ja: '豊洲' }, { en: 'Ariake', ja: '有明' }, { en: 'Odaiba', ja: 'お台場' },
  { en: 'Shinbashi', ja: '新橋' }, { en: 'Azabu', ja: '麻布' }, { en: 'Hiroo', ja: '広尾' },
  { en: 'Shirokane', ja: '白金' }, { en: 'Takanawa', ja: '高輪' }, { en: 'Mita', ja: '三田' },
  { en: 'Shibakoen', ja: '芝公園' }, { en: 'Yoyogi', ja: '代々木' }, { en: 'Sendagaya', ja: '千駄ヶ谷' },
  { en: 'Hatagaya', ja: '幡ヶ谷' }, { en: 'Sasazuka', ja: '笹塚' },
  { en: 'Komaba', ja: '駒場' }, { en: 'Todoroki', ja: '等々力' },
  { en: 'Yoga', ja: '用賀' }, { en: 'Futakotamagawa', ja: '二子玉川' },
  { en: 'Okusawa', ja: '奥沢' }, { en: 'Denenchofu', ja: '田園調布' },
  { en: 'Kamata', ja: '蒲田' }, { en: 'Omori', ja: '大森' },
  { en: 'Kinshicho', ja: '錦糸町' }, { en: 'Ryogoku', ja: '両国' },
  { en: 'Asakusa', ja: '浅草' }, { en: 'Ueno', ja: '上野' },
  { en: 'Yanaka', ja: '谷中' }, { en: 'Nezu', ja: '根津' }, { en: 'Sendagi', ja: '千駄木' },
  { en: 'Nishi-Shinjuku', ja: '西新宿' }, { en: 'Akabane', ja: '赤羽' },
  { en: 'Oji', ja: '王子' }, { en: 'Jujo', ja: '十条' }, { en: 'Itabashi', ja: '板橋' },
  { en: 'Shakujii', ja: '石神井' }, { en: 'Oizumi', ja: '大泉' }, { en: 'Hikarigaoka', ja: '光が丘' },
  { en: 'Tachikawa', ja: '立川' }, { en: 'Fuchu', ja: '府中' }, { en: 'Chofu', ja: '調布' },
  { en: 'Machida', ja: '町田' }, { en: 'Hachioji', ja: '八王子' }, { en: 'Musashino', ja: '武蔵野' },
  { en: 'Motoyama', ja: '本山' }, { en: 'Chikusa', ja: '千種' }, { en: 'Imaike', ja: '今池' },
  { en: 'Yagoto', ja: '八事' },
  { en: 'Ibaraki', ja: '茨木' }, { en: 'Takatsuki', ja: '高槻' },
  { en: 'Moriguchi', ja: '守口' }, { en: 'Neyagawa', ja: '寝屋川' }, { en: 'Hirakata', ja: '枚方' },
  { en: 'Abeno', ja: '阿倍野' }, { en: 'Tsuruhashi', ja: '鶴橋' },
  { en: 'Nishinari', ja: '西成' },
];

const JP_LAST_SYLLABLES_1 = ['サ', 'ス', 'タ', 'ナ', 'ハ', 'マ', 'ヤ', 'カ', 'ワ', 'イ', 'オ', 'コ', 'モ', 'ア', 'フ', 'ニ', 'エ', 'ミ', 'ク', 'シ'];
const JP_LAST_SYLLABLES_2 = ['トウ', 'ズキ', 'カハシ', 'ナカ', 'タナベ', 'マモト', 'カムラ', 'バヤシ', 'ツモト', 'ノウエ', 'ムラ', 'ヤシ', 'ミズ', 'マザキ', 'リ', 'ベ', 'ケダ', 'シモト', 'マシタ', 'シカワ', 'カジマ', 'エダ', 'ジタ', 'ガワ', 'カダ', 'セガワ', 'ラカミ', 'ンドウ', 'シイ', 'カモト', 'オキ', 'ジイ', 'シムラ', 'クダ', 'ウラ', 'ジワラ', 'ツダ', 'カガワ', 'カノ', 'ハラ', 'ノ', 'ダ', 'ワタ', 'グチ', 'ヤマ', 'タ', 'モト', 'ウチ', 'サワ', 'キ'];
const JP_FIRST_PARTS_A = ['ヒロ', 'タカ', 'アキ', 'ケン', 'ダイ', 'ユウ', 'ショウ', 'リョウ', 'ナオ', 'タツ', 'ハル', 'カイ', 'イツ', 'ジュン', 'マサ', 'コウ', 'タク', 'レン', 'ツバ', 'ソウ', 'シン', 'ゲン', 'トモ', 'ノブ', 'ヨシ', 'カズ', 'テツ', 'ミツ', 'ヒデ', 'キヨ'];
const JP_FIRST_PARTS_B = ['シ', 'キ', 'ラ', 'ジ', 'キ', 'タ', 'ト', 'ヤ', 'タロウ', 'スケ', 'イチ', 'ヘイ', 'マサ', 'ノリ', 'ヒコ', 'オ', 'ヤ', '', 'サ', 'タ', 'ジ', 'キ', 'ヤ', 'ヒロ', 'ノリ', 'ヤ', 'ヤ', 'ル', 'アキ', 'シ'];
const JP_FIRST_PARTS_F_A = ['ユ', 'ヒ', 'メ', 'ア', 'サ', 'ハ', 'アオ', 'カ', 'ミ', 'アキ', 'ナオ', 'マリ', 'ケイ', 'アヤ', 'ミサ', 'リ', 'ハル', 'ナナ', 'カナ', 'アス', 'ホノ', 'メグ', 'エリ', 'チ', 'マ', 'レ', 'ノ', 'ミ', 'サ', 'ヒ'];
const JP_FIRST_PARTS_F_B = ['イ', 'ナ', 'イ', 'イ', 'クラ', 'ナ', 'イ', 'ナ', 'オ', 'コ', 'ミ', 'コ', 'コ', 'カ', 'キ', 'ナ', 'カ', 'ミ', 'コ', 'カ', 'カ', 'ミ', 'カ', 'ヒロ', 'ドカ', 'イ', 'ゾミ', 'ユキ', 'ヤカ', 'マリ'];

const JP_CARD_BINS = [
  { bin: '414709', length: 16, brand: 'Visa Debit', issuer: 'SMBC' },
];

const JP_EMAIL_DOMAINS = ['gmail.com', 'yahoo.co.jp', 'hotmail.com', 'outlook.jp', 'icloud.com', 'me.com', 'live.jp'];

const JP_LAST_NAMES_ROMAJI = [
  'sato', 'suzuki', 'takahashi', 'tanaka', 'watanabe', 'ito', 'yamamoto', 'nakamura', 'kobayashi', 'kato',
  'yoshida', 'yamada', 'sasaki', 'yamaguchi', 'matsumoto', 'inoue', 'kimura', 'hayashi', 'shimizu',
  'yamazaki', 'mori', 'abe', 'ikeda', 'hashimoto', 'yamashita', 'ishikawa', 'nakajima', 'maeda', 'fujita',
  'ogawa', 'goto', 'okada', 'hasegawa', 'murakami', 'kondo', 'ishii', 'sakamoto', 'endo', 'aoki',
  'fujii', 'nishimura', 'fukuda', 'ota', 'miura', 'fujiwara', 'okamoto', 'matsuda', 'nakagawa', 'nakano'
];

const JP_FIRST_NAMES_ROMAJI = [
  'hiroshi', 'takashi', 'akira', 'kenji', 'daiki', 'yuki', 'sho', 'ryo', 'kenta', 'naoki',
  'tatsuya', 'shota', 'takeshi', 'haruto', 'sora', 'hayato', 'kaito', 'yuto', 'riku', 'itsuki',
  'ren', 'tsubasa', 'daisuke', 'junichi', 'masaki', 'kohei', 'ryota', 'takuya', 'yusuke', 'takahiro',
  'yui', 'hina', 'mei', 'ai', 'yuna', 'sakura', 'hana', 'aoi', 'kana', 'mio',
  'akiko', 'yumi', 'naomi', 'mariko', 'keiko', 'ayaka', 'misaki', 'saki', 'rina', 'yuka',
  'haruka', 'nanami', 'riko', 'kanako', 'asuka', 'mayu', 'honoka', 'megumi', 'erika'
];

const BR_LAST_NAMES = [
  'Silva', 'Santos', 'Oliveira', 'Souza', 'Rodrigues', 'Ferreira', 'Almeida', 'Pereira', 'Lima', 'Gomes',
  'Costa', 'Ribeiro', 'Martins', 'Carvalho', 'Rocha', 'Barbosa', 'Melo', 'Cardoso', 'Teixeira', 'Correia',
  'Moura', 'Cunha', 'Dias', 'Nunes', 'Moreira', 'Vieira', 'Monteiro', 'Castro', 'Araujo', 'Campos',
  'Freitas', 'Pinto', 'Mendes', 'Cavalcanti', 'Nascimento', 'Batista', 'Andrade', 'Reis', 'Duarte', 'Machado',
  'Farias', 'Borges', 'Miranda', 'Fonseca', 'Ramos', 'Neves', 'Tavares', 'Peixoto', 'Siqueira', 'Moraes'
];

const BR_FIRST_NAMES = [
  'Lucas', 'Miguel', 'Arthur', 'Gabriel', 'Pedro', 'Matheus', 'Rafael', 'Bruno', 'Felipe', 'Gustavo',
  'Diego', 'Caio', 'Andre', 'Thiago', 'Leonardo', 'Eduardo', 'Henrique', 'Vinicius', 'Marcos', 'Daniel',
  'Ana', 'Maria', 'Julia', 'Laura', 'Mariana', 'Beatriz', 'Camila', 'Leticia', 'Larissa', 'Amanda',
  'Fernanda', 'Carolina', 'Isabela', 'Renata', 'Aline', 'Patricia', 'Bianca', 'Bruna', 'Clara', 'Luana',
  'Sofia', 'Helena', 'Manuela', 'Valentina', 'Yasmin', 'Alice', 'Livia', 'Lorena', 'Vitoria', 'Nina'
];

const BR_LOCATIONS = [
  { state: 'SP', city: 'Sao Paulo', ceps: ['01001-000', '01310-100', '01415-001', '04094-050', '04543-011', '05010-000'] },
  { state: 'RJ', city: 'Rio de Janeiro', ceps: ['20040-020', '22010-000', '22250-040', '22410-002', '22640-102', '23050-000'] },
  { state: 'MG', city: 'Belo Horizonte', ceps: ['30130-010', '30140-071', '30310-009', '30421-169', '30640-070'] },
  { state: 'BA', city: 'Salvador', ceps: ['40020-000', '40140-110', '40210-630', '41820-020', '41940-040'] },
  { state: 'PR', city: 'Curitiba', ceps: ['80010-010', '80230-010', '80420-090', '80530-000', '81200-100'] },
  { state: 'RS', city: 'Porto Alegre', ceps: ['90010-150', '90110-001', '90430-001', '90560-002', '91340-000'] },
  { state: 'PE', city: 'Recife', ceps: ['50010-000', '51020-000', '52011-000', '52050-000', '51030-000'] },
  { state: 'CE', city: 'Fortaleza', ceps: ['60025-060', '60160-230', '60325-000', '60410-440', '60811-341'] },
  { state: 'DF', city: 'Brasilia', ceps: ['70040-010', '70297-400', '70390-025', '70770-522', '71919-540'] },
  { state: 'SC', city: 'Florianopolis', ceps: ['88010-400', '88015-201', '88020-300', '88034-000', '88062-000'] },
  { state: 'GO', city: 'Goiania', ceps: ['74003-010', '74110-010', '74210-010', '74605-010', '74810-100'] },
  { state: 'PA', city: 'Belem', ceps: ['66010-000', '66015-160', '66035-170', '66050-000', '66110-000'] },
  { state: 'AM', city: 'Manaus', ceps: ['69005-040', '69010-000', '69020-010', '69050-001', '69058-795'] },
  { state: 'ES', city: 'Vitoria', ceps: ['29010-120', '29015-120', '29050-335', '29055-450', '29060-270'] },
  { state: 'MT', city: 'Cuiaba', ceps: ['78005-370', '78010-000', '78020-400', '78048-000', '78060-900'] },
  { state: 'MS', city: 'Campo Grande', ceps: ['79002-071', '79004-000', '79010-040', '79020-210', '79040-450'] },
  { state: 'RN', city: 'Natal', ceps: ['59010-000', '59020-100', '59030-200', '59064-100', '59090-000'] },
  { state: 'PB', city: 'Joao Pessoa', ceps: ['58010-000', '58013-000', '58030-001', '58045-010', '58051-900'] },
  { state: 'AL', city: 'Maceio', ceps: ['57020-000', '57035-000', '57036-000', '57046-000', '57055-000'] },
  { state: 'SE', city: 'Aracaju', ceps: ['49010-000', '49015-000', '49020-000', '49035-000', '49050-000'] },
  { state: 'SP', city: 'Campinas', ceps: ['13010-001', '13015-000', '13020-060', '13024-200', '13083-970', '13100-000'] },
  { state: 'SP', city: 'Santos', ceps: ['11010-150', '11013-001', '11015-200', '11025-001', '11045-400', '11060-001'] },
  { state: 'RJ', city: 'Niteroi', ceps: ['24020-125', '24030-060', '24210-200', '24220-900', '24340-005', '24350-010'] },
  { state: 'MG', city: 'Uberlandia', ceps: ['38400-100', '38400-170', '38405-202', '38408-100', '38411-186', '38414-064'] },
  { state: 'BA', city: 'Feira de Santana', ceps: ['44001-000', '44002-000', '44020-000', '44050-000', '44075-000', '44088-000'] },
  { state: 'PR', city: 'Londrina', ceps: ['86010-000', '86015-000', '86020-000', '86026-010', '86039-000', '86050-000'] },
  { state: 'RS', city: 'Caxias do Sul', ceps: ['95010-000', '95020-000', '95032-000', '95040-000', '95052-000', '95070-560'] },
  { state: 'PE', city: 'Olinda', ceps: ['53010-000', '53020-000', '53120-000', '53130-000', '53240-000', '53330-000'] },
  { state: 'CE', city: 'Juazeiro do Norte', ceps: ['63010-000', '63020-000', '63030-000', '63040-000', '63050-000', '63060-000'] },
  { state: 'GO', city: 'Anapolis', ceps: ['75020-010', '75023-040', '75024-030', '75043-010', '75110-390', '75113-570'] },
  { state: 'PA', city: 'Santarem', ceps: ['68005-000', '68010-000', '68015-000', '68020-000', '68035-000', '68040-000'] },
  { state: 'SC', city: 'Joinville', ceps: ['89201-000', '89202-000', '89203-000', '89204-000', '89218-000', '89221-000'] }
];

const BR_STATE_NAMES = {
  SP: 'São Paulo',
  RJ: 'Rio de Janeiro',
  MG: 'Minas Gerais',
  BA: 'Bahia',
  PR: 'Paraná',
  RS: 'Rio Grande do Sul',
  PE: 'Pernambuco',
  CE: 'Ceará',
  DF: 'Distrito Federal',
  SC: 'Santa Catarina',
  GO: 'Goiás',
  PA: 'Pará',
  AM: 'Amazonas',
  ES: 'Espírito Santo',
  MT: 'Mato Grosso',
  MS: 'Mato Grosso do Sul',
  RN: 'Rio Grande do Norte',
  PB: 'Paraíba',
  AL: 'Alagoas',
  SE: 'Sergipe',
};

const BR_STREET_NAMES = [
  'Avenida Paulista', 'Rua Augusta', 'Rua Oscar Freire', 'Rua Vergueiro', 'Rua Haddock Lobo',
  'Avenida Atlantica', 'Rua Voluntarios da Patria', 'Rua Visconde de Piraja', 'Rua das Laranjeiras',
  'Avenida Afonso Pena', 'Rua da Bahia', 'Rua Paraiba', 'Avenida do Contorno', 'Rua Curitiba',
  'Avenida Sete de Setembro', 'Rua Chile', 'Rua das Hortensias', 'Avenida Tancredo Neves',
  'Rua XV de Novembro', 'Avenida Batel', 'Rua Marechal Deodoro', 'Rua Comendador Araujo',
  'Avenida Ipiranga', 'Rua dos Andradas', 'Rua Padre Chagas', 'Avenida Borges de Medeiros',
  'Rua da Aurora', 'Avenida Boa Viagem', 'Rua do Hospicio', 'Rua Benfica',
  'Avenida Beira Mar', 'Rua Barão de Aracati', 'Rua Costa Barros', 'Avenida Dom Luis',
  'SQS 308 Bloco A', 'CLN 102 Bloco B', 'SHIS QI 05 Conjunto 02', 'Avenida das Nacoes',
  'Rua Bocaiuva', 'Rua Felipe Schmidt', 'Avenida Mauro Ramos', 'Rua Esteves Junior',
  'Avenida Goias', 'Rua 10', 'Avenida T-63', 'Rua 9',
  'Avenida Nazare', 'Travessa Padre Eutiquio', 'Rua dos Mundurucus', 'Avenida Almirante Barroso'
];

const BR_STREETS_BY_CITY = {
  'Sao Paulo': ['Avenida Paulista', 'Rua Augusta', 'Rua Oscar Freire', 'Rua Vergueiro', 'Rua Haddock Lobo'],
  'Rio de Janeiro': ['Avenida Atlantica', 'Rua Voluntarios da Patria', 'Rua Visconde de Piraja', 'Rua das Laranjeiras'],
  'Belo Horizonte': ['Avenida Afonso Pena', 'Rua da Bahia', 'Rua Paraiba', 'Avenida do Contorno', 'Rua Curitiba'],
  'Salvador': ['Avenida Sete de Setembro', 'Rua Chile', 'Rua das Hortensias', 'Avenida Tancredo Neves'],
  'Curitiba': ['Rua XV de Novembro', 'Avenida Batel', 'Rua Marechal Deodoro', 'Rua Comendador Araujo'],
  'Porto Alegre': ['Avenida Ipiranga', 'Rua dos Andradas', 'Rua Padre Chagas', 'Avenida Borges de Medeiros'],
  'Recife': ['Rua da Aurora', 'Avenida Boa Viagem', 'Rua do Hospicio', 'Rua Benfica'],
  'Fortaleza': ['Avenida Beira Mar', 'Rua Barão de Aracati', 'Rua Costa Barros', 'Avenida Dom Luis'],
  'Brasilia': ['SQS 308 Bloco A', 'CLN 102 Bloco B', 'SHIS QI 05 Conjunto 02', 'Avenida das Nacoes'],
  'Florianopolis': ['Rua Bocaiuva', 'Rua Felipe Schmidt', 'Avenida Mauro Ramos', 'Rua Esteves Junior'],
  'Goiania': ['Avenida Goias', 'Rua 10', 'Avenida T-63', 'Rua 9'],
  'Belem': ['Avenida Nazare', 'Travessa Padre Eutiquio', 'Rua dos Mundurucus', 'Avenida Almirante Barroso'],
  'Manaus': ['Avenida Eduardo Ribeiro', 'Rua Miranda Leao', 'Avenida Djalma Batista', 'Rua Ramos Ferreira'],
  'Vitoria': ['Avenida Jeronimo Monteiro', 'Rua Sete de Setembro', 'Avenida Nossa Senhora da Penha', 'Rua Aleixo Netto'],
  'Cuiaba': ['Avenida Getulio Vargas', 'Rua Barão de Melgaço', 'Avenida Historiador Rubens de Mendonça', 'Rua 24 de Outubro'],
  'Campo Grande': ['Avenida Afonso Pena', 'Rua 14 de Julho', 'Rua Dom Aquino', 'Avenida Mato Grosso'],
  'Natal': ['Avenida Prudente de Morais', 'Rua Mossoro', 'Avenida Hermes da Fonseca', 'Rua Potengi'],
  'Joao Pessoa': ['Avenida Epitacio Pessoa', 'Rua Duque de Caxias', 'Avenida Almirante Tamandare', 'Rua das Trincheiras'],
  'Maceio': ['Avenida Fernandes Lima', 'Rua do Comercio', 'Avenida Doutor Antonio Gouveia', 'Rua Barao de Maceio'],
  'Aracaju': ['Avenida Beira Mar', 'Rua Itabaiana', 'Avenida Ivo do Prado', 'Rua Laranjeiras'],
  'Campinas': ['Avenida Francisco Glicerio', 'Rua Conceicao', 'Avenida Orosimbo Maia', 'Rua Barreto Leme', 'Avenida Jose de Souza Campos'],
  'Santos': ['Avenida Conselheiro Nebias', 'Avenida Ana Costa', 'Rua XV de Novembro', 'Avenida Washington Luis', 'Rua Tolentino Filgueiras'],
  'Niteroi': ['Rua da Conceicao', 'Avenida Amaral Peixoto', 'Rua Gavio Peixoto', 'Avenida Roberto Silveira', 'Rua Miguel de Frias'],
  'Uberlandia': ['Avenida Afonso Pena', 'Rua Olegario Maciel', 'Avenida Joao Naves de Avila', 'Rua Duque de Caxias', 'Avenida Rondon Pacheco'],
  'Feira de Santana': ['Avenida Getulio Vargas', 'Rua Conselheiro Franco', 'Avenida Senhor dos Passos', 'Rua Marechal Deodoro', 'Avenida Maria Quiteria'],
  'Londrina': ['Avenida Higienopolis', 'Rua Sergipe', 'Avenida Juscelino Kubitschek', 'Rua Pio XII', 'Avenida Madre Leonia Milito'],
  'Caxias do Sul': ['Avenida Julio de Castilhos', 'Rua Sinimbu', 'Rua Feijo Junior', 'Avenida Rio Branco', 'Rua Os Dezoito do Forte'],
  'Olinda': ['Avenida Presidente Kennedy', 'Rua do Sol', 'Avenida Getulio Vargas', 'Rua Prudente de Morais', 'Avenida Carlos de Lima Cavalcanti'],
  'Juazeiro do Norte': ['Rua Sao Pedro', 'Avenida Padre Cicero', 'Rua Santa Luzia', 'Avenida Castelo Branco', 'Rua Sao Francisco'],
  'Anapolis': ['Avenida Brasil', 'Rua Engenheiro Portela', 'Avenida Goias', 'Rua Manoel DAbadia', 'Avenida Universitaria'],
  'Santarem': ['Avenida Rui Barbosa', 'Travessa dos Martires', 'Avenida Mendonca Furtado', 'Rua Galdino Veloso', 'Avenida Borges Leal'],
  'Joinville': ['Rua XV de Novembro', 'Rua Blumenau', 'Avenida Getulio Vargas', 'Rua do Principe', 'Rua Otto Boehm'],
};

const BR_CARD_BINS = [
  { bin: '414709', length: 16, brand: 'Visa Debit' },
  { bin: '516292', length: 16, brand: 'Mastercard Debit' },
];

const BR_EMAIL_DOMAINS = ['gmail.com', 'hotmail.com', 'outlook.com', 'yahoo.com.br', 'icloud.com', 'uol.com.br', 'bol.com.br'];
const US_FIRST_NAMES = ['James', 'John', 'Robert', 'Michael', 'David', 'William', 'Daniel', 'Matthew', 'Joseph', 'Andrew', 'Emily', 'Olivia', 'Emma', 'Sophia', 'Ava', 'Mia', 'Charlotte', 'Amelia'];
const US_LAST_NAMES = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Miller', 'Davis', 'Wilson', 'Moore', 'Taylor', 'Anderson', 'Thomas', 'Jackson', 'White', 'Harris'];
const US_LOCATIONS = [
  { city: 'Los Angeles', state: 'CA', zips: ['90026', '90028', '90036', '90046'], streets: ['Sunset Boulevard', 'Wilshire Boulevard', 'Melrose Avenue', 'North Western Avenue', 'Beverly Boulevard'] },
  { city: 'San Francisco', state: 'CA', zips: ['94102', '94103', '94109', '94117'], streets: ['Market Street', 'Geary Boulevard', 'Van Ness Avenue', 'California Street', 'Mission Street'] },
  { city: 'New York', state: 'NY', zips: ['10001', '10003', '10011', '10019'], streets: ['West 34th Street', 'Madison Avenue', 'Lexington Avenue', 'Broadway', 'West 23rd Street'] },
  { city: 'Brooklyn', state: 'NY', zips: ['11201', '11211', '11215', '11222'], streets: ['Atlantic Avenue', 'Bedford Avenue', 'Flatbush Avenue', 'Court Street', 'Nostrand Avenue'] },
  { city: 'Chicago', state: 'IL', zips: ['60601', '60605', '60611', '60614'], streets: ['North Michigan Avenue', 'West Madison Street', 'South State Street', 'North Clark Street', 'West Belmont Avenue'] },
  { city: 'Houston', state: 'TX', zips: ['77002', '77006', '77019', '77027'], streets: ['Main Street', 'Westheimer Road', 'Louisiana Street', 'Kirby Drive', 'Richmond Avenue'] },
  { city: 'Dallas', state: 'TX', zips: ['75201', '75204', '75219', '75225'], streets: ['McKinney Avenue', 'Main Street', 'Oak Lawn Avenue', 'Preston Road', 'Cedar Springs Road'] },
  { city: 'Austin', state: 'TX', zips: ['78701', '78703', '78704', '78705'], streets: ['Congress Avenue', 'South Lamar Boulevard', 'Guadalupe Street', 'West 6th Street', 'Barton Springs Road'] },
  { city: 'Phoenix', state: 'AZ', zips: ['85004', '85006', '85012', '85016'], streets: ['North Central Avenue', 'East Van Buren Street', 'West Washington Street', 'East Camelback Road', 'North 7th Street'] },
  { city: 'Seattle', state: 'WA', zips: ['98101', '98102', '98109', '98121'], streets: ['Pike Street', '1st Avenue', 'Pine Street', 'Queen Anne Avenue North', 'Westlake Avenue'] },
  { city: 'Miami', state: 'FL', zips: ['33130', '33131', '33133', '33137'], streets: ['Brickell Avenue', 'South Miami Avenue', 'Biscayne Boulevard', 'Coral Way', 'North Miami Avenue'] },
  { city: 'Orlando', state: 'FL', zips: ['32801', '32803', '32806', '32819'], streets: ['East Colonial Drive', 'Orange Avenue', 'South Street', 'International Drive', 'Mills Avenue'] },
  { city: 'Boston', state: 'MA', zips: ['02108', '02111', '02116', '02118'], streets: ['Beacon Street', 'Tremont Street', 'Boylston Street', 'Commonwealth Avenue', 'Newbury Street'] },
  { city: 'Denver', state: 'CO', zips: ['80202', '80203', '80205', '80206'], streets: ['Colfax Avenue', 'Broadway', '17th Street', 'Speer Boulevard', 'East 6th Avenue'] },
  { city: 'Portland', state: 'OR', zips: ['97205', '97209', '97210', '97214'], streets: ['West Burnside Street', 'Northwest 23rd Avenue', 'Southeast Hawthorne Boulevard', 'Northeast Alberta Street', 'Southwest Broadway'] },
  { city: 'Atlanta', state: 'GA', zips: ['30303', '30305', '30308', '30309'], streets: ['Peachtree Street', 'Piedmont Avenue', 'North Avenue', 'West Paces Ferry Road', 'Juniper Street'] },
  { city: 'Philadelphia', state: 'PA', zips: ['19103', '19106', '19107', '19130'], streets: ['Market Street', 'Chestnut Street', 'Walnut Street', 'South Broad Street', 'Spring Garden Street'] },
];
const US_EMAIL_DOMAINS = ['gmail.com', 'outlook.com', 'yahoo.com', 'icloud.com', 'hotmail.com'];
const GB_FIRST_NAMES = ['Oliver', 'George', 'Harry', 'Jack', 'Charlie', 'Thomas', 'Emily', 'Olivia', 'Amelia', 'Sophie', 'Grace', 'Charlotte'];
const GB_LAST_NAMES = ['Smith', 'Jones', 'Taylor', 'Brown', 'Williams', 'Wilson', 'Johnson', 'Davies', 'Robinson', 'Wright', 'Thompson', 'Evans'];
const GB_LOCATIONS = [
  { city: 'London', state: 'England', zips: ['NW1 6XE', 'W1D 1BS', 'SW3 4UD', 'EC1A 1BB'], streets: ['Baker Street', 'Oxford Street', "King's Road", 'Fleet Street'] },
  { city: 'Manchester', state: 'England', zips: ['M3 2BW', 'M1 7ED', 'M1 3BE'], streets: ['Deansgate', 'Oxford Road', 'Portland Street'] },
  { city: 'Birmingham', state: 'England', zips: ['B2 4QA', 'B1 2HF', 'B4 6TB'], streets: ['New Street', 'Broad Street', 'Corporation Street'] },
  { city: 'Edinburgh', state: 'Scotland', zips: ['EH2 2ER', 'EH2 2PF', 'EH1 1SG'], streets: ['Princes Street', 'George Street', 'Royal Mile'] },
  { city: 'Cardiff', state: 'Wales', zips: ['CF10 1EP', 'CF10 2HE', 'CF11 9HB'], streets: ['Queen Street', 'Westgate Street', 'Cathedral Road'] },
];
const GB_EMAIL_DOMAINS = ['gmail.com', 'outlook.com', 'hotmail.co.uk', 'icloud.com', 'yahoo.co.uk'];
const PROFILE_COUNTRY_STORAGE_KEY = 'profile_generator_country';

const PROFILE_COUNTRY_LABELS = {
  JP: {
    title: '日本资料',
    badge: 'JP',
    generate: '🎲 一键生成日本资料',
    labels: {
      jpEmail: '邮箱',
      jpLastNameKana: '姓 (片假名)',
      jpFirstNameKana: '名 (片假名)',
      jpZip: '邮编',
      jpHouseNumber: '号',
      jpState: '都道府県',
      jpCity: '市区町村',
      jpStreet: '街道地址',
      jpFullAddress: '完整地址',
      jpCardNumber: '卡号',
      jpCardExp: '有效期',
      jpBirthday: '出生日期',
      jpPassword: '密码',
    },
    copyRows: [
      ['邮箱', 'email'], ['姓 (片假名)', 'lastName'], ['名 (片假名)', 'firstName'],
      ['邮编', 'zip'], ['号', 'houseNumber'], ['都道府県', 'state'], ['市区町村', 'city'],
      ['街道地址', 'street'], ['完整地址', 'fullAddress'], ['卡号', 'cardNumber'],
      ['有效期', 'expDate'], ['CVV', 'cvv'], ['出生日期', 'birthday'], ['密码', 'password'],
    ],
    hiddenFields: ['cpf', 'cardType', 'houseNumber'],
  },
  BR: {
    title: '巴西资料',
    badge: 'BR',
    generate: '🎲 一键生成巴西资料',
    labels: {
      jpEmail: '邮箱',
      jpLastNameKana: '姓',
      jpFirstNameKana: '名',
      jpZip: 'CEP',
      jpHouseNumber: 'N°',
      jpState: '州',
      jpCity: '城市',
      jpStreet: '街道地址',
      jpFullAddress: '完整地址',
      jpCardNumber: '卡号 (Débito)',
      jpCardExp: '有效期',
      jpBirthday: '出生日期',
      jpCpf: 'CPF',
      jpPassword: '密码',
    },
    copyRows: [
      ['邮箱', 'email'], ['姓', 'lastName'], ['名', 'firstName'],
      ['CEP', 'zip'], ['N°', 'houseNumber'], ['州', 'state'], ['城市', 'city'],
      ['街道地址', 'street'], ['完整地址', 'fullAddress'], ['卡号 (Débito)', 'cardNumber'],
      ['有效期', 'expDate'], ['CVV', 'cvv'], ['出生日期', 'birthday'], ['CPF', 'cpf'], ['密码', 'password'],
    ],
    hiddenFields: ['cardType'],
  },
  US: {
    title: '美国资料',
    badge: 'US',
    generate: '🎲 一键生成美国资料',
    labels: {
      jpEmail: '邮箱', jpLastNameKana: '姓', jpFirstNameKana: '名', jpZip: '邮编',
      jpHouseNumber: '门牌号', jpState: '州', jpCity: '城市', jpStreet: '街道地址',
      jpFullAddress: '完整地址', jpCardNumber: '卡号', jpCardExp: '有效期',
      jpBirthday: '出生日期', jpPassword: '密码',
    },
    copyRows: [
      ['邮箱', 'email'], ['姓', 'lastName'], ['名', 'firstName'], ['邮编', 'zip'],
      ['门牌号', 'houseNumber'], ['州', 'state'], ['城市', 'city'], ['街道地址', 'street'],
      ['完整地址', 'fullAddress'], ['卡号', 'cardNumber'], ['有效期', 'expDate'],
      ['CVV', 'cvv'], ['出生日期', 'birthday'], ['密码', 'password'],
    ],
    hiddenFields: ['cpf', 'cardType'],
  },
  GB: {
    title: '英国资料',
    badge: 'GB',
    generate: '🎲 一键生成英国资料',
    labels: {
      jpEmail: '邮箱', jpLastNameKana: '姓', jpFirstNameKana: '名', jpZip: '邮编',
      jpHouseNumber: '门牌号', jpState: '地区', jpCity: '城市', jpStreet: '街道地址',
      jpFullAddress: '完整地址', jpCardNumber: '卡号', jpCardExp: '有效期',
      jpBirthday: '出生日期', jpPassword: '密码',
    },
    copyRows: [
      ['邮箱', 'email'], ['姓', 'lastName'], ['名', 'firstName'], ['邮编', 'zip'],
      ['门牌号', 'houseNumber'], ['地区', 'state'], ['城市', 'city'], ['街道地址', 'street'],
      ['完整地址', 'fullAddress'], ['卡号', 'cardNumber'], ['有效期', 'expDate'],
      ['CVV', 'cvv'], ['出生日期', 'birthday'], ['密码', 'password'],
    ],
    hiddenFields: ['cpf', 'cardType'],
  },
};

function _jpPick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function _jpInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }

function _jpLuhnCheck(numStr) {
  let sum = 0, alt = true;
  for (let i = numStr.length - 1; i >= 0; i--) {
    let n = parseInt(numStr[i], 10);
    if (alt) { n *= 2; if (n > 9) n -= 9; }
    sum += n; alt = !alt;
  }
  return sum % 10 === 0 ? 0 : 10 - (sum % 10);
}

function generateProfilePassword() {
  const letters = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';
  const required = '0123456789!@#$%&*';
  const all = letters + required;
  const length = _jpInt(8, 20);
  const chars = [required[_jpInt(0, required.length - 1)]];
  while (chars.length < length) chars.push(all[_jpInt(0, all.length - 1)]);
  for (let i = chars.length - 1; i > 0; i--) {
    const j = _jpInt(0, i);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join('');
}

function generateJpData() {
  const lastName = _jpPick(JP_LAST_SYLLABLES_1) + _jpPick(JP_LAST_SYLLABLES_2);
  let firstName;
  if (Math.random() < 0.5) { const idx = _jpInt(0, JP_FIRST_PARTS_A.length - 1); firstName = JP_FIRST_PARTS_A[idx] + JP_FIRST_PARTS_B[idx]; }
  else { const idx = _jpInt(0, JP_FIRST_PARTS_F_A.length - 1); firstName = JP_FIRST_PARTS_F_A[idx] + JP_FIRST_PARTS_F_B[idx]; }

  const year = _jpInt(1970, 2000);
  const month = _jpInt(1, 12);
  const daysInMonth = new Date(year, month, 0).getDate();
  const day = _jpInt(1, daysInMonth);
  const birthday = `${year}/${String(month).padStart(2, '0')}/${String(day).padStart(2, '0')}`;

  const loc = _jpPick(JP_LOCATIONS);
  const zip = _jpPick(loc.zips);
  const town = _jpPick(JP_TOWN_NAMES);
  const chome = _jpInt(1, 9);
  const banchi = _jpInt(1, 32);
  const go = _jpInt(1, 28);
  const street = `${town.ja} ${chome}-${banchi}-${go}`;
  const fullAddress = `〒${zip} ${loc.prefectureJa} ${loc.cityJa} ${town.ja}${chome}-${banchi}-${go}`;
  const houseNumber = String(go);

  const chosen = _jpPick(JP_CARD_BINS);
  const middleLen = chosen.length - chosen.bin.length - 1;
  let middle = '';
  for (let i = 0; i < middleLen; i++) middle += Math.floor(Math.random() * 10).toString();
  const partial = chosen.bin + middle;
  const check = _jpLuhnCheck(partial);
  const cardNumber = partial + String(check);
  const formattedCard = cardNumber.replace(/(.{4})/g, '$1 ').trim();

  const now = new Date();
  const expYear = now.getFullYear() + _jpInt(2, 5);
  const expMonth = _jpInt(1, 12);
  const expDate = `${String(expMonth).padStart(2, '0')}/${String(expYear).slice(-2)}`;
  const cvv = String(_jpInt(100, 999));

  const lastRomaji = _jpPick(JP_LAST_NAMES_ROMAJI);
  const firstRomaji = _jpPick(JP_FIRST_NAMES_ROMAJI);
  const emailNum = _jpInt(100, 9999);
  const emailDomain = _jpPick(JP_EMAIL_DOMAINS);
  const email = `${firstRomaji}${lastRomaji}${emailNum}@${emailDomain}`;

  const password = generateProfilePassword();

  return { country: 'JP', lastName, firstName, birthday, zip, houseNumber, state: loc.prefectureJa, city: loc.cityJa, street, fullAddress, cardType: chosen.brand, cardNumber: formattedCard, expDate, cvv, cpf: '-', email, password };
}

function _brCpfDigits(baseDigits) {
  const firstSum = baseDigits.reduce((sum, digit, idx) => sum + digit * (10 - idx), 0);
  const first = firstSum % 11 < 2 ? 0 : 11 - (firstSum % 11);
  const secondBase = [...baseDigits, first];
  const secondSum = secondBase.reduce((sum, digit, idx) => sum + digit * (11 - idx), 0);
  const second = secondSum % 11 < 2 ? 0 : 11 - (secondSum % 11);
  return [...baseDigits, first, second];
}

function _brGenerateCpf() {
  const base = [];
  for (let i = 0; i < 9; i++) base.push(_jpInt(0, 9));
  if (base.every((digit) => digit === base[0])) return _brGenerateCpf();
  const digits = _brCpfDigits(base).join('');
  return `${digits.slice(0, 3)}.${digits.slice(3, 6)}.${digits.slice(6, 9)}-${digits.slice(9)}`;
}

function _generateDebitCard(bins) {
  const chosen = _jpPick(bins);
  const middleLen = chosen.length - chosen.bin.length - 1;
  let middle = '';
  for (let i = 0; i < middleLen; i++) middle += _jpInt(0, 9).toString();
  const partial = chosen.bin + middle;
  const check = _jpLuhnCheck(partial);
  const cardNumber = partial + String(check);
  return { cardType: chosen.brand, cardNumber: cardNumber.replace(/(.{4})/g, '$1 ').trim() };
}

function generateBrData() {
  const lastName = _jpPick(BR_LAST_NAMES);
  const firstName = _jpPick(BR_FIRST_NAMES);
  const year = _jpInt(1970, 2000);
  const month = _jpInt(1, 12);
  const daysInMonth = new Date(year, month, 0).getDate();
  const day = _jpInt(1, daysInMonth);
  const birthday = `${String(day).padStart(2, '0')}/${String(month).padStart(2, '0')}/${year}`;

  const loc = _jpPick(BR_LOCATIONS);
  const stateName = BR_STATE_NAMES[loc.state] || loc.state;
  const zip = _jpPick(loc.ceps);
  const street = _jpPick(BR_STREETS_BY_CITY[loc.city] || BR_STREET_NAMES);
  const houseNumber = String(_jpInt(12, 4899));
  const fullAddress = `${street}, ${houseNumber}, ${loc.city} - ${loc.state}, CEP ${zip}`;

  const card = _generateDebitCard(BR_CARD_BINS);
  const now = new Date();
  const expYear = now.getFullYear() + _jpInt(2, 5);
  const expMonth = _jpInt(1, 12);
  const expDate = `${String(expMonth).padStart(2, '0')}/${String(expYear).slice(-2)}`;
  const cvv = String(_jpInt(100, 999));

  const emailNum = _jpInt(10, 9999);
  const emailDomain = _jpPick(BR_EMAIL_DOMAINS);
  const email = `${firstName.toLowerCase()}.${lastName.toLowerCase()}${emailNum}@${emailDomain}`;

  const password = generateProfilePassword();

  return {
    country: 'BR',
    lastName,
    firstName,
    birthday,
    zip,
    houseNumber,
    state: `${stateName} (${loc.state})`,
    city: loc.city,
    street,
    fullAddress,
    cardType: card.cardType,
    cardNumber: card.cardNumber,
    expDate,
    cvv,
    cpf: _brGenerateCpf(),
    email,
    password,
  };
}

function generateUsData() {
  const firstName = _jpPick(US_FIRST_NAMES);
  const lastName = _jpPick(US_LAST_NAMES);
  const year = _jpInt(1970, 2000);
  const month = _jpInt(1, 12);
  const daysInMonth = new Date(year, month, 0).getDate();
  const day = _jpInt(1, daysInMonth);
  const birthday = `${String(month).padStart(2, '0')}/${String(day).padStart(2, '0')}/${year}`;

  const loc = _jpPick(US_LOCATIONS);
  const zip = _jpPick(loc.zips);
  const houseNumber = String(_jpInt(100, 9999));
  const streetName = _jpPick(loc.streets);
  let street = `${houseNumber} ${streetName}`;
  if (Math.random() < 0.42) {
    const unitType = _jpPick(['Apt', 'Suite', 'Unit', '#']);
    const unitNumber = Math.random() < 0.5
      ? String(_jpInt(1, 999))
      : `${_jpInt(1, 30)}${_jpPick(['A', 'B', 'C', 'D'])}`;
    street += `, ${unitType} ${unitNumber}`;
  }
  const fullAddress = `${street}, ${loc.city}, ${loc.state} ${zip}, USA`;

  const card = _generateDebitCard([...JP_CARD_BINS, ...BR_CARD_BINS]);
  const now = new Date();
  const expYear = now.getFullYear() + _jpInt(2, 5);
  const expMonth = _jpInt(1, 12);
  const expDate = `${String(expMonth).padStart(2, '0')}/${String(expYear).slice(-2)}`;
  const cvv = String(_jpInt(100, 999));
  const email = `${firstName.toLowerCase()}.${lastName.toLowerCase()}${_jpInt(10, 9999)}@${_jpPick(US_EMAIL_DOMAINS)}`;

  return {
    country: 'US', lastName, firstName, birthday, zip, houseNumber,
    state: loc.state, city: loc.city, street, fullAddress,
    cardType: card.cardType, cardNumber: card.cardNumber, expDate, cvv,
    cpf: '-', email, password: generateProfilePassword(),
  };
}

function generateGbData() {
  const firstName = _jpPick(GB_FIRST_NAMES);
  const lastName = _jpPick(GB_LAST_NAMES);
  const year = _jpInt(1970, 2000);
  const month = _jpInt(1, 12);
  const day = _jpInt(1, new Date(year, month, 0).getDate());
  const birthday = `${String(day).padStart(2, '0')}/${String(month).padStart(2, '0')}/${year}`;
  const loc = _jpPick(GB_LOCATIONS);
  const zip = _jpPick(loc.zips);
  const houseNumber = String(_jpInt(1, 199));
  let street = `${houseNumber} ${_jpPick(loc.streets)}`;
  if (Math.random() < 0.35) street = `Flat ${_jpInt(1, 30)}, ${street}`;
  const fullAddress = `${street}, ${loc.city}, ${zip}, United Kingdom`;
  const card = _generateDebitCard([...JP_CARD_BINS, ...BR_CARD_BINS]);
  const now = new Date();
  const expYear = now.getFullYear() + _jpInt(2, 5);
  const expDate = `${String(_jpInt(1, 12)).padStart(2, '0')}/${String(expYear).slice(-2)}`;
  const email = `${firstName.toLowerCase()}.${lastName.toLowerCase()}${_jpInt(10, 9999)}@${_jpPick(GB_EMAIL_DOMAINS)}`;
  return {
    country: 'GB', lastName, firstName, birthday, zip, houseNumber,
    state: loc.state, city: loc.city, street, fullAddress,
    cardType: card.cardType, cardNumber: card.cardNumber, expDate,
    cvv: String(_jpInt(100, 999)), cpf: '-', email, password: generateProfilePassword(),
  };
}

let _jpCurrentData = null;
let _jpProfileCountry = ['JP', 'BR', 'US', 'GB'].includes(localStorage.getItem(PROFILE_COUNTRY_STORAGE_KEY))
  ? localStorage.getItem(PROFILE_COUNTRY_STORAGE_KEY)
  : 'JP';

function generateProfileData() {
  if (_jpProfileCountry === 'BR') return generateBrData();
  if (_jpProfileCountry === 'US') return generateUsData();
  if (_jpProfileCountry === 'GB') return generateGbData();
  return generateJpData();
}

function setProfileCountry(country) {
  _jpProfileCountry = ['JP', 'BR', 'US', 'GB'].includes(country) ? country : 'JP';
  localStorage.setItem(PROFILE_COUNTRY_STORAGE_KEY, _jpProfileCountry);
  renderJpData(generateProfileData());
}

function applyProfileUi(country) {
  const cfg = PROFILE_COUNTRY_LABELS[country] || PROFILE_COUNTRY_LABELS.JP;
  if ($('jpPanelTitle')) $('jpPanelTitle').textContent = cfg.title;
  if ($('jpCountryBadge')) {
    $('jpCountryBadge').textContent = cfg.badge;
    $('jpCountryBadge').classList.toggle('is-br', country === 'BR');
    $('jpCountryBadge').classList.toggle('is-us', country === 'US');
    $('jpCountryBadge').classList.toggle('is-gb', country === 'GB');
  }
  const fields = document.querySelector('.jp-fields');
  if (fields) {
    fields.classList.toggle('is-jp', country === 'JP');
    fields.classList.toggle('is-br', country === 'BR');
    fields.classList.toggle('is-us', country === 'US');
    fields.classList.toggle('is-gb', country === 'GB');
  }
  document.querySelectorAll('.jp-country-tab').forEach((tab) => {
    const active = tab.dataset.country === country;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  if ($('jpGenerateBtn')) $('jpGenerateBtn').textContent = cfg.generate;
  document.querySelectorAll('[data-field]').forEach((cell) => {
    cell.hidden = (cfg.hiddenFields || []).includes(cell.dataset.field);
  });
  document.querySelectorAll('[data-label-for]').forEach((label) => {
    const text = cfg.labels[label.dataset.labelFor];
    if (text) label.textContent = text;
  });
}

function renderJpData(data) {
  _jpCurrentData = data;
  applyProfileUi(data.country || _jpProfileCountry);
  $('jpLastNameKana').textContent = data.lastName;
  $('jpFirstNameKana').textContent = data.firstName;
  $('jpBirthday').textContent = data.birthday;
  $('jpZip').textContent = data.zip;
  $('jpHouseNumber').textContent = data.houseNumber || '-';
  $('jpState').textContent = data.state;
  $('jpCity').textContent = data.city;
  $('jpStreet').textContent = data.street;
  $('jpFullAddress').textContent = data.fullAddress;
  $('jpCardType').textContent = data.cardType || '-';
  $('jpCardNumber').textContent = data.cardNumber;
  $('jpCardExp').textContent = data.expDate;
  $('jpCardCvv').textContent = data.cvv;
  $('jpCpf').textContent = data.cpf || '-';
  $('jpEmail').textContent = data.email;
  $('jpPassword').textContent = data.password;
}

if (SHOW_JP_DATA_PANEL) {
  $('jpGenerateBtn').addEventListener('click', () => { renderJpData(generateProfileData()); });
  document.querySelectorAll('.jp-country-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      if (tab.dataset.country === _jpProfileCountry) return;
      setProfileCountry(tab.dataset.country);
    });
  });
  renderJpData(generateProfileData());

  document.querySelectorAll('.jp-copy-btn').forEach(btn => {
    btn.addEventListener('click', async function () {
      const targetId = this.dataset.copy;
      const el = $(targetId);
      if (!el || el.textContent === '-') return;
      await navigator.clipboard.writeText(el.textContent);
      this.textContent = '已复制';
      this.classList.add('copied');
      setTimeout(() => { this.textContent = '复制'; this.classList.remove('copied'); }, 1200);
    });
  });

  $('jpCopyAllBtn').addEventListener('click', async () => {
    if (!_jpCurrentData) return;
    const d = _jpCurrentData;
    const cfg = PROFILE_COUNTRY_LABELS[d.country || _jpProfileCountry] || PROFILE_COUNTRY_LABELS.JP;
    const text = cfg.copyRows
      .filter(([, key]) => d[key] && d[key] !== '-')
      .map(([label, key]) => `${label}: ${d[key]}`)
      .join('\n');
    await navigator.clipboard.writeText(text);
    const btn = $('jpCopyAllBtn');
    btn.textContent = '已复制全部资料 ✓';
    setTimeout(() => { btn.textContent = '一键复制全部资料'; }, 1500);
  });
}
