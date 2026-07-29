const EXPOSE_PATCH = "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.token=ye,t}({});";
const EXPOSE_REPLACEMENT =
  "return o?r?.[n(63)]?ce({so:o,c:r[n(63)]},t):o:null},t.__debug_setSessionObserver=se,t.token=ye,t.__debug_n=_n,t.__debug_bindProof=D,t}({});";
const INSTANCE_PATCH = "var P=new _;";
const INSTANCE_REPLACEMENT = "var P=new _;globalThis.__debugP=P;";
const SDK_GLOBAL_PATCH = "var SentinelSDK=";
const SDK_GLOBAL_REPLACEMENT = "globalThis.SentinelSDK=";

function bytesToBase64(bytes) {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let out = "";
  let i = 0;
  while (i < bytes.length) {
    const b0 = bytes[i++] || 0;
    const b1 = bytes[i++] || 0;
    const b2 = bytes[i++] || 0;
    const n = (b0 << 16) | (b1 << 8) | b2;
    out += chars[(n >> 18) & 63];
    out += chars[(n >> 12) & 63];
    out += i - 2 < bytes.length ? chars[(n >> 6) & 63] : "=";
    out += i - 1 < bytes.length ? chars[n & 63] : "=";
  }
  return out;
}

function base64ToBytes(base64) {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const clean = String(base64 || "").replace(/[^A-Za-z0-9+/=]/g, "");
  const bytes = [];
  for (let i = 0; i < clean.length; i += 4) {
    const c0 = chars.indexOf(clean[i]);
    const c1 = chars.indexOf(clean[i + 1]);
    const c2 = chars.indexOf(clean[i + 2]);
    const c3 = chars.indexOf(clean[i + 3]);
    const n = ((c0 & 63) << 18) | ((c1 & 63) << 12) | (((c2 < 0 ? 0 : c2) & 63) << 6) | ((c3 < 0 ? 0 : c3) & 63);
    bytes.push((n >> 16) & 255);
    if (clean[i + 2] !== "=") bytes.push((n >> 8) & 255);
    if (clean[i + 3] !== "=") bytes.push(n & 255);
  }
  return bytes;
}

function createEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) {
      if (typeof listener !== "function") return;
      const key = String(type || "");
      if (!listeners.has(key)) listeners.set(key, []);
      listeners.get(key).push(listener);
    },
    removeEventListener(type, listener) {
      const key = String(type || "");
      const items = listeners.get(key) || [];
      listeners.set(key, items.filter((item) => item !== listener));
    },
    dispatchEvent(event) {
      const evt = event && event.type ? event : { type: String(event || "") };
      const items = listeners.get(String(evt.type || "")) || [];
      for (const listener of [...items]) {
        listener.call(this, evt);
      }
      const handler = this[`on${evt.type}`];
      if (typeof handler === "function") handler.call(this, evt);
      return true;
    },
  };
}

function createStorage(seed) {
  const map = new Map();
  for (const [key, value] of Object.entries(seed || {})) {
    map.set(String(key), String(value));
  }
  return {
    get length() {
      return map.size;
    },
    key(index) {
      return Array.from(map.keys())[Number(index)] || null;
    },
    clear() {
      map.clear();
    },
    getItem(key) {
      return map.has(String(key)) ? map.get(String(key)) : null;
    },
    setItem(key, value) {
      map.set(String(key), String(value));
    },
    removeItem(key) {
      map.delete(String(key));
    },
  };
}

function createCookieJar(initialCookie) {
  const values = new Map();
  for (const part of String(initialCookie || "").split(";")) {
    const text = part.trim();
    if (!text) continue;
    const idx = text.indexOf("=");
    if (idx <= 0) continue;
    values.set(text.slice(0, idx).trim(), text.slice(idx + 1).trim());
  }
  return {
    get cookie() {
      return Array.from(values.entries()).map(([key, value]) => `${key}=${value}`).join("; ");
    },
    set cookie(value) {
      const text = String(value || "").split(";", 1)[0].trim();
      const idx = text.indexOf("=");
      if (idx <= 0) return;
      values.set(text.slice(0, idx).trim(), text.slice(idx + 1).trim());
    },
    get(name) {
      return values.get(String(name || "")) || "";
    },
  };
}

function setToStringTag(target, tag) {
  try {
    Object.defineProperty(target, Symbol.toStringTag, { value: tag, configurable: true });
  } catch {}
  return target;
}

function createNamedArray(items, tag) {
  const arr = Array.from(items || []);
  arr.item = (index) => arr[Number(index)] || null;
  arr.namedItem = (name) => arr.find((item) => item && item.name === String(name)) || null;
  arr.refresh = () => {};
  for (const item of arr) {
    if (item && item.name) arr[item.name] = item;
  }
  return setToStringTag(arr, tag || "HTMLCollection");
}

function createElement(tagName, env) {
  const tag = String(tagName || "div").toLowerCase();
  const target = createEventTarget();
  const element = {
    ...target,
    nodeType: 1,
    tagName: tag.toUpperCase(),
    nodeName: tag.toUpperCase(),
    style: {},
    children: [],
    childNodes: [],
    attributes: {},
    dataset: {},
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() { return false; } },
    id: "",
    className: "",
    textContent: "",
    innerHTML: "",
    src: "",
    parentNode: null,
    parentElement: null,
    ownerDocument: null,
    namespaceURI: "http://www.w3.org/1999/xhtml",
    clientLeft: 0,
    clientTop: 0,
    scrollLeft: 0,
    scrollTop: 0,
    offsetLeft: 0,
    offsetTop: 0,
    offsetWidth: 0,
    offsetHeight: 0,
    appendChild(child) {
      if (child && typeof child === "object") {
        child.parentNode = this;
        child.parentElement = this;
      }
      this.children.push(child);
      this.childNodes.push(child);
      return child;
    },
    insertBefore(child) {
      return this.appendChild(child);
    },
    removeChild(child) {
      this.children = this.children.filter((x) => x !== child);
      this.childNodes = this.childNodes.filter((x) => x !== child);
      return child;
    },
    remove() {
      if (this.parentNode && typeof this.parentNode.removeChild === "function") {
        this.parentNode.removeChild(this);
      }
    },
    setAttribute(name, value) {
      const key = String(name);
      const text = String(value);
      this.attributes[key] = { name: key, value: text };
      this[key] = text;
      if (key === "class") this.className = text;
      if (key === "id") this.id = text;
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this, String(name)) ? String(this[String(name)]) : null;
    },
    removeAttribute(name) {
      const key = String(name);
      delete this.attributes[key];
      delete this[key];
    },
    hasAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, String(name));
    },
    contains(node) {
      return node === this || this.children.includes(node);
    },
    matches() {
      return false;
    },
    closest() {
      return null;
    },
    getBoundingClientRect() {
      const width = Number(this.clientWidth || 0);
      const height = Number(this.clientHeight || 0);
      return { x: 0, y: 0, width, height, top: 0, left: 0, right: width, bottom: height };
    },
  };
  if (tag === "iframe") {
    const frameWindow = createEventTarget();
    frameWindow.parent = globalThis;
    frameWindow.top = globalThis;
    frameWindow.self = frameWindow;
    frameWindow.location = { href: "", origin: "", pathname: "", search: "" };
    frameWindow.postMessage = (message) => {
      if (env && typeof env.handleFramePostMessage === "function") {
        env.handleFramePostMessage(frameWindow, message);
      }
    };
    element.contentWindow = frameWindow;
    element.contentDocument = { defaultView: frameWindow };
  }
  if (tag === "canvas") {
    element.width = 300;
    element.height = 150;
    element.toDataURL = () => "data:image/png;base64,";
    element.getContext = (kind) => {
      const type = String(kind || "");
      if (type.includes("webgl")) {
        const webgl = {
          VENDOR: 7936,
          RENDERER: 7937,
          VERSION: 7938,
          SHADING_LANGUAGE_VERSION: 35724,
          UNMASKED_VENDOR_WEBGL: 37445,
          UNMASKED_RENDERER_WEBGL: 37446,
        };
        return {
          ...webgl,
          canvas: element,
          getParameter(name) {
            if (name === webgl.VENDOR) return "Mozilla";
            if (name === webgl.RENDERER) return "Mozilla";
            if (name === webgl.VERSION) return "WebGL 1.0";
            if (name === webgl.SHADING_LANGUAGE_VERSION) return "WebGL GLSL ES 1.0";
            if (name === webgl.UNMASKED_VENDOR_WEBGL) return "Mozilla";
            if (name === webgl.UNMASKED_RENDERER_WEBGL) return "Mozilla";
            return "";
          },
          getExtension() {
            return null;
          },
          getSupportedExtensions() {
            return [];
          },
        };
      }
      return {
        canvas: element,
        fillRect() {},
        clearRect() {},
        getImageData() {
          return { data: new Uint8ClampedArray(4), width: 1, height: 1 };
        },
        putImageData() {},
        createImageData() {
          return { data: new Uint8ClampedArray(4), width: 1, height: 1 };
        },
        measureText(text) {
          return { width: String(text || "").length * 7 };
        },
        fillText() {},
        strokeText() {},
        beginPath() {},
        closePath() {},
        moveTo() {},
        lineTo() {},
        stroke() {},
        fill() {},
      };
    };
  }
  return element;
}

function installRuntime(payload) {
  const nativeSetTimeout = typeof globalThis.setTimeout === "function" ? globalThis.setTimeout.bind(globalThis) : null;
  const nativeClearTimeout = typeof globalThis.clearTimeout === "function" ? globalThis.clearTimeout.bind(globalThis) : null;
  const nativeSetInterval = typeof globalThis.setInterval === "function" ? globalThis.setInterval.bind(globalThis) : null;
  const nativeClearInterval = typeof globalThis.clearInterval === "function" ? globalThis.clearInterval.bind(globalThis) : null;
  const userAgent = String(payload.user_agent || "Mozilla/5.0");
  const sdkUrl = String(payload.sdk_url || "https://sentinel.openai.com/sentinel/sdk.js");
  const frameUrl = String(payload.frame_url || "https://chatgpt.com/backend-api/sentinel/frame.html");
  const pageUrl = String(payload.page_url || "https://auth.openai.com/");
  const pageMatch = pageUrl.match(/^(https?:)\/\/([^\/]+)(\/[^?#]*)?(\?[^#]*)?(#.*)?$/i);
  const pageOrigin = pageMatch ? `${pageMatch[1]}//${pageMatch[2]}` : "https://auth.openai.com";
  const pagePath = pageMatch && pageMatch[3] ? pageMatch[3] : "/";
  const pageSearch = pageMatch && pageMatch[4] ? pageMatch[4] : "";
  const isFirefox = /Firefox\//i.test(userAgent);
  const timezone = String(payload.timezone || "Asia/Shanghai");
  const timezoneOffsetMin = Number(payload.timezone_offset_min ?? -480);
  const eventTarget = createEventTarget();
  const cookieJar = createCookieJar(payload.document_cookie || `oai-did=${encodeURIComponent(payload.device_id || "")}`);
  const screen = {
    width: Number(payload.screen_width || 2560),
    height: Number(payload.screen_height || 1440),
    availWidth: Number(payload.screen_avail_width || payload.screen_width || 2560),
    availHeight: Number(payload.screen_avail_height || payload.screen_height || 1440),
    availLeft: Number(payload.screen_avail_left || 0),
    availTop: Number(payload.screen_avail_top || 0),
    colorDepth: Number(payload.color_depth || 30),
    pixelDepth: Number(payload.pixel_depth || payload.color_depth || 30),
    orientation: { angle: 0, type: "landscape-primary", addEventListener() {}, removeEventListener() {} },
  };
  const scripts = [];
  const env = {
    handleFramePostMessage(frameWindow, message) {
      const event = new globalThis.MessageEvent("message", {
        data: {
          type: "response",
          requestId: message && message.requestId,
          result: null,
          error: "frame fixture unavailable",
        },
        source: frameWindow,
        origin: frameWindow.location.origin || pageOrigin,
      });
      globalThis.dispatchEvent(event);
    },
  };
  const documentElement = createElement("html", env);
  documentElement.clientWidth = Number(payload.viewport_width || 1800);
  documentElement.clientHeight = Number(payload.viewport_height || 839);
  documentElement.setAttribute("data-build", String(payload.client_version || ""));
  const body = createElement("body", env);
  const head = createElement("head", env);
  const document = {
    ...createEventTarget(),
    readyState: "complete",
    hidden: false,
    visibilityState: "visible",
    referrer: String(payload.referrer || "https://auth.openai.com/"),
    URL: pageUrl,
    baseURI: pageUrl,
    domain: pageMatch ? pageMatch[2] : "auth.openai.com",
    characterSet: "UTF-8",
    charset: "UTF-8",
    compatMode: "CSS1Compat",
    contentType: "text/html",
    scripts,
    currentScript: { src: sdkUrl, getAttribute(name) { return String(name || "").toLowerCase() === "src" ? sdkUrl : null; } },
    documentElement,
    body,
    head,
    defaultView: globalThis,
    get cookie() {
      return cookieJar.cookie;
    },
    set cookie(value) {
      cookieJar.cookie = value;
    },
    createElement(tag) {
      const el = createElement(tag, env);
      el.ownerDocument = this;
      if (String(tag).toLowerCase() === "script") scripts.push(el);
      return el;
    },
    createElementNS(_ns, tag) {
      return this.createElement(tag);
    },
    createTextNode(text) {
      return {
        nodeType: 3,
        nodeName: "#text",
        textContent: String(text || ""),
        parentNode: null,
        parentElement: null,
        ownerDocument: this,
      };
    },
    createDocumentFragment() {
      const fragment = createElement("fragment", env);
      fragment.nodeType = 11;
      fragment.nodeName = "#document-fragment";
      fragment.ownerDocument = this;
      return fragment;
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return createNamedArray([], "NodeList");
    },
    getElementById() {
      return null;
    },
    getElementsByTagName(tag) {
      const name = String(tag || "").toLowerCase();
      if (name === "script") return createNamedArray(scripts, "HTMLCollection");
      if (name === "body") return createNamedArray([body], "HTMLCollection");
      if (name === "head") return createNamedArray([head], "HTMLCollection");
      if (name === "html") return createNamedArray([documentElement], "HTMLCollection");
      return createNamedArray([], "HTMLCollection");
    },
    getElementsByClassName() {
      return createNamedArray([], "HTMLCollection");
    },
    getElementsByName() {
      return createNamedArray([], "NodeList");
    },
    elementFromPoint() {
      return null;
    },
    hasFocus() {
      return true;
    },
    title: "",
    doctype: null,
    forms: createNamedArray([], "HTMLCollection"),
    images: createNamedArray([], "HTMLCollection"),
    links: createNamedArray([], "HTMLCollection"),
    anchors: createNamedArray([], "HTMLCollection"),
    embeds: createNamedArray([], "HTMLCollection"),
    plugins: createNamedArray([], "HTMLCollection"),
    children: [documentElement],
    childNodes: [documentElement],
    firstElementChild: documentElement,
    activeElement: body,
    scrollingElement: documentElement,
    fullscreenElement: null,
    fullscreenEnabled: true,
    pictureInPictureElement: null,
    adoptedStyleSheets: [],
    implementation: {
      hasFeature() {
        return true;
      },
      createHTMLDocument(title) {
        return { title: String(title || ""), body: createElement("body", env), documentElement: createElement("html", env) };
      },
    },
    fonts: { ready: Promise.resolve(), check() { return true; } },
  };
  setToStringTag(document, "HTMLDocument");
  const scriptElement = createElement("script", env);
  scriptElement.ownerDocument = document;
  scriptElement.src = sdkUrl;
  scriptElement.getAttribute = (name) => (String(name || "").toLowerCase() === "src" ? sdkUrl : null);
  scripts.push(scriptElement);
  head.appendChild(scriptElement);
  body.clientWidth = documentElement.clientWidth;
  body.clientHeight = documentElement.clientHeight;
  const originalBodyAppendChild = body.appendChild.bind(body);
  body.appendChild = (child) => {
    const result = originalBodyAppendChild(child);
    if (child && child.tagName === "IFRAME") {
      const rawFrameUrl = String(child.src || frameUrl);
      const frameMatch = rawFrameUrl.match(/^(https?:)\/\/([^\/]+)(\/[^?#]*)?(\?[^#]*)?(#.*)?$/i);
      const frameOrigin = frameMatch ? `${frameMatch[1]}//${frameMatch[2]}` : "https://chatgpt.com";
      child.contentWindow.location = {
        href: rawFrameUrl,
        origin: frameOrigin,
        pathname: frameMatch && frameMatch[3] ? frameMatch[3] : "/backend-api/sentinel/frame.html",
        search: frameMatch && frameMatch[4] ? frameMatch[4] : "",
      };
      const fireLoad = () => child.dispatchEvent(new globalThis.Event("load"));
      if (nativeSetTimeout) nativeSetTimeout(fireLoad, 0);
      else fireLoad();
    }
    return result;
  };

  const performance = {
    now: () => Number(payload.performance_now || 12345.67),
    timeOrigin: Number(payload.time_origin || 1710000000000),
    memory: { jsHeapSizeLimit: Number(payload.js_heap_size_limit || 4294967296) },
    getEntries() {
      return [];
    },
    getEntriesByType() {
      return [];
    },
    getEntriesByName() {
      return [];
    },
    mark() {},
    measure() {},
    clearMarks() {},
    clearMeasures() {},
    toJSON() {
      return { timeOrigin: this.timeOrigin };
    },
  };
  setToStringTag(performance, "Performance");

  class TextEncoderPoly {
    encode(text) {
      const str = String(text || "");
      const out = new Uint8Array(str.length);
      for (let i = 0; i < str.length; i += 1) out[i] = str.charCodeAt(i) & 255;
      return out;
    }
  }

  class TextDecoderPoly {
    decode(input) {
      if (!input) return "";
      let out = "";
      for (let i = 0; i < input.length; i += 1) {
        out += String.fromCharCode(input[i]);
      }
      return out;
    }
  }

  class URLSearchParamsPoly {
    constructor(search) {
      this._pairs = [];
      const s = String(search || "").replace(/^\?/, "");
      if (!s) return;
      const parts = s.split("&");
      for (const p of parts) {
        if (!p) continue;
        const i = p.indexOf("=");
        if (i < 0) {
          this._pairs.push([decodeURIComponent(p), ""]);
        } else {
          this._pairs.push([
            decodeURIComponent(p.slice(0, i)),
            decodeURIComponent(p.slice(i + 1)),
          ]);
        }
      }
    }
    keys() {
      return this._pairs.map((x) => x[0])[Symbol.iterator]();
    }
  }

  class URLPoly {
    constructor(input, base) {
      const raw = String(input || "");
      if (/^https?:\/\//i.test(raw)) {
        this.href = raw;
      } else {
        const b = String(base || "https://auth.openai.com/").replace(/\/$/, "");
        this.href = `${b}/${raw.replace(/^\//, "")}`;
      }
      const m = this.href.match(/^(https?:)\/\/([^\/]+)(\/[^?#]*)?(\?[^#]*)?(#.*)?$/i);
      this.protocol = m ? m[1] : "https:";
      this.host = m ? m[2] : "auth.openai.com";
      this.hostname = this.host;
      this.pathname = m && m[3] ? m[3] : "/";
      this.search = m && m[4] ? m[4] : "";
      this.hash = m && m[5] ? m[5] : "";
      this.origin = `${this.protocol}//${this.host}`;
    }
    toString() {
      return this.href;
    }
  }

  class ObserverPoly {
    constructor(callback) {
      this.callback = callback;
    }
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  }

  class WorkerPoly {
    constructor() {
      Object.assign(this, createEventTarget());
    }
    postMessage() {}
    terminate() {}
  }

  globalThis.window = globalThis;
  globalThis.self = globalThis;
  globalThis.top = globalThis;
  globalThis.parent = globalThis;
  globalThis.document = document;
  class NavigatorPoly {
    javaEnabled() {
      return false;
    }
    sendBeacon() {
      return true;
    }
    vibrate() {
      return false;
    }
    getBattery() {
      return Promise.resolve({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1 });
    }
  }
  const navigatorPayload = {
    appCodeName: "Mozilla",
    appName: "Netscape",
    appVersion: userAgent.replace(/^Mozilla\//, ""),
    userAgent,
    language: String(payload.language || "en-US"),
    languages: Array.isArray(payload.languages) ? payload.languages : ["en-US", "en"],
    hardwareConcurrency: Number(payload.hardware_concurrency || 10),
    platform: String(payload.platform || (isFirefox && /Macintosh/i.test(userAgent) ? "MacIntel" : "Win32")),
    vendor: Object.prototype.hasOwnProperty.call(payload, "vendor") ? String(payload.vendor || "") : (isFirefox ? "" : "Google Inc."),
    product: "Gecko",
    productSub: "20100101",
    oscpu: /Macintosh/i.test(userAgent) ? "Intel Mac OS X 10.15" : "Windows NT 10.0; Win64; x64",
    cookieEnabled: true,
    onLine: true,
    webdriver: false,
    maxTouchPoints: 0,
    pdfViewerEnabled: true,
    mimeTypes: createNamedArray([], "MimeTypeArray"),
    plugins: createNamedArray([], "PluginArray"),
    permissions: { query: () => Promise.resolve({ state: "prompt", onchange: null }) },
    storage: {
      estimate: () => Promise.resolve({ quota: 2147483648, usage: 0 }),
      persist: () => Promise.resolve(false),
      persisted: () => Promise.resolve(false),
    },
    clipboard: { readText: () => Promise.resolve(""), writeText: () => Promise.resolve() },
    mediaDevices: {
      enumerateDevices: () => Promise.resolve([]),
      getUserMedia: () => Promise.reject(new Error("Permission denied")),
    },
    geolocation: {
      getCurrentPosition(_success, error) {
        if (typeof error === "function") error({ code: 1, message: "User denied Geolocation" });
      },
      watchPosition() {
        return 1;
      },
      clearWatch() {},
    },
  };
  if (!isFirefox) {
    navigatorPayload.connection = { effectiveType: "4g", rtt: 50, downlink: 10, saveData: false };
  }
  globalThis.navigator = Object.assign(new NavigatorPoly(), navigatorPayload);
  setToStringTag(globalThis.navigator, "Navigator");
  globalThis.location = {
    href: pageUrl,
    origin: pageOrigin,
    pathname: pagePath,
    search: pageSearch,
  };
  document.location = globalThis.location;
  globalThis.screen = screen;
  setToStringTag(globalThis.screen, "Screen");
  globalThis.innerWidth = Number(payload.viewport_width || 1800);
  globalThis.innerHeight = Number(payload.viewport_height || 839);
  globalThis.outerWidth = Number(payload.outer_width || 1800);
  globalThis.outerHeight = Number(payload.outer_height || 1000);
  globalThis.devicePixelRatio = Number(payload.device_pixel_ratio || 1);
  globalThis.origin = pageOrigin;
  globalThis.name = "";
  globalThis.frames = globalThis;
  globalThis.length = 0;
  globalThis.opener = null;
  globalThis.frameElement = null;
  globalThis.closed = false;
  globalThis.status = "";
  globalThis.defaultStatus = "";
  globalThis.screenX = Number(payload.screen_x || 0);
  globalThis.screenY = Number(payload.screen_y || 0);
  globalThis.screenLeft = globalThis.screenX;
  globalThis.screenTop = globalThis.screenY;
  globalThis.scrollX = 0;
  globalThis.scrollY = 0;
  globalThis.pageXOffset = 0;
  globalThis.pageYOffset = 0;
  globalThis.mozInnerScreenX = globalThis.screenX;
  globalThis.mozInnerScreenY = globalThis.screenY;
  globalThis.visualViewport = {
    width: globalThis.innerWidth,
    height: globalThis.innerHeight,
    offsetLeft: 0,
    offsetTop: 0,
    pageLeft: 0,
    pageTop: 0,
    scale: 1,
    addEventListener() {},
    removeEventListener() {},
  };
  globalThis.scrollTo = () => {};
  globalThis.scrollBy = () => {};
  globalThis.moveTo = () => {};
  globalThis.moveBy = () => {};
  globalThis.resizeTo = () => {};
  globalThis.resizeBy = () => {};
  globalThis.focus = () => {};
  globalThis.blur = () => {};
  globalThis.alert = () => {};
  globalThis.confirm = () => false;
  globalThis.prompt = () => null;
  globalThis.queueMicrotask = globalThis.queueMicrotask || ((cb) => Promise.resolve().then(cb));
  globalThis.locationbar = { visible: true };
  globalThis.menubar = { visible: true };
  globalThis.personalbar = { visible: true };
  globalThis.scrollbars = { visible: true };
  globalThis.statusbar = { visible: true };
  globalThis.toolbar = { visible: true };
  globalThis.external = {};
  globalThis.customElements = globalThis.customElements || {
    define() {},
    get() {
      return undefined;
    },
    whenDefined() {
      return Promise.resolve();
    },
  };
  globalThis.performance = performance;
  globalThis.localStorage = createStorage(payload.local_storage || {});
  globalThis.sessionStorage = createStorage(payload.session_storage || {});
  globalThis.__sentinel_init_pending = [];
  globalThis.__sentinel_token_pending = [];

  globalThis.setTimeout = nativeSetTimeout || ((cb) => {
    if (typeof cb === "function") cb();
    return 1;
  });
  globalThis.clearTimeout = nativeClearTimeout || (() => {});
  globalThis.setInterval = nativeSetInterval || (() => 1);
  globalThis.clearInterval = nativeClearInterval || (() => {});
  globalThis.requestIdleCallback = (cb) => {
    const run = () => {
      if (typeof cb === "function") cb({ didTimeout: false, timeRemaining: () => 50 });
    };
    if (nativeSetTimeout) return nativeSetTimeout(run, 0);
    run();
    return 1;
  };
  globalThis.cancelIdleCallback = nativeClearTimeout || (() => {});
  globalThis.requestAnimationFrame = (cb) => {
    const run = () => {
      if (typeof cb === "function") cb(globalThis.performance.now());
    };
    if (nativeSetTimeout) return nativeSetTimeout(run, 16);
    run();
    return 1;
  };
  globalThis.cancelAnimationFrame = nativeClearTimeout || (() => {});
  globalThis.addEventListener = eventTarget.addEventListener.bind(globalThis);
  globalThis.removeEventListener = eventTarget.removeEventListener.bind(globalThis);
  globalThis.dispatchEvent = eventTarget.dispatchEvent.bind(globalThis);
  globalThis.postMessage = (message, targetOrigin) => {
    const event = new globalThis.MessageEvent("message", {
      data: message,
      source: globalThis,
      origin: targetOrigin || pageOrigin,
    });
    globalThis.dispatchEvent(event);
  };

  globalThis.atob = (input) => String.fromCharCode(...base64ToBytes(input));
  globalThis.btoa = (input) => {
    const str = String(input || "");
    const bytes = [];
    for (let i = 0; i < str.length; i += 1) bytes.push(str.charCodeAt(i) & 255);
    return bytesToBase64(bytes);
  };
  globalThis.TextEncoder = globalThis.TextEncoder || TextEncoderPoly;
  globalThis.TextDecoder = globalThis.TextDecoder || TextDecoderPoly;
  globalThis.URL = globalThis.URL || URLPoly;
  globalThis.URLSearchParams = globalThis.URLSearchParams || URLSearchParamsPoly;
  globalThis.Event =
    globalThis.Event ||
    class Event {
      constructor(type) {
        this.type = type;
      }
    };
  globalThis.CustomEvent =
    globalThis.CustomEvent ||
    class CustomEvent extends globalThis.Event {
      constructor(type, init) {
        super(type);
        this.detail = init && Object.prototype.hasOwnProperty.call(init, "detail") ? init.detail : null;
      }
    };
  globalThis.MessageEvent =
    globalThis.MessageEvent ||
    class MessageEvent extends globalThis.Event {
      constructor(type, init) {
        super(type);
        this.data = init && Object.prototype.hasOwnProperty.call(init, "data") ? init.data : null;
        this.origin = init && init.origin ? String(init.origin) : "";
        this.source = init && init.source ? init.source : null;
      }
    };
  globalThis.MessageChannel =
    globalThis.MessageChannel ||
    class MessageChannel {
      constructor() {
        this.port1 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
        this.port2 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
      }
    };
  globalThis.MutationObserver = globalThis.MutationObserver || ObserverPoly;
  globalThis.ResizeObserver = globalThis.ResizeObserver || ObserverPoly;
  globalThis.IntersectionObserver = globalThis.IntersectionObserver || ObserverPoly;
  globalThis.Worker = globalThis.Worker || WorkerPoly;
  globalThis.HTMLElement = globalThis.HTMLElement || function HTMLElement() {};
  globalThis.HTMLCanvasElement = globalThis.HTMLCanvasElement || function HTMLCanvasElement() {};
  globalThis.HTMLIFrameElement = globalThis.HTMLIFrameElement || function HTMLIFrameElement() {};
  globalThis.HTMLScriptElement = globalThis.HTMLScriptElement || function HTMLScriptElement() {};
  globalThis.Document = globalThis.Document || function Document() {};
  globalThis.Window = globalThis.Window || function Window() {};
  if (!globalThis.Notification) {
    const NotificationPoly = function Notification() {};
    NotificationPoly.permission = "default";
    NotificationPoly.requestPermission = () => Promise.resolve("default");
    globalThis.Notification = NotificationPoly;
  }
  globalThis.matchMedia =
    globalThis.matchMedia ||
    ((query) => ({
      media: String(query || ""),
      matches: false,
      onchange: null,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
      dispatchEvent() {
        return false;
      },
    }));
  globalThis.getComputedStyle =
    globalThis.getComputedStyle ||
    (() => ({
      getPropertyValue() {
        return "";
      },
    }));
  globalThis.history = globalThis.history || { length: 1, state: null, back() {}, forward() {}, go() {}, pushState() {}, replaceState() {} };
  if (globalThis.Intl && globalThis.Intl.DateTimeFormat) {
    const NativeDateTimeFormat = globalThis.Intl.DateTimeFormat;
    const DateTimeFormatPoly = function DateTimeFormat(locales, options) {
      const formatter = new NativeDateTimeFormat(locales, options);
      const nativeResolvedOptions = formatter.resolvedOptions.bind(formatter);
      formatter.resolvedOptions = () => ({ ...nativeResolvedOptions(), timeZone: timezone });
      return formatter;
    };
    DateTimeFormatPoly.prototype = NativeDateTimeFormat.prototype;
    DateTimeFormatPoly.supportedLocalesOf = NativeDateTimeFormat.supportedLocalesOf.bind(NativeDateTimeFormat);
    globalThis.Intl.DateTimeFormat = DateTimeFormatPoly;
  }
  try {
    Date.prototype.getTimezoneOffset = () => timezoneOffsetMin;
  } catch {}
  if (isFirefox) {
    globalThis.InstallTrigger = globalThis.InstallTrigger || {};
    try {
      delete globalThis.chrome;
    } catch {
      globalThis.chrome = undefined;
    }
  } else {
    globalThis.chrome = globalThis.chrome || { runtime: {}, app: {} };
  }
  globalThis.CSS = globalThis.CSS || { supports() { return true; } };
  globalThis.indexedDB =
    globalThis.indexedDB ||
    {
      open() {
        return { onerror: null, onsuccess: null, onupgradeneeded: null, result: {}, error: null };
      },
      deleteDatabase() {
        return {};
      },
    };
  globalThis.fetch = async () => {
    throw new Error("fetch should not be called");
  };

  const randomFill = (arr) => {
    for (let i = 0; i < arr.length; i += 1) {
      arr[i] = Math.floor(Math.random() * 256);
    }
    return arr;
  };
  const nativeCrypto = globalThis.crypto || {};
  globalThis.crypto = {
    randomUUID: nativeCrypto && typeof nativeCrypto.randomUUID === "function"
      ? nativeCrypto.randomUUID.bind(nativeCrypto)
      : undefined,
    getRandomValues: randomFill,
    subtle: nativeCrypto.subtle || {},
  };
}

function loadPatchedSdk(sdkSource) {
  let sdk = String(sdkSource || "");
  sdk = sdk.replace(SDK_GLOBAL_PATCH, SDK_GLOBAL_REPLACEMENT);
  sdk = sdk.replace(INSTANCE_PATCH, INSTANCE_REPLACEMENT);
  sdk = sdk.replace(EXPOSE_PATCH, EXPOSE_REPLACEMENT);
  eval(sdk);
}

async function run(payload, sdkSource) {
  installRuntime(payload);
  loadPatchedSdk(sdkSource);

  if (payload.action === "requirements") {
    const requestP = await globalThis.__debugP.getRequirementsToken();
    return { request_p: requestP };
  }

  if (payload.action === "solve") {
    const challenge = payload.challenge || {};
    const requestP = String(payload.request_p || "").trim();
    if (!requestP) throw new Error("missing request_p");
    const finalP = await globalThis.__debugP.getEnforcementToken(challenge);
    globalThis.SentinelSDK.__debug_bindProof(challenge, requestP);
    const dx = challenge && challenge.turnstile ? challenge.turnstile.dx : null;
    const tValue = dx ? await globalThis.SentinelSDK.__debug_n(challenge, dx) : null;
    let soToken = "";
    try {
      const flow = String(payload.flow || "authorize_continue");
      if (
        typeof globalThis.SentinelSDK.__debug_setSessionObserver === "function" &&
        typeof globalThis.SentinelSDK.sessionObserverToken === "function"
      ) {
        globalThis.SentinelSDK.__debug_setSessionObserver(flow, challenge);
        soToken = await globalThis.SentinelSDK.sessionObserverToken(flow) || "";
      }
    } catch {
      soToken = "";
    }
    return { final_p: finalP, t: tValue, so_token: soToken };
  }

  throw new Error(`unsupported action: ${payload.action}`);
}

(async () => {
  try {
    const payload = JSON.parse(String(globalThis.__payload_json || "{}"));
    const sdkSource = String(globalThis.__sdk_source || "");
    const result = await run(payload, sdkSource);
    globalThis.__vm_output_json = JSON.stringify(result);
  } catch (error) {
    const detail = {
      name: error && error.name ? String(error.name) : "Error",
      message: error && error.message ? String(error.message) : String(error),
      stack: error && error.stack ? String(error.stack) : String(error),
    };
    const message = `${detail.name}: ${detail.message}\n${detail.stack}`;
    globalThis.__vm_error = message;
  } finally {
    globalThis.__vm_done = true;
  }
})();
