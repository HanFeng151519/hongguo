/**
 * 番茄达人中心 · 下载地址自动回传助手
 * 用法：在 https://koc.fqopenplatform.com 登录后，控制台粘贴本脚本全文回车；
 * 或在书签地址栏填入：javascript:(function(){var s=document.createElement('script');s.src='http://127.0.0.1:8000/koc-capture-helper.js';document.body.appendChild(s);})();
 */
(function () {
  if (window.__hongguoKocCapture) return;
  window.__hongguoKocCapture = true;

  const API = "http://127.0.0.1:8000/api/material/fq-koc/capture";
  const VOD_RE = /fanqieopenvod|fqkol/i;
  let lastIds = { series_id: "", item_id: "" };

  function post(payload) {
    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json())
      .then((d) => {
        if (d.ok) {
          console.info("[红果] 已回传下载地址", d);
          showToast(d.message || "已保存下载地址");
        } else {
          console.warn("[红果] 回传失败", d);
        }
      })
      .catch((e) => console.warn("[红果] 无法连接本地服务", e));
  }

  function showToast(msg) {
    let el = document.getElementById("hongguo-koc-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "hongguo-koc-toast";
      el.style.cssText =
        "position:fixed;z-index:99999;right:16px;bottom:16px;padding:12px 16px;background:#111;color:#0f0;font:14px/1.4 sans-serif;border-radius:8px;max-width:360px;box-shadow:0 4px 20px rgba(0,0,0,.4)";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    setTimeout(() => el.remove(), 5000);
  }

  function parseIdsFromBody(body) {
    if (!body) return {};
    const raw = typeof body === "string" ? body : "";
    if (raw.includes("=") && !raw.trim().startsWith("{")) {
      const p = new URLSearchParams(raw);
      return {
        series_id: p.get("book_id") || "",
        item_id: p.get("item_id") || "",
      };
    }
    try {
      const o = typeof body === "string" ? JSON.parse(body) : body;
      const book = String(o.book_id || o.content_id || o.series_id || "");
      const list = o.item_id_list || o.item_ids || [];
      const item = String(
        o.item_id || (Array.isArray(list) && list[0]) || ""
      );
      return { series_id: book, item_id: item };
    } catch {
      return {};
    }
  }

  function extractUrlFromJson(text) {
    try {
      const o = JSON.parse(text);
      const keys = ["download_url", "url", "play_url", "video_url"];
      function walk(x) {
        if (!x || typeof x !== "object") return "";
        for (const k of keys) {
          if (typeof x[k] === "string" && x[k].startsWith("http")) return x[k];
        }
        for (const v of Object.values(x)) {
          const u = walk(v);
          if (u) return u;
        }
        return "";
      }
      return walk(o);
    } catch {
      return "";
    }
  }

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input?.url || "";
    const isCreate = url.includes("batch_download/create");
    const isVod = VOD_RE.test(url);

    return origFetch.apply(this, arguments).then(async (res) => {
      try {
        if (isCreate) {
          const clone = res.clone();
          const text = await clone.text();
          const ids = parseIdsFromBody(init?.body);
          if (ids.series_id) lastIds = ids;
          const downloadUrl = extractUrlFromJson(text);
          post({
            series_id: ids.series_id,
            item_id: ids.item_id,
            create_url: url,
            cookie: document.cookie,
            download_body:
              typeof init?.body === "string" ? init.body : undefined,
            download_url: downloadUrl || undefined,
            mp4_url: downloadUrl && VOD_RE.test(downloadUrl) ? downloadUrl : undefined,
          });
        } else if (isVod && res.ok) {
          post({
            series_id: lastIds.series_id,
            item_id: lastIds.item_id,
            mp4_url: url,
          });
        }
      } catch (e) {
        console.warn("[红果] 捕获异常", e);
      }
      return res;
    });
  };

  // PerformanceObserver 兜底：捕获已发起的 CDN 请求
  try {
    const obs = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (VOD_RE.test(e.name)) {
          post({
            series_id: lastIds.series_id,
            item_id: lastIds.item_id,
            mp4_url: e.name,
          });
        }
      }
    });
    obs.observe({ type: "resource", buffered: true });
  } catch {
    /* ignore */
  }

  showToast("红果助手已启用：在内容库点「下载」将自动回传 MP4 地址");
  console.info("[红果] koc-capture-helper 已加载，API=", API);
})();
