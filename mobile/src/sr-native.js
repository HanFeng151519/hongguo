/** iOS 本机 Core ML Real-ESRGAN（输出统一 1080×1920，支持后台处理） */
import { getPlugin, isNativePlatform } from "./crawl/capacitor-bridge.js";

function getSrPlugin() {
  return getPlugin("RealEsrgan");
}

export async function isNativeSrAvailable() {
  if (!isNativePlatform()) return false;
  try {
    const res = await getSrPlugin().isAvailable();
    return !!(res?.available || res?.downloadable);
  } catch {
    return false;
  }
}

export async function isNativeSrModelReady() {
  if (!isNativePlatform()) return false;
  try {
    const res = await getSrPlugin().isAvailable();
    return !!res?.available;
  } catch {
    return false;
  }
}

export async function requestSrNotifications() {
  if (!isNativePlatform()) return false;
  try {
    const res = await getSrPlugin().requestNotificationPermission();
    return !!res?.granted;
  } catch {
    return false;
  }
}

export async function getNativeSrJobStatus() {
  if (!isNativePlatform()) return { status: "idle" };
  try {
    return await getSrPlugin().getBackgroundJobStatus();
  } catch {
    return { status: "idle" };
  }
}

export async function clearNativeSrJob() {
  if (!isNativePlatform()) return;
  try {
    await getSrPlugin().clearBackgroundJob();
  } catch {
    /* ignore */
  }
}

/**
 * 后台超分：立即返回，完成后系统通知；进度/完成通过回调或 getNativeSrJobStatus 查询。
 */
export async function startNativeSuperResolutionBackground(
  inputUri,
  displayFilename,
  { onStatus, onProgress, onComplete } = {}
) {
  if (!isNativePlatform()) {
    throw new Error("本机 AI 超分仅支持 iOS App");
  }
  const plugin = getSrPlugin();
  const listeners = [];

  if (onStatus || onProgress) {
    listeners.push(
      await plugin.addListener("progress", (ev) => {
        const pct = Math.round((Number(ev?.progress) || 0) * 100);
        const msg = ev?.message || "Real-ESRGAN 处理中…";
        if (onProgress) onProgress(pct, msg);
        if (onStatus) onStatus(`${msg}${pct > 0 ? ` ${pct}%` : ""}`);
      })
    );
  }
  if (onComplete) {
    listeners.push(
      await plugin.addListener("jobComplete", (ev) => {
        onComplete(ev);
      })
    );
  }

  try {
    await plugin.prepareModel();
    const result = await plugin.startSuperResolveInBackground({
      inputPath: inputUri,
      displayFilename,
    });
    return result;
  } finally {
    // 后台任务继续；仅在前台时保留进度监听，完成回调仍有效
  }
}

export async function runNativeSuperResolution(inputUri, { onStatus } = {}) {
  if (!isNativePlatform()) {
    throw new Error("本机 AI 超分仅支持 iOS App");
  }
  const plugin = getSrPlugin();
  let listener = null;
  if (onStatus) {
    listener = await plugin.addListener("progress", (ev) => {
      const pct = Math.round((Number(ev?.progress) || 0) * 100);
      const msg = ev?.message || "Real-ESRGAN 处理中…";
      onStatus(`${msg}${pct > 0 ? ` ${pct}%` : ""}`);
    });
  }
  try {
    await plugin.prepareModel();
    if (onStatus) onStatus("Real-ESRGAN 本机超分启动…");
    const result = await plugin.superResolveVideo({ inputPath: inputUri });
    return result;
  } finally {
    if (listener) {
      try {
        await listener.remove();
      } catch {
        /* ignore */
      }
    }
  }
}

export async function shareNativeOutput(outputPath, title = "保存视频") {
  const Share = getPlugin("Share");
  await Share.share({
    title,
    files: [outputPath],
  });
}
