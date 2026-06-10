/** iOS/Android：优先 CapacitorHttp 原生请求；浏览器用 fetch。 */
import { getPlugin, isNativePlatform } from "./capacitor-bridge.js";

function normalizeHeaders(headers = {}) {
  const out = {};
  for (const [k, v] of Object.entries(headers)) {
    if (v != null && v !== "") out[k] = String(v);
  }
  return out;
}

function base64ToArrayBuffer(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function parseJsonPayload(data) {
  if (data == null) return {};
  if (typeof data === "object") return data;
  if (typeof data === "string") {
    const s = data.trim();
    if (!s) return {};
    try {
      return JSON.parse(s);
    } catch {
      return {};
    }
  }
  return {};
}

function normalizeNativeUrl(url, fallback) {
  const u = String(url || fallback || "");
  if (!u || u.includes("_capacitor_http_interceptor")) return fallback || u;
  return u;
}

async function nativeRequest(url, options = {}) {
  const CapacitorHttp = getPlugin("CapacitorHttp");
  const responseType = options.responseType || "text";
  const method = (options.method || "GET").toUpperCase();
  const req = {
    url,
    method,
    headers: normalizeHeaders(options.headers),
    responseType,
    connectTimeout: options.connectTimeout ?? 60000,
    readTimeout: options.readTimeout ?? 120000,
  };
  const res =
    method === "GET"
      ? await CapacitorHttp.get(req)
      : await CapacitorHttp.request(req);
  if (res.status >= 400) {
    throw new Error(`HTTP ${res.status}`);
  }
  let data = res.data;
  if (responseType === "arraybuffer" && typeof data === "string") {
    data = base64ToArrayBuffer(data);
  } else if (responseType === "json") {
    data = parseJsonPayload(data);
  }
  return {
    status: res.status,
    url: normalizeNativeUrl(res.url, url),
    headers: res.headers || {},
    data,
  };
}

async function fetchRequest(url, options = {}) {
  const headers = normalizeHeaders(options.headers);
  const responseType = options.responseType || "text";
  const resp = await fetch(url, {
    method: options.method || "GET",
    headers,
    redirect: "follow",
  });
  let data;
  if (responseType === "arraybuffer") {
    data = await resp.arrayBuffer();
  } else if (responseType === "json") {
    data = parseJsonPayload(await resp.text());
  } else {
    data = await resp.text();
  }
  if (resp.status >= 400) {
    throw new Error(`HTTP ${resp.status}`);
  }
  return {
    status: resp.status,
    url: resp.url || url,
    headers: Object.fromEntries(resp.headers.entries()),
    data,
  };
}

export async function httpGet(url, options = {}) {
  if (isNativePlatform()) {
    try {
      return await nativeRequest(url, options);
    } catch (err) {
      console.warn("[http] native GET failed, fallback fetch:", err?.message);
    }
  }
  return fetchRequest(url, options);
}

export async function httpHeadRange(url, headers = {}) {
  const h = normalizeHeaders({ ...headers, Range: "bytes=0-2047" });
  return httpGet(url, { headers: h, responseType: "arraybuffer" });
}

export async function downloadBinary(url, headers = {}, onProgress) {
  const h = normalizeHeaders({ ...headers, Accept: "*/*" });
  let buffer;
  let finalUrl = url;

  if (isNativePlatform()) {
    try {
      const res = await nativeRequest(url, {
        headers: h,
        responseType: "arraybuffer",
        readTimeout: 600000,
      });
      buffer = res.data;
      finalUrl = res.url || url;
    } catch (err) {
      console.warn("[http] native download failed, fallback fetch:", err?.message);
      const res = await fetchRequest(url, { headers: h, responseType: "arraybuffer" });
      buffer = res.data;
      finalUrl = res.url || url;
    }
  } else {
    const res = await fetchRequest(url, { headers: h, responseType: "arraybuffer" });
    buffer = res.data;
    finalUrl = res.url || url;
  }

  if (!(buffer instanceof ArrayBuffer)) {
    throw new Error("下载失败：未获得视频数据");
  }
  if (buffer.byteLength < 10_000) {
    throw new Error(`下载过小（${buffer.byteLength} 字节），链接可能已失效`);
  }
  if (buffer.byteLength > 500_000_000) {
    throw new Error("视频超过大小限制（500MB）");
  }
  if (onProgress) onProgress(buffer.byteLength, buffer.byteLength);
  return { buffer, finalUrl, size: buffer.byteLength };
}
