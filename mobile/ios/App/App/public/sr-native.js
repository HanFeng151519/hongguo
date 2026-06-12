/** iOS 本机真实感优化（Core Image：降噪 · 调色 · 轻锐化，无 AI 模型） */
import { getPlugin, isNativePlatform } from "./crawl/capacitor-bridge.js";

function getSrPlugin() {
  return getPlugin("RealEsrgan");
}

export async function isNativeSrAvailable() {
  return isNativePlatform();
}

export async function isNativeSrModelReady() {
  return isNativePlatform();
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

export async function cancelNativeSrJob() {
  if (!isNativePlatform()) return;
  try {
    const plugin = getSrPlugin();
    if (plugin.cancelBackgroundJob) {
      await plugin.cancelBackgroundJob();
    } else {
      await plugin.clearBackgroundJob();
    }
  } catch {
    /* ignore */
  }
}

/**
 * 后台真实感优化：立即返回，完成后系统通知。
 */
export async function startNativeSuperResolutionBackground(
  inputUri,
  displayFilename,
  { onStatus, onProgress, onComplete, outputScale = "1080" } = {}
) {
  if (!isNativePlatform()) {
    throw new Error("本机优化仅支持 iOS App");
  }
  const plugin = getSrPlugin();
  const listeners = [];

  if (onStatus || onProgress) {
    listeners.push(
      await plugin.addListener("progress", (ev) => {
        const pct = Math.round((Number(ev?.progress) || 0) * 100);
        const msg = ev?.message || "真实感优化中…";
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
    const result = await plugin.startSuperResolveInBackground({
      inputPath: inputUri,
      displayFilename,
      outputScale,
      srProfile: "real",
    });
    return result;
  } finally {
    /* 后台任务继续 */
  }
}

export async function runNativeSuperResolution(inputUri, { onStatus } = {}) {
  if (!isNativePlatform()) {
    throw new Error("本机优化仅支持 iOS App");
  }
  const plugin = getSrPlugin();
  let listener = null;
  if (onStatus) {
    listener = await plugin.addListener("progress", (ev) => {
      const pct = Math.round((Number(ev?.progress) || 0) * 100);
      const msg = ev?.message || "真实感优化中…";
      onStatus(`${msg}${pct > 0 ? ` ${pct}%` : ""}`);
    });
  }
  try {
    if (onStatus) onStatus("本机真实感优化启动…");
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
