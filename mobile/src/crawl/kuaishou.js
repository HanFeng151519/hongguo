import { downloadBinary, httpGet } from "./http.js";
import { extractAllHttpUrls } from "./utils.js";

const PC_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
const KS_HOSTS = ["kuaishou.com", "kuaishou.cn", "chenzhongtech.com"];
const KS_SHARE_URL_RE =
  /https?:\/\/(?:v\.|www\.)?kuaishou\.com\/[^\s\]\)"'<>，。；;]+|https?:\/\/[^\s\]\)"'<>，。；;]*chenzhongtech\.com\/[^\s\]\)"'<>，。；;]+/gi;

export function isKuaishouShareUrl(url) {
  const u = String(url || "").toLowerCase();
  return KS_HOSTS.some((h) => u.includes(h));
}

export function extractKuaishouShareUrl(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  KS_SHARE_URL_RE.lastIndex = 0;
  const m = KS_SHARE_URL_RE.exec(raw);
  if (m) return m[0].replace(/[，。,.;；'"`]+$/g, "");
  for (const u of extractAllHttpUrls(raw)) {
    if (isKuaishouShareUrl(u)) return u;
  }
  return "";
}

function photoIdFromUrl(url) {
  const u = String(url || "");
  let m = u.match(/short-video\/([A-Za-z0-9_-]+)/);
  if (m) return m[1];
  m = u.match(/photoId=([A-Za-z0-9_-]+)/);
  if (m) return m[1];
  return "";
}

function scoreMp4Url(url) {
  const u = String(url || "").toLowerCase();
  let score = 0;
  if (u.includes(".mp4")) score += 50;
  if (u.includes("photo-video")) score += 40;
  if (u.includes("hd")) score += 20;
  if (u.includes("ndcimgs.com") || u.includes("kwaicdn") || u.includes("yximgs.com")) score += 10;
  if (u.includes("upic/") && !u.includes("photo-video")) score -= 30;
  return score;
}

function urlsFromManifest(manifest) {
  if (!manifest) return [];
  if (typeof manifest === "string") {
    try {
      manifest = JSON.parse(manifest);
    } catch {
      return [];
    }
  }
  const urls = [];
  for (const adapt of manifest.adaptationSet || []) {
    for (const rep of adapt.representation || []) {
      const height = Number(rep.height || 0);
      for (const key of ["url", "backupUrl"]) {
        const u = rep[key];
        if (typeof u === "string" && u.startsWith("http")) urls.push([height, u.trim()]);
      }
    }
  }
  urls.sort((a, b) => b[0] - a[0]);
  const out = [];
  const seen = new Set();
  for (const [, u] of urls) {
    if (!seen.has(u)) {
      seen.add(u);
      out.push(u);
    }
  }
  return out;
}

function urlsFromApollo(html) {
  const m = String(html || "").match(/window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;/s);
  if (!m) return [];
  let data;
  try {
    data = JSON.parse(m[1]);
  } catch {
    return [];
  }
  const found = [];
  const walk = (obj) => {
    if (!obj || typeof obj !== "object") return;
    if (Array.isArray(obj)) {
      obj.forEach(walk);
      return;
    }
    for (const [key, val] of Object.entries(obj)) {
      if ((key === "photoUrl" || key === "srcNoMark") && typeof val === "string") {
        const u = val.replace(/\\u002F/g, "/").trim();
        if (u.startsWith("http")) found.push(u);
      }
      if (key === "manifest" && typeof val === "string") {
        try {
          found.push(...urlsFromManifest(JSON.parse(val)));
        } catch {
          /* ignore */
        }
      }
      if (key === "url" && typeof val === "string") {
        const u = val.replace(/\\u002F/g, "/").trim();
        if (u.startsWith("http") && (u.includes(".mp4") || u.includes("photo-video"))) found.push(u);
      }
      walk(val);
    }
  };
  walk(data);
  const seen = new Set();
  const out = [];
  for (const u of found) {
    if (!seen.has(u)) {
      seen.add(u);
      out.push(u);
    }
  }
  out.sort((a, b) => scoreMp4Url(b) - scoreMp4Url(a));
  return out;
}

async function resolvePage(shareUrl) {
  const res = await httpGet(shareUrl.trim(), {
    headers: {
      "User-Agent": PC_UA,
      Referer: "https://www.kuaishou.com/",
      Accept: "text/html,application/xhtml+xml,*/*;q=0.8",
    },
  });
  const final = res.url;
  const html = String(res.data || "");
  const photoId = photoIdFromUrl(final) || photoIdFromUrl(shareUrl);
  const plays = urlsFromApollo(html);
  return { finalUrl: final, photoId, plays };
}

export async function tryCrawlKuaishou(shareText, onProgress) {
  let shareUrl = extractKuaishouShareUrl(shareText);
  if (!shareUrl) {
    const urls = extractAllHttpUrls(shareText).filter(isKuaishouShareUrl);
    shareUrl = urls[0] || "";
  }
  if (!shareUrl) return null;

  try {
    const { finalUrl, photoId, plays } = await resolvePage(shareUrl);
    const playUrl = plays[0] || "";
    if (!playUrl) {
      throw new Error("无法从快手页面解析视频地址");
    }
    const { buffer, size, finalUrl: dlUrl } = await downloadBinary(
      playUrl,
      { "User-Agent": PC_UA, Referer: "https://www.kuaishou.com/" },
      onProgress
    );
    return {
      buffer,
      size,
      aweme_id: photoId || "kuaishou",
      caption: "",
      play_url: dlUrl,
      duration: 0,
      crawl_method: "kuaishou_page",
      watermark_free: true,
      source: "kuaishou_crawl",
    };
  } catch (err) {
    console.info("快手爬取失败:", err);
    return null;
  }
}

export function isKuaishouShare(text) {
  return Boolean(extractKuaishouShareUrl(text)) || extractAllHttpUrls(text).some(isKuaishouShareUrl);
}
