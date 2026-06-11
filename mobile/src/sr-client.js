/** Mac 后端 Real-ESRGAN 超分客户端（需同一 Wi‑Fi + ./start.sh） */
import { getPlugin } from "./crawl/capacitor-bridge.js";
import { downloadBinary, httpGet, parseJsonPayload } from "./crawl/http.js";
import { formatSrEta } from "./sr-eta.js";

const SERVER_KEY = "hongguo_sr_server";
const SCALE_KEY = "hongguo_sr_scale";
const MAC_JOB_KEY = "hongguo_mac_sr_job";

export function normalizeServerUrl(raw) {
  let s = String(raw || "").trim();
  if (!s) return "";
  if (!/^https?:\/\//i.test(s)) s = `http://${s}`;
  return s.replace(/\/+$/, "");
}

export async function loadSrSettings() {
  const Preferences = getPlugin("Preferences");
  const [server, scale] = await Promise.all([
    Preferences.get({ key: SERVER_KEY }),
    Preferences.get({ key: SCALE_KEY }),
  ]);
  return {
    serverUrl: normalizeServerUrl(server.value || ""),
    outputScale: scale.value === "1080" ? "1080" : "4k", // 默认为4k
  };
}

export async function saveMacSrJob(job) {
  const Preferences = getPlugin("Preferences");
  await Preferences.set({ key: MAC_JOB_KEY, value: JSON.stringify(job || {}) });
}

export async function loadMacSrJob() {
  const Preferences = getPlugin("Preferences");
  const res = await Preferences.get({ key: MAC_JOB_KEY });
  if (!res?.value) return null;
  try {
    const data = JSON.parse(res.value);
    return data?.jobId && data?.serverUrl ? data : null;
  } catch {
    return null;
  }
}

export async function clearMacSrJob() {
  const Preferences = getPlugin("Preferences");
  await Preferences.remove({ key: MAC_JOB_KEY });
}

export async function fetchSuperResolutionJob(baseUrl, jobId) {
  const root = normalizeServerUrl(baseUrl);
  const res = await httpGet(`${root}/api/tools/super-resolution/job/${encodeURIComponent(jobId)}`, {
    responseType: "json",
    readTimeout: 30000,
  });
  return parseJsonPayload(res.data);
}

export async function saveSrSettings({ serverUrl = "", outputScale = "1080" } = {}) {
  const Preferences = getPlugin("Preferences");
  await Promise.all([
    Preferences.set({ key: SERVER_KEY, value: normalizeServerUrl(serverUrl) }),
    Preferences.set({ key: SCALE_KEY, value: outputScale === "4k" ? "4k" : "1080" }),
  ]);
}

function assertReachableMacUrl(url) {
  if (/localhost|127\.0\.0\.1/i.test(url)) {
    throw new Error(
      "iPhone 不能填 localhost。请填 Mac 局域网 IP，例如 http://192.168.1.5:8000（终端运行 ./start.sh 后看本机 IP）"
    );
  }
}

export async function checkSrServer(baseUrl) {
  const url = normalizeServerUrl(baseUrl);
  if (!url) throw new Error("请先填写 Mac 后端地址");
  assertReachableMacUrl(url);
  let res;
  try {
    res = await httpGet(`${url}/api/tools/sr-status`, {
      responseType: "json",
      readTimeout: 15000,
      connectTimeout: 10000,
    });
  } catch (err) {
    const detail = String(err?.message || err || "连接失败");
    throw new Error(
      `无法连接 ${url}（${detail}）。请确认：① Mac 已运行 ./start.sh ② 同一 Wi‑Fi ③ 地址为 Mac 的 192.168.x.x ④ 设置 → 隐私与安全性 → 本地网络 → 允许「视频爬取」`
    );
  }
  const data = parseJsonPayload(res.data);
  if (!data.available) {
    throw new Error("Mac 后端未安装 Real-ESRGAN，请运行 ./start.sh 等待工具下载完成");
  }
  return data;
}

async function postMultipart(url, formData) {
  const resp = await fetch(url, { method: "POST", body: formData });
  let data = {};
  try {
    data = await resp.json();
  } catch {
    /* ignore */
  }
  if (!resp.ok) {
    const detail = data.detail || data.error || `HTTP ${resp.status}`;
    throw new Error(typeof detail === "string" ? detail : "上传失败");
  }
  return data;
}

export async function startSuperResolution(baseUrl, buffer, filename, { outputScale = "1080" } = {}) {
  const root = normalizeServerUrl(baseUrl);
  if (!root) throw new Error("请先填写 Mac 后端地址");
  const blob = new Blob([buffer], { type: "video/mp4" });
  const fd = new FormData();
  fd.append("file", blob, filename || "video.mp4");
  fd.append("output_scale", outputScale === "4k" ? "4k" : "1080");
  fd.append("output_fps", "native");
  return postMultipart(`${root}/api/tools/super-resolution`, fd);
}

export async function pollSuperResolutionJob(baseUrl, jobId, onTick) {
  const root = normalizeServerUrl(baseUrl);
  const started = Date.now();
  while (true) {
    const res = await httpGet(`${root}/api/tools/super-resolution/job/${encodeURIComponent(jobId)}`, {
      responseType: "json",
      readTimeout: 30000,
    });
    const data = parseJsonPayload(res.data);
    const elapsed = Math.floor((Date.now() - started) / 1000);
    const m = Math.floor(elapsed / 60);
    const s = elapsed % 60;
    const elapsedText = m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`;
    const progressPct =
      data.progress_pct != null && !Number.isNaN(Number(data.progress_pct))
        ? Number(data.progress_pct)
        : null;
    const etaSec =
      data.eta_sec != null && !Number.isNaN(Number(data.eta_sec)) ? Number(data.eta_sec) : null;
    const etaText = formatSrEta(etaSec);
    if (onTick) {
      onTick({
        status: data.status,
        progress: data.progress || "处理中…",
        progressPct,
        etaSec,
        etaText,
        elapsedText,
        data,
      });
    }
    if (data.status === "completed") return data;
    if (data.status === "failed") {
      throw new Error(data.error || "AI 超分失败");
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
}

export async function downloadSuperResolutionResult(baseUrl, downloadPath) {
  const root = normalizeServerUrl(baseUrl);
  const path = String(downloadPath || "");
  const url = path.startsWith("http") ? path : `${root}${path.startsWith("/") ? path : `/${path}`}`;
  const { buffer } = await downloadBinary(url, {}, null);
  return buffer;
}

export async function runSuperResolutionPipeline(
  baseUrl,
  buffer,
  filename,
  { outputScale = "1080", onStatus, onProgress } = {}
) {
  if (onStatus) onStatus("正在上传到 Mac…", 2);
  if (onProgress) onProgress(2, "正在上传到 Mac…");
  const { job_id: jobId } = await startSuperResolution(baseUrl, buffer, filename, { outputScale });
  if (!jobId) throw new Error("未获得任务 ID");

  await saveMacSrJob({
    jobId,
    serverUrl: normalizeServerUrl(baseUrl),
    filename,
    outputScale,
    startedAt: Date.now(),
  });

  const result = await pollSuperResolutionJob(baseUrl, jobId, ({ progress, progressPct, elapsedText, etaSec, etaText }) => {
    const pct = progressPct != null ? progressPct : null;
    const etaPart = etaText ? `，${etaText}` : "";
    const label = `${progress}（已等待 ${elapsedText}${etaPart}）`;
    if (onProgress && pct != null) onProgress(pct, progress, etaSec);
    if (onStatus) onStatus(label, pct, etaSec);
  });

  if (onStatus) onStatus("正在下载超分结果…", 98);
  if (onProgress) onProgress(98, "正在下载超分结果…");
  const outBuffer = await downloadSuperResolutionResult(baseUrl, result.download_url);
  await clearMacSrJob();
  return { buffer: outBuffer, meta: result.region || {}, jobId };
}

/** 恢复被中断的 Mac 超分轮询（切后台后回到 App） */
export async function resumeMacSuperResolutionJob(pending, { onStatus, onProgress } = {}) {
  const { jobId, serverUrl, filename, outputScale = "1080" } = pending;
  const result = await pollSuperResolutionJob(serverUrl, jobId, ({ progress, progressPct, elapsedText, etaSec, etaText }) => {
    const pct = progressPct != null ? progressPct : null;
    const etaPart = etaText ? `，${etaText}` : "";
    const label = `${progress}（已等待 ${elapsedText}${etaPart}）`;
    if (onProgress && pct != null) onProgress(pct, progress, etaSec);
    if (onStatus) onStatus(label, pct, etaSec);
  });
  if (onStatus) onStatus("正在下载超分结果…", 98);
  if (onProgress) onProgress(98, "正在下载超分结果…");
  const outBuffer = await downloadSuperResolutionResult(serverUrl, result.download_url);
  await clearMacSrJob();
  return { buffer: outBuffer, meta: result.region || {}, jobId, filename, outputScale };
}
