/**
 * 浏览器 API 基址（可选）。默认使用当前页面同源，手机直连 http://电脑IP:8000 时无需配置。
 */
(function (global) {
  const STORAGE_KEY = "hongguo_api_base";

  function normalizeBase(url) {
    return String(url || "")
      .trim()
      .replace(/\/+$/, "");
  }

  function getApiBase() {
    if (global.HONGGUO_API_BASE) {
      return normalizeBase(global.HONGGUO_API_BASE);
    }
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) return normalizeBase(saved);
    } catch {
      /* private mode */
    }
    return "";
  }

  function setApiBase(url) {
    const base = normalizeBase(url);
    try {
      if (base) localStorage.setItem(STORAGE_KEY, base);
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
    global.HONGGUO_API_BASE = base;
  }

  function apiUrl(path) {
    if (!path) return "";
    if (/^https?:\/\//i.test(path)) return path;
    const p = path.startsWith("/") ? path : `/${path}`;
    const base = getApiBase();
    if (base) return `${base}${p}`;
    if (global.location?.origin) return `${global.location.origin}${p}`;
    return p;
  }

  function isCapacitor() {
    return Boolean(global.Capacitor?.isNativePlatform?.());
  }

  const origFetch = global.fetch?.bind(global);
  if (origFetch) {
    global.fetch = function patchedFetch(input, init) {
      if (typeof input === "string" && input.startsWith("/")) {
        return origFetch(apiUrl(input), init);
      }
      if (input instanceof Request && input.url.startsWith("/")) {
        return origFetch(new Request(apiUrl(input.url), input), init);
      }
      return origFetch(input, init);
    };
  }

  function bindSettingsUi() {
    const input = document.getElementById("api-base-input");
    const saveBtn = document.getElementById("api-base-save");
    const clearBtn = document.getElementById("api-base-clear");
    if (!input || !saveBtn) return;

    input.value = getApiBase();
    saveBtn.addEventListener("click", () => {
      setApiBase(input.value);
      alert(getApiBase() ? `已保存：${getApiBase()}` : "已清除，使用当前页面地址");
    });
    clearBtn?.addEventListener("click", () => {
      setApiBase("");
      input.value = "";
      alert("已恢复为当前页面服务器");
    });
  }

  global.HongguoApi = {
    getApiBase,
    setApiBase,
    apiUrl,
    isCapacitor,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindSettingsUi);
  } else {
    bindSettingsUi();
  }
})(window);
