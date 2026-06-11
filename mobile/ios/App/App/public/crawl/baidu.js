import { downloadBinary, httpGet } from "./http.js";
import { extractAllHttpUrls, isHttpUrl, unescapeHtml } from "./utils.js";

const MOBILE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 baiduboxapp/13.56.0";
const BAIDU_HOSTS = [
  "mr.baidu.com",
  "mbd.baidu.com",
  "haokan.baidu.com",
  "baijiahao.baidu.com",
  "flp.baidu.com",
];
const BAIDU_SHARE_URL_RE =
  /https?:\/\/(?:mr\.|mbd\.|haokan\.|baijiahao\.|flp\.)?baidu\.com\/[^\s\]\)"'<>，。；;]+/gi;

function mobileHeaders(referer = "https://mbd.baidu.com/") {
  return {
    "User-Agent": MOBILE_UA,
    Accept: "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    Referer: referer,
    Origin: "https://mbd.baidu.com",
  };
}

export function isBaiduShareUrl(url) {
  const u = String(url || "").toLowerCase();
  return BAIDU_HOSTS.some((h) => u.includes(h));
}

export function extractBaiduShareUrl(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  BAIDU_SHARE_URL_RE.lastIndex = 0;
  const m = BAIDU_SHARE_URL_RE.exec(raw);
  if (m) return m[0].replace(/[，。,.;；'"`]+$/g, "");
  return "";
}

function normalizeMediaUrl(url) {
  let u = String(url || "").trim().replace(/\\u002F/g, "/").replace(/\\\//g, "/");
  if (!u) return "";
  if (u.startsWith("//")) u = `https:${u}`;
  if (u.startsWith("http://")) u = `https://${u.slice(7)}`;
  return u;
}

function parseJsonObjectFrom(text, startIdx) {
  const s = String(text || "");
  const start = s.indexOf("{", startIdx);
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escape = false;
  for (let i = start; i < s.length; i += 1) {
    const c = s[i];
    if (inString) {
      if (escape) escape = false;
      else if (c === "\\") escape = true;
      else if (c === '"') inString = false;
      continue;
    }
    if (c === '"') {
      inString = true;
      continue;
    }
    if (c === "{") depth += 1;
    else if (c === "}") {
      depth -= 1;
      if (depth === 0) {
        try {
          return JSON.parse(s.slice(start, i + 1));
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

function parseJsonData(html) {
  const s = unescapeHtml(String(html || ""));
  const marker = s.match(/window\.jsonData\s*=\s*/i);
  if (marker) {
    const parsed = parseJsonObjectFrom(s, marker.index);
    if (parsed) return parsed;
  }
  const m = s.match(/window\.jsonData\s*=\s*(\{[\s\S]*?\})\s*<\/script>/i);
  if (!m) return null;
  try {
    return JSON.parse(m[1]);
  } catch {
    return null;
  }
}

function extractRedirectTarget(html) {
  const s = unescapeHtml(String(html || ""));
  const hrefPatterns = [
    /<a[^>]+href=["'](https?:\/\/[^"']*(?:videoshare|newspage\/data)[^"']*)["']/i,
    /<a[^>]+href=["'](https?:\/\/mbd\.baidu\.com[^"']+)["']/i,
    /href=["'](https?:\/\/[^"']+baidu\.com[^"']*(?:videoshare|\/r\/)[^"']*)["']/i,
  ];
  for (const re of hrefPatterns) {
    const m = s.match(re);
    if (m?.[1]) return normalizeMediaUrl(m[1]);
  }
  const jsonUrl = s.match(/"url"\s*:\s*"(https?:\\\/\\\/[^"]+)"/i);
  if (jsonUrl?.[1]) {
    return normalizeMediaUrl(jsonUrl[1].replace(/\\\//g, "/"));
  }
  return "";
}

function extractNidFromHtml(html) {
  const s = String(html || "");
  let m = s.match(/[?&]nid=(sv_\d+|news_\d+)/i);
  if (m) return m[1];
  m = s.match(/"nid"\s*:\s*"(sv_\d+|news_\d+)"/i);
  if (m) return m[1];
  m = s.match(/sv_(\d{10,})/i);
  if (m) return `sv_${m[1]}`;
  return "";
}

function nidFromUrl(url) {
  const u = String(url || "");
  let m = u.match(/[?&]nid=(sv_\d+|news_\d+)/i);
  if (m) return m[1];
  m = u.match(/\/videoshare\?[^#]*nid=(sv_\d+)/i);
  if (m) return m[1];
  m = u.match(/[?&]id=(\d{10,})/i);
  if (m) return `sv_${m[1]}`;
  return "";
}

function videoIdFromPayload(payload) {
  const data = payload?.data || {};
  const vid = String(data.videoInfo?.vid || data.id || "").replace(/\D/g, "");
  if (vid) return vid;
  const m = String(data.nid || "").match(/(\d{10,})/);
  return m ? m[1] : "";
}

function pickPlayUrls(payload) {
  const data = payload?.data || {};
  const info = data.videoInfo || {};
  const ranked = [];

  const push = (url, score = 0) => {
    const u = normalizeMediaUrl(url);
    if (isHttpUrl(u)) ranked.push([u, score]);
  };

  for (const item of info.clarityArr || []) {
    const bps = Number(item.videoBps || item.bitrate || 0);
    let score = bps * 1000;
    const hw = String(item.vodVideoHW || "");
    const parts = hw.split("$$").map(Number);
    if (parts.length === 2) score = Math.max(score, parts[0] * parts[1]);
    const title = String(item.title || item.name || "").toLowerCase();
    if (title.includes("1080")) score = Math.max(score, 300000);
    if (title.includes("720")) score = Math.max(score, 200000);
    if (title.includes("540") || title.includes("360")) score = Math.max(score, 120000);
    push(item.url, score + (item.used ? 1000 : 0));
  }

  push(info.play_url, 100000);
  push(info.videoUrl, 90000);

  for (const item of data.mpd || []) {
    push(item.url, 50000);
  }

  ranked.sort((a, b) => b[1] - a[1]);
  const urls = [];
  const seen = new Set();
  for (const [u] of ranked) {
    if (!seen.has(u)) {
      seen.add(u);
      urls.push(u);
    }
  }
  return urls;
}

async function fetchBaiduHtml(initialUrl, maxHops = 5) {
  let url = initialUrl.trim();
  let html = "";
  let finalUrl = url;

  for (let hop = 0; hop < maxHops; hop += 1) {
    const res = await httpGet(url, { headers: mobileHeaders() });
    html = String(res.data || "");
    finalUrl = res.url || url;

    if (parseJsonData(html)?.data?.videoInfo) {
      return { html, finalUrl };
    }

    const nid =
      nidFromUrl(finalUrl) || nidFromUrl(url) || extractNidFromHtml(html);
    if (nid && !String(finalUrl).includes("videoshare")) {
      url = `https://mbd.baidu.com/newspage/data/videoshare?nid=${encodeURIComponent(nid)}`;
      continue;
    }

    const next = extractRedirectTarget(html);
    if (!next || next === url) break;
    url = next;
  }

  return { html, finalUrl: finalUrl || url };
}

async function fetchSharePayload(shareUrl) {
  const { html, finalUrl } = await fetchBaiduHtml(shareUrl);
  let payload = parseJsonData(html);

  if (!payload?.data?.videoInfo) {
    const nid =
      nidFromUrl(finalUrl) ||
      nidFromUrl(shareUrl) ||
      extractNidFromHtml(html);
    if (nid) {
      const pageUrl = `https://mbd.baidu.com/newspage/data/videoshare?nid=${encodeURIComponent(nid)}`;
      const res2 = await httpGet(pageUrl, { headers: mobileHeaders() });
      payload = parseJsonData(String(res2.data || ""));
    }
  }

  if (!payload?.data?.videoInfo) {
    throw new Error("百度页面未包含视频数据，链接可能已失效");
  }
  return payload;
}

export async function tryCrawlBaidu(shareText, onProgress) {
  let shareUrl = extractBaiduShareUrl(shareText);
  if (!shareUrl) {
    const urls = extractAllHttpUrls(shareText).filter(isBaiduShareUrl);
    shareUrl = urls[0] || "";
  }
  if (!shareUrl) return null;

  try {
    const payload = await fetchSharePayload(shareUrl);
    const data = payload.data || {};
    const info = data.videoInfo || {};
    const playUrls = pickPlayUrls(payload);
    if (!playUrls.length) throw new Error("百度未返回可播放地址");

    const videoId = videoIdFromPayload(payload) || "baidu";
    const duration = Number(info.duration || info.freeDuration || 0);
    const cover = normalizeMediaUrl(info.posterImage || info.poster || "");
    const referer = normalizeMediaUrl(data.host || "https://mbd.baidu.com/");

    let lastErr = null;
    for (const playUrl of playUrls.slice(0, 6)) {
      try {
        const { buffer, size, finalUrl } = await downloadBinary(
          playUrl,
          mobileHeaders(referer),
          onProgress
        );
        return {
          buffer,
          size,
          aweme_id: videoId,
          caption: String(data.title || info.title || "").slice(0, 200),
          play_url: finalUrl,
          duration,
          cover_url: cover,
          crawl_method: "baidu_videoshare",
          watermark_free: true,
          source: "baidu_crawl",
        };
      } catch (err) {
        lastErr = err;
      }
    }
    throw lastErr || new Error("百度视频下载失败");
  } catch (err) {
    throw new Error(err?.message || "百度爬取失败");
  }
}

export function isBaiduShare(text) {
  return Boolean(extractBaiduShareUrl(text)) || extractAllHttpUrls(text).some(isBaiduShareUrl);
}
