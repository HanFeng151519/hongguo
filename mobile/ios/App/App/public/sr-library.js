/** 超分成片库：Mac / 本机完成后入库，可保存到相册或删除 */
import { Directory, getCapacitor, getPlugin } from "./crawl/capacitor-bridge.js";
import { deleteCachedPath, shareVideoFile } from "./crawl/storage.js";

const LIBRARY_KEY = "hongguo_sr_library";
const MAX_ITEMS = 30;

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
  const entry = {
    id: item.id || `sr_${Date.now()}`,
    filename: item.filename || "video_sr.mp4",
    uri: item.uri,
    fsPath: item.fsPath || "",
    directory: item.directory || "DATA",
    backend: item.backend === "native" ? "native" : "mac",
    width: Number(item.width) || 0,
    height: Number(item.height) || 0,
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

export async function saveNativeSrToLibrary(job) {
  const outputPath = job.outputPath || job.outputUri;
  if (!outputPath) throw new Error("无输出文件");
  const name = safeName(job.displayFilename || "video_sr.mp4");
  return addSrLibraryItem({
    filename: name,
    uri: outputPath,
    fsPath: "",
    directory: "DATA",
    backend: "native",
    width: job.outputWidth,
    height: job.outputHeight,
  });
}

export async function shareSrLibraryItem(item) {
  await shareVideoFile(item.uri, item.filename || "保存视频");
}

export async function deleteSrLibraryItem(id) {
  await removeSrLibraryItem(id);
}
