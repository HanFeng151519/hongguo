import { downloadBinary, httpGet, parseJsonPayload } from "./http.js";
import { extractAllHttpUrls, isHttpUrl } from "./utils.js";

const MOBILE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1";
const KLING_HOSTS = [
  "klingai-share.kuaishou.com",
  "klingai.kuaishou.com",
  "klingai.com",
];
const KLING_SHARE_URL_RE =
  /https?:\/\/(?:klingai-share\.kuaishou|klingai\.kuaishou|klingai)\.com\/[^\s\]\)"'<>，。；;]+/gi;
const API_PATH = "/app/creatives/query";

function mobileHeaders(referer = "https://klingai-share.kuaishou.com/") {
  return {
    "User-Agent": MOBILE_UA,
    Accept: "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    Referer: referer,
  };
}

export function isKlingShareUrl(url) {
  const u = String(url || "").toLowerCase();
  return KLING_HOSTS.some((h) => u.includes(h)) && /\/h5-app\/share|creative_id=|work_id=/i.test(u);
}

export function extractKlingShareUrl(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  KLING_SHARE_URL_RE.lastIndex = 0;
  const m = KLING_SHARE_URL_RE.exec(raw);
  if (m) return m[0].replace(/[，。,.;；'"`]+$/g, "");
  for (const u of extractAllHttpUrls(raw)) {
    if (isKlingShareUrl(u)) return u;
  }
  return "";
}

function normalizeMediaUrl(url) {
  let u = String(url || "").trim().replace(/\\u002F/g, "/").replace(/\\\//g, "/");
  if (!u) return "";
  if (u.startsWith("//")) u = `https:${u}`;
  return u;
}

function apiBaseFromShareUrl(shareUrl) {
  try {
    const host = new URL(shareUrl).hostname.toLowerCase();
    if (host.includes("klingai.com") && !host.includes("kuaishou")) {
      return "https://klingai.com";
    }
    if (host.includes("klingai.kuaishou.com")) {
      return "https://klingai.kuaishou.com";
    }
  } catch {
    /* fall through */
  }
  return "https://klingai-share.kuaishou.com";
}

function queryFromShareUrl(shareUrl) {
  const out = { creativeId: "", creativeType: "WORK" };
  try {
    const u = new URL(shareUrl);
    out.creativeId =
      u.searchParams.get("creative_id") ||
      u.searchParams.get("creativeId") ||
      u.searchParams.get("work_id") ||
      u.searchParams.get("workId") ||
      "";
    out.creativeType =
      u.searchParams.get("creative_type") ||
      u.searchParams.get("creativeType") ||
      "WORK";
  } catch {
    return out;
  }
  return out;
}

async function fetchCreativeDetail(shareUrl) {
  const { creativeId, creativeType } = queryFromShareUrl(shareUrl);
  if (!creativeId) {
    throw new Error("可灵链接缺少 work_id / creative_id，无法解析");
  }

  const bases = [apiBaseFromShareUrl(shareUrl), "https://klingai-share.kuaishou.com"];
  const seen = new Set();
  let lastErr = null;

  for (const base of bases) {
    if (seen.has(base)) continue;
    seen.add(base);
    const apiUrl = `${base}${API_PATH}?creativeId=${encodeURIComponent(creativeId)}&creativeType=${encodeURIComponent(creativeType)}`;
    try {
      const res = await httpGet(apiUrl, {
        headers: mobileHeaders(shareUrl),
        responseType: "json",
      });
      const payload = parseJsonPayload(res.data);
      const detail = payload?.data ?? payload;
      if (!detail?.resource?.resource) {
        throw new Error(payload?.message || "可灵接口未返回视频资源");
      }
      return detail;
    } catch (err) {
      lastErr = err;
    }
  }

  throw lastErr || new Error("可灵作品详情获取失败");
}

function pickVideoUrl(detail) {
  const resource = detail?.resource || {};
  const url = normalizeMediaUrl(resource.resource || "");
  if (!url) return null;
  return {
    playUrl: url,
    duration: Math.round(Number(resource.duration || 0) / 1000),
    width: Number(resource.width || 0),
    height: Number(resource.height || 0),
  };
}

export async function tryCrawlKling(shareText, onProgress) {
  let shareUrl = extractKlingShareUrl(shareText);
  if (!shareUrl) {
    const urls = extractAllHttpUrls(shareText).filter(isKlingShareUrl);
    shareUrl = urls[0] || "";
  }
  if (!shareUrl) return null;

  try {
    const detail = await fetchCreativeDetail(shareUrl);
    const picked = pickVideoUrl(detail);
    if (!picked?.playUrl || !isHttpUrl(picked.playUrl)) {
      throw new Error("可灵未返回有效视频地址，可能不是视频作品或链接已失效");
    }

    const { creativeId } = queryFromShareUrl(shareUrl);
    const caption = String(detail.title || detail.introduction || "").slice(0, 200);
    const cover = normalizeMediaUrl(detail.cover?.resource || detail.firstFrame?.resource || "");

    // Kling CDN：带 Referer 会 400，需裸拉
    const downloadHeaderSets = [
      { "User-Agent": MOBILE_UA, Accept: "*/*" },
      { "User-Agent": MOBILE_UA, Accept: "*/*", Referer: "https://www.douyin.com/" },
    ];
    let buffer;
    let size;
    let finalUrl = picked.playUrl;
    let lastErr = null;
    for (const headers of downloadHeaderSets) {
      try {
        const got = await downloadBinary(picked.playUrl, headers, onProgress);
        buffer = got.buffer;
        size = got.size;
        finalUrl = got.finalUrl;
        lastErr = null;
        break;
      } catch (err) {
        lastErr = err;
        const msg = String(err?.message || err);
        if (!/400|403|Forbidden/i.test(msg)) break;
      }
    }
    if (!buffer) {
      throw lastErr || new Error("可灵视频下载失败（CDN 拒绝）");
    }

    return {
      buffer,
      size,
      aweme_id: String(creativeId || "kling"),
      caption,
      play_url: finalUrl,
      duration: picked.duration,
      cover_url: cover,
      crawl_method: "kling_creatives_query",
      watermark_free: true,
      source: "kling_crawl",
    };
  } catch (err) {
    throw new Error(err?.message || "可灵爬取失败");
  }
}

export function isKlingShare(text) {
  return Boolean(extractKlingShareUrl(text)) || extractAllHttpUrls(text).some(isKlingShareUrl);
}
