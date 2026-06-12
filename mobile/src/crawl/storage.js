import { Directory, getCapacitor, getPlugin, isNativePlatform } from "./capacitor-bridge.js";

const COOKIE_KEYS = {
  douyin: "hongguo_dy_cookie",
  xhs: "hongguo_xhs_cookie",
};

function arrayBufferToBase64(buffer) {
  // Use more efficient chunk size for large files
  const bytes = new Uint8Array(buffer);
  let binary = '';
  
  // Process in larger chunks for better performance
  const chunkSize = 0x8000; // 32KB chunks (was 8KB)
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const end = Math.min(i + chunkSize, bytes.length);
    // Use a more efficient way to build the string
    const chunk = bytes.subarray(i, end);
    binary += String.fromCharCode.apply(null, chunk);
  }
  
  return btoa(binary);
}

export async function loadCookies() {
  const Preferences = getPlugin("Preferences");
  const [dy, xhs] = await Promise.all([
    Preferences.get({ key: COOKIE_KEYS.douyin }),
    Preferences.get({ key: COOKIE_KEYS.xhs }),
  ]);
  return {
    douyinCookie: dy.value || "",
    xhsCookie: xhs.value || "",
  };
}

export async function saveCookies({ douyinCookie = "", xhsCookie = "" }) {
  const Preferences = getPlugin("Preferences");
  await Promise.all([
    Preferences.set({ key: COOKIE_KEYS.douyin, value: douyinCookie }),
    Preferences.set({ key: COOKIE_KEYS.xhs, value: xhsCookie }),
  ]);
}

export async function saveVideoBuffer(buffer, filename) {
  const Capacitor = getCapacitor();
  const Filesystem = getPlugin("Filesystem");
  const safe = String(filename || "video.mp4").replace(/[/\\]/g, "_");
  const name = safe.endsWith(".mp4") ? safe : `${safe}.mp4`;
  const path = `crawl/${Date.now()}_${name}`;
  
  console.log('saveVideoBuffer: converting to base64, buffer size:', buffer.byteLength);
  const startTime = Date.now();
  const base64 = arrayBufferToBase64(buffer);
  const convertTime = Date.now() - startTime;
  console.log(`saveVideoBuffer: base64 conversion took ${convertTime}ms, length:`, base64.length);

  console.log('saveVideoBuffer: writing file to', path);
  const writeStart = Date.now();
  await Filesystem.writeFile({
    path,
    data: base64,
    directory: Directory.Cache,
    recursive: true,
  });
  const writeTime = Date.now() - writeStart;
  console.log(`saveVideoBuffer: file write took ${writeTime}ms`);

  console.log('saveVideoBuffer: getting URI for', path);
  const { uri } = await Filesystem.getUri({
    path,
    directory: Directory.Cache,
  });
  console.log('saveVideoBuffer: URI obtained:', uri);

  const webPath = Capacitor.convertFileSrc(uri);
  return { path, uri, webPath, filename: name };
}

export async function resolveVideoFileUri(uri, { fsPath, directory = "DATA" } = {}) {
  if (fsPath) {
    const Filesystem = getPlugin("Filesystem");
    const dir = directory === "CACHE" ? Directory.Cache : Directory.Data;
    const { uri: resolved } = await Filesystem.getUri({ path: fsPath, directory: dir });
    if (resolved) {
      const stat = await Filesystem.stat({ path: fsPath, directory: dir }).catch(() => null);
      if (!stat?.size) {
        throw new Error("视频文件不存在或已损坏");
      }
      return resolved;
    }
  }
  if (!uri) {
    throw new Error("视频路径无效");
  }
  return uri;
}

/** @returns {"photos"|"share"} */
export async function shareVideoFile(uri, title = "保存视频", { fsPath, directory = "DATA" } = {}) {
  if (!isNativePlatform()) {
    throw new Error("请在 iOS App 内使用保存功能");
  }

  const videoPath = await resolveVideoFileUri(uri, { fsPath, directory });

  const RealEsrgan = getPlugin("RealEsrgan");
  if (RealEsrgan?.saveToPhotos) {
    try {
      await RealEsrgan.saveToPhotos({ videoPath });
      return "photos";
    } catch (e) {
      const msg = String(e?.message || e || "");
      if (/权限|permission|denied/i.test(msg)) {
        throw new Error(msg);
      }
      console.warn("saveToPhotos failed, falling back to Share:", msg);
    }
  }

  const Share = getPlugin("Share");
  await Share.share({
    title,
    files: [videoPath],
    dialogTitle: title,
  });
  return "share";
}

export async function deleteCachedPath(path) {
  try {
    const Filesystem = getPlugin("Filesystem");
    await Filesystem.deleteFile({ path, directory: Directory.Cache });
  } catch {
    /* ignore */
  }
}
