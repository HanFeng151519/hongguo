import { crawlAndDownload } from "./crawl/index.js";
import { loadCookies, saveCookies, saveVideoBuffer, shareVideoFile } from "./crawl/storage.js";
import {
  bindVideoAdjust,
  getEnhanceSettings,
  needsEnhanceExport,
  prepareVideoPreview,
  revealResult,
} from "./video-player.js";
import { exportEnhancedVideoBuffer } from "./video-export.js";
import {
  checkSrServer,
  abortMacSrPoll,
  cancelMacSuperResolutionJob,
  clearMacSrJob,
  downloadSuperResolutionResult,
  fetchSuperResolutionJob,
  hasActiveMacSrJob,
  loadMacSrJob,
  loadSrSettings,
  MacSrCancelledError,
  normalizeServerUrl,
  resumeMacSuperResolutionJob,
  runSuperResolutionPipeline,
  saveSrSettings,
} from "./sr-client.js";
import { etaFromProgressPct, formatSrEta } from "./sr-eta.js";
import {
  isNativeSrAvailable,
  isNativeSrModelReady,
  startNativeSuperResolutionBackground,
  getNativeSrJobStatus,
  clearNativeSrJob,
  cancelNativeSrJob,
  requestSrNotifications,
} from "./sr-native.js";
import { saveMacSrToLibrary, saveNativeSrToLibrary } from "./sr-library.js";
import { bindSrLibraryUI } from "./sr-library-ui.js";
import { getPlugin, isNativePlatform } from "./crawl/capacitor-bridge.js";

const form = document.getElementById("crawl-form");
const shareInput = document.getElementById("share-input");
const dyCookie = document.getElementById("dy-cookie");
const xhsCookie = document.getElementById("xhs-cookie");
const submitBtn = document.getElementById("submit-btn");
const statusEl = document.getElementById("status");
const srProgressWrap = document.getElementById("sr-progress-wrap");
const srProgressBar = document.getElementById("sr-progress-bar");
const srProgressTrack = srProgressWrap?.querySelector(".sr-progress-track");
const srProgressLabel = document.getElementById("sr-progress-label");
const resultEl = document.getElementById("result");
const metaEl = document.getElementById("meta");
const previewEl = document.getElementById("preview");
const saveBtn = document.getElementById("save-btn");
const shareClearBtn = document.getElementById("share-clear");
const sharePasteBtn = document.getElementById("share-paste");
const MAC_SERVER_PRESETS = [
  "http://9.112.85.25:8000",
  "http://192.168.3.56:8000",
  "http://172.31.12.193:8000",
];
const srServerPreset = document.getElementById("sr-server-preset");
const srServerCustom = document.getElementById("sr-server-custom");

function getMacServerUrl() {
  const preset = srServerPreset?.value;
  if (preset && preset !== "__custom__") {
    return normalizeServerUrl(preset);
  }
  return normalizeServerUrl(srServerCustom?.value || "");
}

function applyMacServerUrl(url) {
  const normalized = normalizeServerUrl(url);
  if (!srServerPreset) {
    if (srServerCustom) srServerCustom.value = normalized;
    return;
  }
  if (MAC_SERVER_PRESETS.includes(normalized)) {
    srServerPreset.value = normalized;
    srServerCustom?.classList.add("hidden");
    if (srServerCustom) srServerCustom.value = "";
  } else if (normalized) {
    srServerPreset.value = "__custom__";
    srServerCustom?.classList.remove("hidden");
    if (srServerCustom) srServerCustom.value = normalized;
  } else {
    srServerPreset.value = MAC_SERVER_PRESETS[0];
    srServerCustom?.classList.add("hidden");
    if (srServerCustom) srServerCustom.value = "";
  }
}

function onMacServerChanged() {
  macSrVerified = false;
  syncSrButtons();
  persistSrSettings();
}
const srScaleSelect = document.getElementById("sr-scale");
const srProfileSelect = null;
const srMacStatus = document.getElementById("sr-mac-status");
const srTestBtn = document.getElementById("sr-test");
const srButtons = document.getElementById("sr-buttons");
const srNativeBtn = document.getElementById("sr-native-btn");
const srMacBtn = document.getElementById("sr-mac-btn");
const srNativeTask = document.getElementById("sr-native-task");
const srNativeTaskText = document.getElementById("sr-native-task-text");
const srNativeCancelBtn = document.getElementById("sr-native-cancel-btn");
const srMacCancelBtn = document.getElementById("sr-mac-cancel-btn");
const srLibraryPanel = document.getElementById("sr-library-panel");
const srLibraryList = document.getElementById("sr-library-list");
const srLibraryCount = document.getElementById("sr-library-count");
const uploadVideoBtn = document.getElementById("upload-video-btn");
const videoFileInput = document.getElementById("video-file-input");

let lastFile = null;
let lastVideoBuffer = null;
let srSettings = { serverUrl: "", outputScale: "1080", srProfile: "real" };
let nativeSrReady = false;
let macSrVerified = false;
let macSrPollActive = false;
let srShareInProgress = false;
let nativeSrStartedAt = 0;

const refreshSrLibraryUI = bindSrLibraryUI({
  panel: srLibraryPanel,
  listEl: srLibraryList,
  countEl: srLibraryCount,
});

function syncNativeTaskPanel(job) {
  if (!srNativeTask || !isNativePlatform()) return;
  const show = job && (job.status === "running" || job.status === "failed");
  srNativeTask.classList.toggle("hidden", !show);
  if (!show) return;
  if (job.status === "running") {
    const pct = Math.round((Number(job.progress) || 0) * 100);
    srNativeTaskText.textContent = `本机优化进行中：${job.message || "处理中"}${pct > 0 ? `（${pct}%）` : ""}`;
    if (srNativeCancelBtn) srNativeCancelBtn.textContent = "取消并删除本机任务";
  } else {
    srNativeTaskText.textContent = `本机优化失败：${job.error || job.message || "未知错误"}`;
    if (srNativeCancelBtn) srNativeCancelBtn.textContent = "清除失败任务";
  }
}

refreshSrLibraryUI().catch(() => {});

function showSrProgress(pct, message, prefix = "", etaSec = null) {
  const p = Math.min(100, Math.max(0, Number(pct) || 0));
  srProgressWrap?.classList.remove("hidden");
  if (srProgressBar) srProgressBar.style.width = `${p}%`;
  srProgressTrack?.setAttribute("aria-valuenow", String(Math.round(p)));
  const label = message || "处理中…";
  const text = prefix ? `${prefix}${label}` : label;
  const eta = formatSrEta(etaSec);
  const parts = [text];
  if (p > 0) parts.push(`${Math.round(p)}%`);
  if (eta) parts.push(eta);
  if (srProgressLabel) srProgressLabel.textContent = parts.join(" · ");
}

function hideSrProgress() {
  srProgressWrap?.classList.add("hidden");
  if (srProgressBar) srProgressBar.style.width = "0%";
  srProgressTrack?.setAttribute("aria-valuenow", "0");
  if (srProgressLabel) srProgressLabel.textContent = "";
  nativeSrStartedAt = 0;
  syncMacCancelPanel(false);
}

function isMacSrCancelled(err) {
  return (
    err instanceof MacSrCancelledError ||
    err?.name === "MacSrCancelledError" ||
    /cancel|已取消/i.test(String(err?.message || ""))
  );
}

function syncMacCancelPanel(visible) {
  srMacCancelBtn?.classList.toggle("hidden", !visible);
}

function syncMacServerCustomVisibility() {
  if (!srServerPreset || !srServerCustom) return;
  const isCustom = srServerPreset.value === "__custom__";
  srServerCustom.classList.toggle("hidden", !isCustom);
  srServerCustom.disabled = !isCustom;
  if (isCustom) {
    requestAnimationFrame(() => {
      srServerCustom.focus();
    });
  }
}

function setMacFeedback(text, variant = "pending") {
  if (!srMacStatus) return;
  srMacStatus.textContent = text;
  srMacStatus.classList.remove("hidden", "is-pending", "is-ok", "is-error");
  srMacStatus.classList.add(
    variant === "ok" ? "is-ok" : variant === "error" ? "is-error" : "is-pending"
  );
  srMacStatus.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function updateMacStatus() {
  if (!srMacStatus) return;
  const macUrl = getMacServerUrl();
  if (macSrVerified && macUrl) {
    setMacFeedback(`Mac 已连通：${macUrl}，可点「Mac 真实感优化」`, "ok");
  }
}

function bindClearInput(input, clearBtn) {
  if (!input || !clearBtn) return () => {};
  const sync = () => clearBtn.classList.toggle("hidden", !input.value.trim());
  input.addEventListener("input", sync);
  clearBtn.addEventListener("click", () => {
    input.value = "";
    sync();
    input.focus();
  });
  sync();
  return sync;
}

const syncShareClear = bindClearInput(shareInput, shareClearBtn);

async function readClipboardText() {
  if (isNativePlatform()) {
    try {
      const Clipboard = getPlugin("Clipboard");
      const res = await Clipboard.read();
      return String(res?.value ?? res?.data ?? "").trim();
    } catch (err) {
      const msg = String(err?.message || err);
      if (/denied|permission|NotAllowed/i.test(msg)) {
        throw new Error("无法读取剪贴板，请在 设置 → 视频爬取 中允许粘贴");
      }
      throw new Error(msg || "读取剪贴板失败");
    }
  }
  if (!navigator.clipboard?.readText) {
    throw new Error("当前环境不支持读取剪贴板");
  }
  try {
    return String(await navigator.clipboard.readText()).trim();
  } catch (err) {
    const denied = /denied|permission|NotAllowed/i.test(String(err?.message || err));
    throw new Error(denied ? "无法读取剪贴板，请在系统设置中允许访问" : "读取剪贴板失败");
  }
}

async function pasteClipboardReplace(input, onSynced) {
  if (!input) return;
  const text = await readClipboardText();
  if (!text) throw new Error("剪贴板为空，请先在抖音等 App 复制分享文案");
  input.value = text;
  if (onSynced) onSynced();
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus();
}

sharePasteBtn?.addEventListener("click", async () => {
  try {
    await pasteClipboardReplace(shareInput, syncShareClear);
    setStatus("已从剪贴板粘贴", "ok");
  } catch (err) {
    setStatus(err.message || "粘贴失败", "error");
  }
});

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

function syncSrButtons(modelReady = nativeSrReady) {
  const macUrl = getMacServerUrl();
  const hasVideo = !!(lastVideoBuffer && lastFile);
  const showNative = hasVideo && isNativePlatform();
  const showMac = hasVideo && !!macUrl;

  srButtons?.classList.toggle("hidden", !showNative && !showMac);
  srNativeBtn?.classList.toggle("hidden", !showNative);
  srMacBtn?.classList.toggle("hidden", !showMac);

  if (srNativeBtn) {
    srNativeBtn.textContent = "本机真实感优化";
  }
  updateMacStatus();
}

function setSrBusy(busy) {
  srNativeBtn && (srNativeBtn.disabled = busy);
  srMacBtn && (srMacBtn.disabled = busy);
}

function currentSrProfile() {
  return "real";
}

async function refreshSrAvailability() {
  const profile = currentSrProfile();
  nativeSrReady = await isNativeSrAvailable();
  syncSrButtons(true);
}

async function initSrSettings() {
  srSettings = await loadSrSettings();
  applyMacServerUrl(srSettings.serverUrl || MAC_SERVER_PRESETS[0]);
  syncMacServerCustomVisibility();
  if (srScaleSelect) srScaleSelect.value = srSettings.outputScale;
  await refreshSrAvailability();
}

initSrSettings().catch(() => {});

async function ingestNativeSrResult(job) {
  if (!job || job.status !== "done" || !job.outputPath || srShareInProgress) return false;
  srShareInProgress = true;
  try {
    await saveNativeSrToLibrary(job);
    await clearNativeSrJob();
    syncNativeTaskPanel(null);
    hideSrProgress();
    await refreshSrLibraryUI();
    if (srLibraryPanel) srLibraryPanel.open = true;
    const w = job.outputWidth;
    const h = job.outputHeight;
    const sizeHint = w && h ? `（${w}×${h}）` : "";
    setStatus(`【本机】已加入优化成片库${sizeHint}，可点「存相册」`, "ok");
    return true;
  } catch (err) {
    setStatus(err.message || "入库失败", "error");
    return false;
  } finally {
    srShareInProgress = false;
  }
}

async function handlePendingSrJob() {
  if (!isNativePlatform()) return;
  const job = await getNativeSrJobStatus();
  if (job.status === "idle") {
    syncNativeTaskPanel(null);
    return;
  }
  if (job.status === "running") {
    syncNativeTaskPanel(job);
    const pct = Math.round((Number(job.progress) || 0) * 100);
    const msg = job.message || "后台优化进行中";
    if (!nativeSrStartedAt) nativeSrStartedAt = Date.now();
    const etaSec = etaFromProgressPct(pct, nativeSrStartedAt);
    const eta = formatSrEta(etaSec);
    showSrProgress(pct, msg, "【本机】", etaSec);
    setStatus(`【本机】${msg}${pct > 0 ? ` ${pct}%` : ""}${eta ? `，${eta}` : ""}`);
    return;
  }
  if (job.status === "failed") {
    hideSrProgress();
    syncNativeTaskPanel(job);
    setStatus(`【本机】${job.error || "后台优化失败"}，可点下方清除`, "error");
    return;
  }
  if (job.status === "cancelled") {
    hideSrProgress();
    syncNativeTaskPanel(null);
    return;
  }
  hideSrProgress();
  syncNativeTaskPanel(null);
  await ingestNativeSrResult(job);
}

async function ingestMacSrResult({ buffer, meta, filename }) {
  const srName = String(filename || "video.mp4").replace(/\.mp4$/i, "_sr.mp4");
  await saveMacSrToLibrary(buffer, srName, meta || {});
  await refreshSrLibraryUI();
  if (srLibraryPanel) srLibraryPanel.open = true;
  const outW = meta.output_width;
  const outH = meta.output_height;
  const sizeHint = outW && outH ? `（${outW}×${outH}）` : "";
  setStatus(`【Mac 后端】已加入优化成片库${sizeHint}，可点「存相册」`, "ok");
}

async function handlePendingMacSrJob() {
  if (macSrPollActive) return;
  const pending = await loadMacSrJob();
  if (!pending?.jobId || !pending?.serverUrl) return;

  let data;
  try {
    data = await fetchSuperResolutionJob(pending.serverUrl, pending.jobId);
  } catch {
    return;
  }

  if (data.status === "failed") {
    await clearMacSrJob();
    hideSrProgress();
    setStatus(`【Mac 后端】${data.error || "优化失败"}`, "error");
    return;
  }

  if (data.status === "cancelled") {
    await clearMacSrJob();
    hideSrProgress();
    return;
  }

  if (data.status === "completed") {
    macSrPollActive = true;
    setSrBusy(true);
    try {
      showSrProgress(98, "正在下载优化结果…", "【Mac 后端】");
      const outBuffer = await downloadSuperResolutionResult(pending.serverUrl, data.download_url);
      await clearMacSrJob();
      await ingestMacSrResult({
        buffer: outBuffer,
        meta: data.region || {},
        filename: pending.filename,
      });
    } catch (err) {
      setStatus(`【Mac 后端】${err.message || "下载失败"}`, "error");
    } finally {
      macSrPollActive = false;
      setSrBusy(false);
      hideSrProgress();
    }
    return;
  }

  if (data.status !== "running" && data.status !== "queued") return;

  macSrPollActive = true;
  setSrBusy(true);
  syncMacCancelPanel(true);
  const pct = data.progress_pct != null ? Number(data.progress_pct) : 5;
  const etaSec = data.eta_sec != null ? Number(data.eta_sec) : null;
  showSrProgress(pct, data.progress || "Mac 优化进行中", "【Mac 后端】", etaSec);
  setStatus(`【Mac 后端】${data.progress || "优化进行中"}（已恢复轮询）`);
  try {
    const result = await resumeMacSuperResolutionJob(pending, {
      onStatus: (text, progressPct, eta) => {
        setStatus(`【Mac 后端】${text}`);
        if (progressPct != null) {
          showSrProgress(progressPct, text.split("（")[0], "【Mac 后端】", eta);
        }
      },
      onProgress: (progressPct, message, eta) => {
        showSrProgress(progressPct, message, "【Mac 后端】", eta);
      },
    });
    await ingestMacSrResult({
      buffer: result.buffer,
      meta: result.meta,
      filename: result.filename || pending.filename,
    });
  } catch (err) {
    if (!isMacSrCancelled(err)) {
      setStatus(`【Mac 后端】${err.message || "优化失败"}`, "error");
    } else {
      setStatus("Mac 优化已取消", "ok");
    }
  } finally {
    macSrPollActive = false;
    setSrBusy(false);
    hideSrProgress();
  }
}

async function initSrBackground() {
  if (isNativePlatform()) {
    await requestSrNotifications();
  }
  try {
    const App = getPlugin("App");
    App.addListener("appStateChange", ({ isActive }) => {
      if (!isActive) return;
      handlePendingSrJob().catch(() => {});
      handlePendingMacSrJob().catch(() => {});
    });
  } catch {
    /* ignore */
  }
  await handlePendingSrJob();
  await handlePendingMacSrJob();
}

initSrBackground().catch(() => {});

srMacCancelBtn?.addEventListener("click", async () => {
  srMacCancelBtn.disabled = true;
  try {
    const pending = await loadMacSrJob();
    abortMacSrPoll();
    if (pending?.jobId && pending?.serverUrl) {
      await cancelMacSuperResolutionJob(pending.serverUrl, pending.jobId);
    } else {
      await clearMacSrJob();
    }
    macSrPollActive = false;
    setSrBusy(false);
    hideSrProgress();
    setStatus("Mac 优化已取消", "ok");
  } catch (err) {
    if (!isMacSrCancelled(err)) {
      setStatus(err.message || "取消失败", "error");
    } else {
      setStatus("Mac 优化已取消", "ok");
    }
  } finally {
    srMacCancelBtn.disabled = false;
  }
});

srNativeCancelBtn?.addEventListener("click", async () => {
  srNativeCancelBtn.disabled = true;
  try {
    await cancelNativeSrJob();
    hideSrProgress();
    nativeSrStartedAt = 0;
    syncNativeTaskPanel(null);
    setStatus("本机优化任务已取消并删除", "ok");
  } catch (err) {
    setStatus(err.message || "取消失败", "error");
  } finally {
    srNativeCancelBtn.disabled = false;
  }
});

function persistSrSettings() {
  saveSrSettings({
    serverUrl: getMacServerUrl(),
    outputScale: srScaleSelect?.value || "1080",
    srProfile: "real",
  }).catch(() => {});
}

function onMacServerPresetChange() {
  syncMacServerCustomVisibility();
  onMacServerChanged();
}

srServerPreset?.addEventListener("change", onMacServerPresetChange);
srServerPreset?.addEventListener("input", onMacServerPresetChange);

srServerCustom?.addEventListener("input", onMacServerChanged);

srScaleSelect?.addEventListener("change", persistSrSettings);

srTestBtn?.addEventListener("click", async () => {
  if (!srTestBtn) return;
  srTestBtn.disabled = true;
  const url = getMacServerUrl();
  if (!url) {
    setMacFeedback("请先选择地址，或在「自定义」里填写 http://192.168.x.x:8000", "error");
    srTestBtn.disabled = false;
    return;
  }
  setMacFeedback(`正在连接 ${url}…`, "pending");
  try {
    await saveSrSettings({ serverUrl: url, outputScale: srScaleSelect?.value || "1080" });
    await checkSrServer(url);
    macSrVerified = true;
    await refreshSrAvailability();
    setMacFeedback("Mac 后端连接正常，可点「Mac 真实感优化」", "ok");
    setStatus("Mac 后端连接正常", "ok");
  } catch (err) {
    macSrVerified = false;
    syncSrButtons();
    const msg = err.message || "连接失败";
    setMacFeedback(msg, "error");
    setStatus(msg, "error");
  } finally {
    srTestBtn.disabled = false;
  }
});

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
  lastVideoBuffer = null;

  try {
    await saveCookies({ douyinCookie: douyinCookieVal, xhsCookie: xhsCookieVal });
    const platformHint = /klingai/i.test(text)
      ? "可灵AI"
      : /mr\.baidu|mbd\.baidu|haokan\.baidu/i.test(text)
      ? "百度"
      : /douyin|iesdouyin/i.test(text)
      ? "抖音"
      : /kuaishou|chenzhongtech/i.test(text) && !/klingai/i.test(text)
        ? "快手"
        : /toutiao/i.test(text)
          ? "头条"
          : /xhslink|xiaohongshu/i.test(text)
            ? "小红书"
            : "";
    setStatus(
      platformHint
        ? `正在爬取${platformHint}（解析链接 → 获取直链 → 下载）…`
        : "正在爬取（解析链接 → 获取直链 → 下载）…"
    );

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
    lastVideoBuffer = result.buffer;

    revealResult(resultEl);

    await prepareVideoPreview({
      video: previewEl,
      stage: document.getElementById("video-stage"),
      stageBg: document.getElementById("video-stage-bg"),
      posterLayer: document.getElementById("video-poster-layer"),
      badge: document.getElementById("video-badge"),
      fsBtn: document.getElementById("fullscreen-btn"),
      videoSrc: saved.webPath,
      coverUrl: result.cover_url || "",
    });
    bindVideoAdjust(document.getElementById("video-stage"), previewEl);
    await refreshSrAvailability();

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
  } catch (err) {
    const msg = err?.message || String(err) || "爬取失败";
    console.error("[crawl]", err);
    setStatus(msg.includes("Capacitor") ? `${msg}（请 Xcode 重新 Run 安装最新版）` : msg, "error");
  } finally {
    submitBtn.disabled = false;
  }
});

saveBtn?.addEventListener("click", async () => {
  if (!lastFile?.uri || !lastVideoBuffer) {
    setStatus("请先爬取或上传视频", "error");
    return;
  }
  saveBtn.disabled = true;
  const settings = getEnhanceSettings();
  let shareUri = lastFile.uri;
  let shareName = lastFile.filename;

  try {
    let buffer = lastVideoBuffer;
    if (needsEnhanceExport(settings)) {
      const label =
        settings.preset === "cinema"
          ? "影院"
          : settings.preset === "sharp"
            ? "细节"
            : settings.preset === "clear"
              ? "清晰"
              : "自定义";
      setStatus(`正在应用${label}效果并写入文件…`);
      try {
        buffer = await exportEnhancedVideoBuffer(
          lastVideoBuffer,
          settings,
          (done, total) => {
            if (total > 0) {
              const pct = Math.min(100, Math.round((done / total) * 100));
              setStatus(`正在写入${label}效果… ${pct}%`);
            }
          }
        );
        const enhancedName = shareName.replace(/\.mp4$/i, "_enhanced.mp4");
        const enhanced = await saveVideoBuffer(buffer, enhancedName);
        shareUri = enhanced.uri;
        shareName = enhanced.filename;
      } catch (err) {
        console.warn("[enhance-export]", err);
        setStatus(`${err.message || "增强失败"}，将保存原片…`);
        buffer = lastVideoBuffer;
      }
    }

    setStatus("正在保存到相册…");
    const via = await shareVideoFile(shareUri, shareName, {
      fsPath: lastFile.path,
      directory: "CACHE",
    });
    if (via === "photos") {
      setStatus(needsEnhanceExport(settings) ? "已保存到相册（含画质调整）" : "已保存到相册", "ok");
    } else {
      setStatus("请在分享菜单中选择「储存视频」", "ok");
    }
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

async function runMacSuperResolution(serverUrl) {
  if (!serverUrl) throw new Error("请先在上方填写 Mac 后端地址");
  if (await hasActiveMacSrJob()) {
    throw new Error("已有 Mac 优化任务进行中");
  }
  macSrPollActive = true;
  syncMacCancelPanel(true);
  await saveSrSettings({
    serverUrl,
    outputScale: srScaleSelect?.value || "1080",
    srProfile: "real",
  });
  try {
    const result = await runSuperResolutionPipeline(
      serverUrl,
      lastVideoBuffer,
      lastFile.filename,
      {
        outputScale: srScaleSelect?.value || "1080",
        srProfile: "real",
        onStatus: (text, progressPct, etaSec) => {
          setStatus(`【Mac 后端】${text}`);
          if (progressPct != null) {
            showSrProgress(progressPct, text.split("（")[0], "【Mac 后端】", etaSec);
          }
        },
        onProgress: (progressPct, message, etaSec) => {
          showSrProgress(progressPct, message, "【Mac 后端】", etaSec);
        },
      }
    );
    await ingestMacSrResult({
      buffer: result.buffer,
      meta: result.meta,
      filename: lastFile.filename,
    });
  } catch (err) {
    if (isMacSrCancelled(err)) {
      setStatus("Mac 优化已取消", "ok");
    } else {
      throw err;
    }
  } finally {
    macSrPollActive = false;
    hideSrProgress();
  }
}

async function runNativeSuperResolution() {
  const srName = lastFile.filename.replace(/\.mp4$/i, "_sr.mp4");
  nativeSrStartedAt = Date.now();
  await startNativeSuperResolutionBackground(lastFile.uri, srName, {
    outputScale: srScaleSelect?.value || srSettings.outputScale || "1080",
    onProgress: (pct, msg) => {
      const etaSec = etaFromProgressPct(pct, nativeSrStartedAt);
      showSrProgress(pct, msg, "【本机】", etaSec);
    },
    onStatus: (text) => setStatus(`【本机】${text}`),
    onComplete: async (ev) => {
      if (ev?.status === "done") {
        await ingestNativeSrResult({
          status: "done",
          outputPath: ev.outputPath,
          outputWidth: ev.outputWidth,
          outputHeight: ev.outputHeight,
          displayFilename: ev.displayFilename || srName,
        });
      } else if (ev?.status === "cancelled") {
        hideSrProgress();
        syncNativeTaskPanel(null);
        setStatus("本机优化已取消", "ok");
      } else if (ev?.status === "failed") {
        syncNativeTaskPanel({
          status: "failed",
          error: ev.error,
          message: ev.error,
        });
        setStatus(`【本机】${ev.error || "后台优化失败"}，可点下方清除`, "error");
      }
    },
  });
  nativeSrReady = true;
  syncSrButtons(true);
  showSrProgress(5, "后台优化已启动", "【本机】");
  const job = await getNativeSrJobStatus();
  syncNativeTaskPanel(job.status !== "idle" ? job : null);
  setStatus(
    "【本机】已在后台开始优化，完成后会进入「优化成片库」。卡住可点「取消并删除本机任务」。",
    "ok"
  );
}

// Handle local video upload for super-resolution
uploadVideoBtn?.addEventListener("click", () => {
  if (!isNativePlatform()) {
    setStatus("请在 iOS App 内使用上传功能", "error");
    return;
  }
  videoFileInput?.click();
});

videoFileInput?.addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;

  // Reset input to allow selecting the same file again
  e.target.value = "";

  if (!file.type.startsWith("video/")) {
    setStatus("请选择视频文件", "error");
    return;
  }

  setSrBusy(true);
  try {
    setStatus("正在保存视频到应用目录…");
    
    console.log('File selected:', file.name, 'Size:', (file.size / 1024 / 1024).toFixed(2), 'MB');
    
    // Show progress for large files
    const fileSizeMB = file.size / 1024 / 1024;
    if (fileSizeMB > 10) {
      setStatus(`文件较大 (${fileSizeMB.toFixed(1)} MB)，正在处理…`);
    }
    
    // Convert File to ArrayBuffer
    const arrayBuffer = await file.arrayBuffer();
    console.log('ArrayBuffer created, size:', arrayBuffer.byteLength);
    
    // Save video file to app storage
    console.log('Calling saveVideoBuffer...');
    setStatus("正在写入文件（大文件可能需要几秒）…");
    const savedFile = await saveVideoBuffer(arrayBuffer, file.name);
    console.log('File saved successfully:', savedFile);
    
    lastFile = savedFile;
    lastVideoBuffer = arrayBuffer;

    revealResult(resultEl);
    setStatus("正在加载视频预览…");

    await prepareVideoPreview({
      video: previewEl,
      stage: document.getElementById("video-stage"),
      stageBg: document.getElementById("video-stage-bg"),
      posterLayer: document.getElementById("video-poster-layer"),
      badge: document.getElementById("video-badge"),
      fsBtn: document.getElementById("fullscreen-btn"),
      videoSrc: savedFile.webPath,
    });
    bindVideoAdjust(document.getElementById("video-stage"), previewEl);

    if (metaEl) {
      metaEl.textContent = [
        "来源：本地上传",
        `${formatMb(file.size)} MB`,
        file.type || "video/mp4",
      ].join(" · ");
    }

    await refreshSrAvailability();
    setStatus(`视频已加载：${savedFile.filename}，可预览或点「本机真实感优化」`, "ok");
  } catch (err) {
    console.error("Failed to upload video:", err);
    console.error("Error details:", err.message, err.stack);
    setStatus(`上传失败：${err.message || "未知错误"}`, "error");
  } finally {
    setSrBusy(false);
  }
});

srNativeBtn?.addEventListener("click", async () => {
  if (!lastVideoBuffer || !lastFile) {
    setStatus("请先爬取或上传视频", "error");
    return;
  }
  if (macSrPollActive || (await hasActiveMacSrJob())) {
    setStatus("Mac 优化进行中，请等待完成或取消后再开本机优化", "error");
    return;
  }
  if (!isNativePlatform()) {
    setStatus("当前环境不支持本机优化", "error");
    return;
  }
  setSrBusy(true);
  try {
    await runNativeSuperResolution();
  } catch (err) {
    setStatus(`【本机】${err.message || "本机优化失败"}`, "error");
  } finally {
    setSrBusy(false);
  }
});

srMacBtn?.addEventListener("click", async () => {
  if (!lastVideoBuffer || !lastFile) {
    setStatus("请先爬取或上传视频", "error");
    return;
  }
  if (macSrPollActive || (await hasActiveMacSrJob())) {
    setStatus("已有 Mac 优化任务进行中，请等待完成或点「取消 Mac 优化」", "error");
    syncMacCancelPanel(true);
    return;
  }
  const serverUrl = getMacServerUrl();
  if (!serverUrl) {
    setStatus("请先在「Mac 后端成片优化」里填写地址", "error");
    return;
  }
  setSrBusy(true);
  try {
    await runMacSuperResolution(serverUrl);
  } catch (err) {
    if (isMacSrCancelled(err)) {
      setStatus("Mac 优化已取消", "ok");
    } else if (err?.message?.includes("cancel") || err?.message?.includes("Cancel")) {
      setStatus("已取消保存", "error");
    } else {
      setStatus(`【Mac 后端】${err.message || "优化失败"}`, "error");
    }
  } finally {
    setSrBusy(false);
  }
});
