import { crawlAndDownload } from "./crawl/index.js";
import { loadCookies, saveCookies, saveVideoBuffer, shareVideoFile } from "./crawl/storage.js";

const form = document.getElementById("crawl-form");
const shareInput = document.getElementById("share-input");
const dyCookie = document.getElementById("dy-cookie");
const xhsCookie = document.getElementById("xhs-cookie");
const submitBtn = document.getElementById("submit-btn");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const metaEl = document.getElementById("meta");
const previewEl = document.getElementById("preview");
const saveBtn = document.getElementById("save-btn");

let lastFile = null;

function setStatus(text, type = "") {
  statusEl.textContent = text;
  statusEl.className = `wm-status ${type}`.trim();
  statusEl.classList.remove("hidden");
}

function formatMb(bytes) {
  return ((bytes || 0) / 1024 / 1024).toFixed(1);
}

async function initCookies() {
  const saved = await loadCookies();
  if (dyCookie) dyCookie.value = saved.douyinCookie;
  if (xhsCookie) xhsCookie.value = saved.xhsCookie;
}

initCookies().catch(() => {});

form?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = shareInput?.value.trim() || "";
  const douyinCookieVal = dyCookie?.value.trim() || "";
  const xhsCookieVal = xhsCookie?.value.trim() || "";

  if (!text) {
    setStatus("请粘贴分享文案或链接", "error");
    return;
  }

  submitBtn.disabled = true;
  resultEl.classList.add("hidden");
  lastFile = null;

  try {
    await saveCookies({ douyinCookie: douyinCookieVal, xhsCookie: xhsCookieVal });
    setStatus("正在爬取（解析链接 → 获取直链 → 下载）…");

    const result = await crawlAndDownload(text, {
      douyinCookie: douyinCookieVal,
      xhsCookie: xhsCookieVal,
      onProgress(done, total) {
        if (total > 0) {
          setStatus(`正在下载视频… ${formatMb(done)} MB`);
        }
      },
    });

    const id = String(result.aweme_id || "video").replace(/\D/g, "") || "video";
    const filename = `${result.source || "video"}_${id}.mp4`;
    const saved = await saveVideoBuffer(result.buffer, filename);
    lastFile = saved;

    previewEl.removeAttribute("src");
    previewEl.load();
    previewEl.src = saved.webPath;

    const wmHint = result.watermark_free ? "无水印" : "含水印/平台流";
    const parts = [
      `来源：${result.source || "—"}`,
      result.duration ? `${Math.round(result.duration)} 秒` : "",
      `${formatMb(result.size)} MB`,
      wmHint,
      result.crawl_method ? `(${result.crawl_method})` : "",
    ].filter(Boolean);
    metaEl.textContent = parts.join(" · ");

    setStatus(`爬取完成 · ${wmHint}`, "ok");
    resultEl.classList.remove("hidden");
  } catch (err) {
    setStatus(err.message || "爬取失败", "error");
  } finally {
    submitBtn.disabled = false;
  }
});

saveBtn?.addEventListener("click", async () => {
  if (!lastFile?.uri) {
    setStatus("请先爬取视频", "error");
    return;
  }
  saveBtn.disabled = true;
  setStatus("正在打开系统分享菜单，请选择「储存视频」…");
  try {
    await shareVideoFile(lastFile.uri, lastFile.filename);
    setStatus("已发起保存，请在系统菜单确认", "ok");
  } catch (err) {
    if (err?.message?.includes("cancel") || err?.message?.includes("Cancel")) {
      setStatus("已取消保存", "error");
    } else {
      setStatus(err.message || "保存失败", "error");
    }
  } finally {
    saveBtn.disabled = false;
  }
});
