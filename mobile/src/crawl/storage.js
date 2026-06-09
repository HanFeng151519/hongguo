import { Capacitor } from "@capacitor/core";
import { Directory, Filesystem } from "@capacitor/filesystem";
import { Preferences } from "@capacitor/preferences";
import { Share } from "@capacitor/share";

const COOKIE_KEYS = {
  douyin: "hongguo_dy_cookie",
  xhs: "hongguo_xhs_cookie",
};

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

export async function loadCookies() {
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
  await Promise.all([
    Preferences.set({ key: COOKIE_KEYS.douyin, value: douyinCookie }),
    Preferences.set({ key: COOKIE_KEYS.xhs, value: xhsCookie }),
  ]);
}

export async function saveVideoBuffer(buffer, filename) {
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
  if (!Capacitor.isNativePlatform()) {
    throw new Error("请在 iOS App 内使用保存功能");
  }
  await Share.share({
    title,
    files: [uri],
  });
}

export async function deleteCachedPath(path) {
  try {
    await Filesystem.deleteFile({ path, directory: Directory.Cache });
  } catch {
    /* ignore */
  }
}
