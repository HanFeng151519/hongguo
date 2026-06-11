import { Directory, getCapacitor, getPlugin, isNativePlatform } from "./capacitor-bridge.js";

const COOKIE_KEYS = {
  douyin: "hongguo_dy_cookie",
  xhs: "hongguo_xhs_cookie",
};

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  const chunk = 0x2000;
  let binary = "";
  for (let i = 0; i < bytes.length; i += chunk) {
    const slice = bytes.subarray(i, Math.min(i + chunk, bytes.length));
    binary += String.fromCharCode.apply(null, slice);
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
  const base64 = arrayBufferToBase64(buffer);

  await Filesystem.writeFile({
    path,
    data: base64,
    directory: Directory.Cache,
    recursive: true,
  });

  const { uri } = await Filesystem.getUri({
    path,
    directory: Directory.Cache,
  });

  const webPath = Capacitor.convertFileSrc(uri);
  return { path, uri, webPath, filename: name };
}

export async function shareVideoFile(uri, title = "保存视频") {
  if (!isNativePlatform()) {
    throw new Error("请在 iOS App 内使用保存功能");
  }
  const Share = getPlugin("Share");
  await Share.share({
    title,
    files: [uri],
  });
}

export async function deleteCachedPath(path) {
  try {
    const Filesystem = getPlugin("Filesystem");
    await Filesystem.deleteFile({ path, directory: Directory.Cache });
  } catch {
    /* ignore */
  }
}
