/** 超分成片库：Mac / 本机完成后入库，可保存到相册或删除 */
import { Directory, getCapacitor, getPlugin } from "./crawl/capacitor-bridge.js";
import { deleteCachedPath, shareVideoFile } from "./crawl/storage.js";

const LIBRARY_KEY = "hongguo_sr_library";
const MAX_ITEMS = 30;

/**
 * 从视频生成缩略图（base64 data URL）
 * @param {string} videoUri - 视频URI
 * @param {number} [maxWidth=120] - 最大宽度
 * @returns {Promise<string>} base64 data URL
 */
function generateVideoThumbnail(videoUri, maxWidth = 120) {
  return new Promise((resolve, reject) => {
    console.log('Generating thumbnail for:', videoUri);
    
    const video = document.createElement('video');
    video.crossOrigin = 'anonymous';
    video.preload = 'metadata';
    video.muted = true; // Required for autoplay
    
    const timeout = setTimeout(() => {
      console.warn('Thumbnail generation timeout for:', videoUri);
      video.src = '';
      video.load();
      resolve(''); // Return empty string on timeout
    }, 5000);
    
    video.onloadeddata = () => {
      console.log('Video loaded, seeking to first frame');
      // Seek to first frame (currentTime = 0)
      video.currentTime = 0;
    };
    
    video.onseeked = () => {
      clearTimeout(timeout);
      try {
        const canvas = document.createElement('canvas');
        const scale = maxWidth / video.videoWidth;
        canvas.width = maxWidth;
        canvas.height = Math.round(video.videoHeight * scale);
        
        const ctx = canvas.getContext('2d');
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        
        // Convert to JPEG with quality 0.7
        const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
        console.log('Thumbnail generated, length:', dataUrl.length);
        resolve(dataUrl);
      } catch (e) {
        console.error('Failed to generate thumbnail:', e);
        reject(e);
      }
      
      // Clean up
      video.src = '';
      video.load();
    };
    
    video.onerror = (e) => {
      clearTimeout(timeout);
      console.error('Video load error:', e, videoUri);
      reject(new Error('Failed to load video for thumbnail'));
    };
    
    video.src = videoUri;
  });
}

/**
 * 格式化文件大小
 * @param {number} bytes - 字节数
 * @returns {string} 格式化后的大小字符串
 */
export function formatFileSize(bytes) {
  if (!bytes || bytes === 0) return '--';
  const mb = bytes / (1024 * 1024);
  if (mb >= 100) {
    return `${Math.round(mb)} MB`;
  } else if (mb >= 1) {
    return `${mb.toFixed(1)} MB`;
  } else {
    const kb = bytes / 1024;
    return `${kb.toFixed(0)} KB`;
  }
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
