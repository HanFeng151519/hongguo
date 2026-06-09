/** Native HTTP via Capacitor（绕过 WebView CORS），浏览器调试时回退 fetch。 */
import { Capacitor, CapacitorHttp } from "@capacitor/core";

function normalizeHeaders(headers = {}) {
  const out = {};
  for (const [k, v] of Object.entries(headers)) {
    if (v != null && v !== "") out[k] = String(v);
  }
  return out;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function base64ToArrayBuffer(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

async function fetchGet(url, { headers, responseType }) {
  const resp = await fetch(url, { headers, redirect: "follow" });
  const out = {
    status: resp.status,
    url: resp.url,
    headers: Object.fromEntries(resp.headers.entries()),
  };
  if (responseType === "arraybuffer") {
    out.data = await resp.arrayBuffer();
  } else if (responseType === "json") {
    out.data = await resp.json();
  } else {
    out.data = await resp.text();
  }
  return out;
}

export async function httpGet(url, options = {}) {
  const headers = normalizeHeaders(options.headers);
  const responseType = options.responseType || "text";

  if (Capacitor.isNativePlatform()) {
    const res = await CapacitorHttp.get({
      url,
      headers,
      responseType,
    });
    let data = res.data;
    if (responseType === "arraybuffer" && typeof data === "string") {
      data = base64ToArrayBuffer(data);
    }
    return {
      status: res.status,
      url: res.url || url,
      headers: res.headers || {},
      data,
    };
  }
  return fetchGet(url, { headers, responseType });
}

export async function httpHeadRange(url, headers = {}) {
  const h = normalizeHeaders({ ...headers, Range: "bytes=0-2047" });
  const res = await httpGet(url, { headers: h, responseType: "arraybuffer" });
  return res;
}

export async function downloadBinary(url, headers = {}, onProgress) {
  const res = await httpGet(url, {
    headers: normalizeHeaders({ ...headers, Accept: "*/*" }),
    responseType: "arraybuffer",
  });
  if (res.status !== 200 && res.status !== 206) {
    throw new Error(`视频地址返回 HTTP ${res.status}`);
  }
  const ct = String(res.headers["Content-Type"] || res.headers["content-type"] || "").toLowerCase();
  if (ct.includes("text/html")) {
    throw new Error("链接返回网页而非视频");
  }
  const buf = res.data;
  if (!(buf instanceof ArrayBuffer) || buf.byteLength < 50_000) {
    throw new Error("下载过小，链接可能已失效");
  }
  if (buf.byteLength > 500_000_000) {
    throw new Error("视频超过大小限制（500MB）");
  }
  if (onProgress) onProgress(buf.byteLength, buf.byteLength);
  return { buffer: buf, finalUrl: res.url || url, size: buf.byteLength };
}
