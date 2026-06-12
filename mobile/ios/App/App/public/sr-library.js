/** 优化成片库：Mac / 本机真实感优化完成后入库 */
import { Directory, getCapacitor, getPlugin } from "./crawl/capacitor-bridge.js";
import { deleteCachedPath, shareVideoFile } from "./crawl/storage.js";

const LIBRARY_KEY = "hongguo_sr_library";
const MAX_ITEMS = 30;

function videoWebSrc(uri) {
  if (!uri) return "";
  const Capacitor = getCapacitor();
  if (
    uri.startsWith("http://") ||
    uri.startsWith("https://") ||
    uri.startsWith("capacitor://") ||
    uri.startsWith("/_capacitor_file_")
  ) {
    return uri;
  }
  return Capacitor.convertFileSrc(uri);
}

/**
 * 从视频生成缩略图（base64 data URL）
 * iOS 必须用 convertFileSrc，不能直接喂 file://
 */
function generateVideoThumbnail(videoUri, maxWidth = 120) {
  return new Promise((resolve) => {
    const src = videoWebSrc(videoUri);
    if (!src) {
      resolve("");
      return;
    }

    const video = document.createElement("video");
    video.preload = "auto";
    video.muted = true;
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.setAttribute("webkit-playsinline", "");

    const cleanup = () => {
      video.onloadeddata = null;
      video.onseeked = null;
      video.onerror = null;
      video.removeAttribute("src");
      video.load();
    };

    const timeout = setTimeout(() => {
      cleanup();
      resolve("");
    }, 8000);

    video.onloadeddata = () => {
      const t = Math.min(0.5, Math.max(0, (video.duration || 1) * 0.05));
      video.currentTime = Number.isFinite(t) ? t : 0;
    };

    video.onseeked = () => {
      clearTimeout(timeout);
      try {
        const vw = video.videoWidth || maxWidth;
        const vh = video.videoHeight || Math.round(maxWidth * 16 / 9);
        const scale = maxWidth / vw;
        const canvas = document.createElement("canvas");
        canvas.width = maxWidth;
        canvas.height = Math.max(1, Math.round(vh * scale));
        const ctx = canvas.getContext("2d");
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL("image/jpeg", 0.72));
      } catch {
        resolve("");
      }
      cleanup();
    };

    video.onerror = () => {
      clearTimeout(timeout);
      cleanup();
      resolve("");
    };

    video.src = src;
    video.load();
  });
}

/**
 * 格式化文件大小
 * @param {number} bytes - 字节数
 * @returns {string} 格式化后的大小字符串
 */
export function formatFileSize(bytes) {
  if (!bytes || bytes <= 0) return "";
  const mb = bytes / (1024 * 1024);
  if (mb >= 100) return `${Math.round(mb)} MB`;
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  return `${(bytes / 1024).toFixed(0)} KB`;
}

async function statLibraryFile(item) {
  if (!item?.fsPath) return 0;
  try {
    const Filesystem = getPlugin("Filesystem");
    const dir = item.directory === "CACHE" ? Directory.Cache : Directory.Data;
    const stat = await Filesystem.stat({ path: item.fsPath, directory: dir });
    return Number(stat.size) || 0;
  } catch {
    return 0;
  }
}

/** 补全缺失的缩略图与文件大小，并写回 Preferences */
export async function hydrateSrLibraryItem(item) {
  let fileSize = Number(item.fileSize) || 0;
  let thumbnail = item.thumbnail || "";
  let changed = false;

  if (!fileSize) {
    const size = await statLibraryFile(item);
    if (size > 0) {
      fileSize = size;
      changed = true;
    }
  }

  if ((!thumbnail || thumbnail.length < 100) && item.uri) {
    const nextThumb = await generateVideoThumbnail(item.uri);
    if (nextThumb && nextThumb.length > 100) {
      thumbnail = nextThumb;
      changed = true;
    }
  }

  if (!changed) return item;

  const list = await loadSrLibrary();
  const next = list.map((x) =>
    x.id === item.id ? { ...x, fileSize, thumbnail } : x
  );
  await saveSrLibrary(next);
  return { ...item, fileSize, thumbnail };
}

export async function hydrateSrLibrary(items) {
  const out = [];
  for (const item of items) {
    out.push(await hydrateSrLibraryItem(item));
  }
  return out;
}

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

function safeName(name) {
  return String(name || "video_sr.mp4").replace(/[/\\]/g, "_");
}

export async function loadSrLibrary() {
  const Preferences = getPlugin("Preferences");
  const res = await Preferences.get({ key: LIBRARY_KEY });
  if (!res?.value) return [];
  try {
    const list = JSON.parse(res.value);
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

async function saveSrLibrary(list) {
  const Preferences = getPlugin("Preferences");
  await Preferences.set({ key: LIBRARY_KEY, value: JSON.stringify(list) });
}

export async function addSrLibraryItem(item) {
  const list = await loadSrLibrary();
  
  // Get file size
  let fileSize = item.fileSize || 0;
  if (!fileSize && item.fsPath) {
    try {
      const Filesystem = getPlugin("Filesystem");
      const dir = item.directory === "CACHE" ? Directory.Cache : Directory.Data;
      const stat = await Filesystem.stat({ path: item.fsPath, directory: dir });
      fileSize = stat.size || 0;
    } catch (e) {
      console.warn("Failed to get file size:", e);
    }
  }
  
  // Generate thumbnail if not provided
  let thumbnail = item.thumbnail || "";
  if (!thumbnail && item.uri) {
    try {
      thumbnail = await generateVideoThumbnail(item.uri);
    } catch (e) {
      console.warn("Failed to generate thumbnail:", e);
    }
  }
  
  const entry = {
    id: item.id || `sr_${Date.now()}`,
    filename: item.filename || "video_sr.mp4",
    uri: item.uri,
    fsPath: item.fsPath || "",
    directory: item.directory || "DATA",
    backend: item.backend === "native" ? "native" : "mac",
    width: Number(item.width) || 0,
    height: Number(item.height) || 0,
    fileSize: fileSize,
    thumbnail: thumbnail,
    createdAt: item.createdAt || Date.now(),
  };
  const next = [entry, ...list.filter((x) => x.id !== entry.id)].slice(0, MAX_ITEMS);
  await saveSrLibrary(next);
  return entry;
}

export async function removeSrLibraryItem(id) {
  const list = await loadSrLibrary();
  const target = list.find((x) => x.id === id);
  const next = list.filter((x) => x.id !== id);
  await saveSrLibrary(next);
  if (target?.fsPath) {
    try {
      const Filesystem = getPlugin("Filesystem");
      const dir = target.directory === "CACHE" ? Directory.Cache : Directory.Data;
      await Filesystem.deleteFile({ path: target.fsPath, directory: dir });
    } catch {
      /* ignore */
    }
  }
  return target;
}

export async function saveMacSrToLibrary(buffer, filename, meta = {}) {
  const Filesystem = getPlugin("Filesystem");
  const Capacitor = getCapacitor();
  const name = safeName(filename).replace(/\.mp4$/i, "_sr.mp4");
  const fsPath = `sr_library/${Date.now()}_${name}`;
  await Filesystem.writeFile({
    path: fsPath,
    data: arrayBufferToBase64(buffer),
    directory: Directory.Data,
    recursive: true,
  });
  const { uri } = await Filesystem.getUri({ path: fsPath, directory: Directory.Data });
  return addSrLibraryItem({
    filename: name,
    uri,
    fsPath,
    directory: "DATA",
    backend: "mac",
    width: meta.output_width || meta.outputWidth,
    height: meta.output_height || meta.outputHeight,
  });
}

function parseDocumentsRelativePath(uri) {
  const decoded = decodeURIComponent(String(uri || ""));
  const match = decoded.match(/\/Documents\/(.+)$/);
  return match ? match[1] : "";
}

async function readNativeOutputAsBase64(outputPath) {
  const Filesystem = getPlugin("Filesystem");
  const rel = parseDocumentsRelativePath(outputPath);
  if (rel) {
    try {
      const { data } = await Filesystem.readFile({
        path: rel,
        directory: Directory.Documents,
      });
      if (data) return { data, sourcePath: rel };
    } catch (e) {
      console.warn("[sr-library] read Documents failed:", e?.message || e);
    }
  }

  const Capacitor = getCapacitor();
  const fetchUrl =
    outputPath.startsWith("http://") ||
    outputPath.startsWith("https://") ||
    outputPath.startsWith("file://") ||
    outputPath.startsWith("capacitor://")
      ? outputPath
      : Capacitor.convertFileSrc(outputPath);
  const res = await fetch(fetchUrl);
  if (!res.ok) {
    throw new Error("无法读取本机优化视频（文件可能已被删除）");
  }
  const buffer = await res.arrayBuffer();
  return { data: arrayBufferToBase64(buffer), sourcePath: rel };
}

export async function saveNativeSrToLibrary(job) {
  const outputPath = job.outputPath || job.outputUri;
  if (!outputPath) throw new Error("无输出文件");
  const name = safeName(job.displayFilename || "video_sr.mp4").replace(/\.mp4$/i, "_sr.mp4");
  const { data: base64, sourcePath } = await readNativeOutputAsBase64(outputPath);

  const Filesystem = getPlugin("Filesystem");
  const fsPath = `sr_library/${Date.now()}_${name}`;
  await Filesystem.writeFile({
    path: fsPath,
    data: base64,
    directory: Directory.Data,
    recursive: true,
  });
  const { uri } = await Filesystem.getUri({ path: fsPath, directory: Directory.Data });

  if (sourcePath) {
    try {
      await Filesystem.deleteFile({ path: sourcePath, directory: Directory.Documents });
    } catch {
      /* ignore */
    }
  }

  return addSrLibraryItem({
    filename: name,
    uri,
    fsPath,
    directory: "DATA",
    backend: "native",
    width: job.outputWidth,
    height: job.outputHeight,
  });
}

export async function shareSrLibraryItem(item) {
  if (!item?.fsPath && !item?.uri) {
    throw new Error("视频记录无效，请删除后重新优化");
  }
  return shareVideoFile(item.uri, item.filename || "保存视频", {
    fsPath: item.fsPath,
    directory: item.directory || "DATA",
  });
}

export async function deleteSrLibraryItem(id) {
  await removeSrLibraryItem(id);
}
