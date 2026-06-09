import { downloadBinary, httpGet, httpHeadRange } from "./http.js";
import { awemeIdFromUrl, extractAllHttpUrls, isHttpUrl, unescapeHtml } from "./utils.js";

const MOBILE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1";
const PC_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
const DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/";
const CDN_HOST_HINTS = ["douyinvod", "zjcdn.com", "bytecdn", "bytevcloud", "amemv.com", "/video/tos/"];

function mobileHeaders(referer = "https://www.douyin.com/", cookie = "") {
  const h = {
    "User-Agent": MOBILE_UA,
    Accept: "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    Referer: referer,
  };
  if (cookie) h.Cookie = cookie;
  return h;
}

function apiHeaders(referer, cookie) {
  const h = {
    "User-Agent": PC_UA,
    Accept: "application/json, text/plain, */*",
    Referer: referer,
  };
  if (cookie) h.Cookie = cookie;
  return h;
}

function isSharePageUrl(url) {
  const u = String(url || "").toLowerCase();
  return ["v.douyin.com", "douyin.com", "iesdouyin.com"].some((x) => u.includes(x));
}

function isCdnVideoUrl(url) {
  const u = String(url || "").toLowerCase();
  if (!isHttpUrl(url)) return false;
  if (u.includes("snssdk.com/aweme/v1/play")) return false;
  if (u.includes("douyinpic.com")) return false;
  return CDN_HOST_HINTS.some((h) => u.includes(h)) || u.split("?")[0].endsWith(".mp4");
}

function playApiUrl(vid, watermark) {
  const path = watermark ? "playwm" : "play";
  return `https://aweme.snssdk.com/aweme/v1/${path}/?video_id=${vid}&ratio=720p&line=0`;
}

function scoreCandidate(url) {
  const u = String(url || "").toLowerCase();
  if (isCdnVideoUrl(url)) return 0;
  if (u.includes("download_addr") || (u.includes("/play/") && !u.includes("playwm"))) return 2;
  if (u.includes("aweme/v1/play?") && !u.includes("playwm") && u.includes("video_id=")) return 3;
  if (u.includes("playwm") && u.includes("video_id=")) return 60;
  if (u.includes("playwm")) return 80;
  if (u.includes("snssdk.com")) return 50;
  return 40;
}

function urlsFromJson(obj, out, preferDownload = true) {
  if (!obj || typeof obj !== "object") return;
  if (Array.isArray(obj)) {
    obj.forEach((v) => urlsFromJson(v, out, preferDownload));
    return;
  }
  const video = obj.video;
  if (video && typeof video === "object") {
    const keys = preferDownload
      ? ["download_addr", "play_addr", "play_addr_h264"]
      : ["play_addr", "download_addr", "play_addr_h264"];
    for (const key of keys) {
      const addr = video[key] || {};
      for (const u of addr.url_list || []) {
        if (u) out.push(String(u));
      }
    }
  }
  for (const [k, v] of Object.entries(obj)) {
    if (k !== "video") urlsFromJson(v, out, preferDownload);
  }
}

function collectCandidatesFromHtml(html) {
  html = unescapeHtml(html);
  const found = [];
  const seen = new Set();
  const add = (u) => {
    u = String(u || "").trim().replace(/\\+$/, "");
    if (!u || seen.has(u) || !u.startsWith("http")) return;
    seen.add(u);
    found.push(u);
  };

  for (const m of html.matchAll(/"download_addr"\s*:\s*\{[^}]*"url_list"\s*:\s*\[\s*"(https?:\/\/[^"]+)"/gs)) {
    add(m[1]);
  }
  for (const m of html.matchAll(/https?:\/\/aweme\.snssdk\.com\/aweme\/v1\/play(?!wm)\/\?[^"'\\s\]]+/gi)) {
    add(m[0]);
  }
  for (const m of html.matchAll(/https?:\/\/[a-zA-Z0-9./_?=&%-]*(?:douyinvod|zjcdn\.com|bytevcloud)[a-zA-Z0-9./_?=&%-]*/gi)) {
    add(m[0]);
  }
  for (const m of html.matchAll(/"url_list"\s*:\s*\[(.*?)\]/gs)) {
    const block = m[0];
    if (!block.includes("download_addr") && block.includes("playwm")) continue;
    for (const u of m[1].matchAll(/"(https?:\/\/[^"]+)"/g)) {
      if (u[1].includes("douyinpic.com") || u[1].includes("playwm")) continue;
      if (u[1].includes("playwm") || isCdnVideoUrl(u[1]) || u[1].includes("aweme/v1/play")) add(u[1]);
    }
  }
  for (const m of html.matchAll(/video_id=(v[0-9a-zA-Z]+)/g)) {
    add(playApiUrl(m[1], false));
  }
  for (const m of html.matchAll(/"uri"\s*:\s*"(v[0-9][^"]+)"/g)) {
    add(playApiUrl(m[1], false));
  }

  const render = html.match(/<script[^>]+id=["']RENDER_DATA["'][^>]*>([^<]+)<\/script>/i);
  if (render) {
    try {
      const data = JSON.parse(decodeURIComponent(render[1]));
      const extra = [];
      urlsFromJson(data, extra, true);
      extra.forEach(add);
    } catch {
      /* ignore */
    }
  }

  found.sort((a, b) => scoreCandidate(a) - scoreCandidate(b));
  return found;
}

async function fetchShareHtml(shareLink) {
  const res = await httpGet(shareLink.trim(), { headers: mobileHeaders() });
  return { html: String(res.data || ""), finalUrl: res.url };
}

async function verifyVideoUrl(url, referer) {
  try {
    const res = await httpHeadRange(url, mobileHeaders(referer));
    if (res.status !== 200 && res.status !== 206) return false;
    const ct = String(res.headers["Content-Type"] || res.headers["content-type"] || "").toLowerCase();
    if (ct.includes("video") || ct.includes("octet-stream")) return true;
    if (res.data?.byteLength > 500) return true;
    const final = String(res.url || "").toLowerCase();
    return CDN_HOST_HINTS.some((h) => final.includes(h));
  } catch {
    return false;
  }
}

function crawlMethodLabel(url) {
  const u = String(url || "").toLowerCase();
  if (u.includes("playwm")) return "playwm";
  if (u.includes("aweme/v1/play")) return "play_nowm";
  if (isCdnVideoUrl(url)) return "cdn";
  return "direct";
}

async function resolveFromShareLink(shareLink) {
  let { html, finalUrl } = await fetchShareHtml(shareLink);
  let candidates = collectCandidatesFromHtml(html);

  const awemeId = awemeIdFromUrl(finalUrl) || awemeIdFromUrl(shareLink);
  if (awemeId && !finalUrl.includes("iesdouyin.com/share/video")) {
    try {
      const extra = await fetchShareHtml(`https://www.iesdouyin.com/share/video/${awemeId}/`);
      for (const u of collectCandidatesFromHtml(extra.html)) {
        if (!candidates.includes(u)) candidates.push(u);
      }
      finalUrl = extra.finalUrl;
    } catch {
      /* ignore */
    }
  }

  candidates.sort((a, b) => scoreCandidate(a) - scoreCandidate(b));
  return { candidates, referer: finalUrl };
}

function playUrlFromAwemeDetail(aweme) {
  const video = aweme?.video || {};
  for (const key of ["download_addr", "play_addr"]) {
    for (const u of video[key]?.url_list || []) {
      if (isHttpUrl(u)) return String(u).trim();
    }
  }
  return "";
}

function normalizeSearchItem(raw) {
  const aweme = raw?.aweme_info || raw?.aweme || raw;
  if (!aweme || typeof aweme !== "object") return null;
  const awemeId = String(aweme.aweme_id || aweme.awemeId || "");
  if (!awemeId) return null;
  const playUrl = playUrlFromAwemeDetail(aweme);
  const durationMs = Number(aweme.video?.duration || 0);
  return {
    aweme_id: awemeId,
    caption: String(aweme.desc || ""),
    play_url: playUrl,
    duration: durationMs > 1000 ? durationMs / 1000 : durationMs,
    cover_url: "",
  };
}

async function crawlWithCookie(shareLink, cookie) {
  let awemeId = awemeIdFromUrl(shareLink);
  if (!awemeId) {
    const page = await fetchShareHtml(shareLink);
    awemeId = awemeIdFromUrl(page.finalUrl);
  }
  if (!awemeId) throw new Error("无法解析作品 ID");

  const referer = `https://www.douyin.com/video/${awemeId}`;
  const qs = new URLSearchParams({
    device_platform: "webapp",
    aid: "6383",
    aweme_id: awemeId,
  });
  const res = await httpGet(`${DETAIL_URL}?${qs}`, {
    headers: apiHeaders(referer, cookie),
    responseType: "json",
  });
  const payload = res.data || {};
  const detail = payload.aweme_detail || payload.aweme_info || {};
  let playUrl = playUrlFromAwemeDetail(detail);
  const item = normalizeSearchItem({ aweme_info: detail }) || {};
  if (!playUrl) playUrl = item.play_url || "";
  if (!playUrl) throw new Error("接口未返回播放地址");
  return {
    ...item,
    play_url: playUrl,
    crawl_method: "api_nowm",
    watermark_free: !playUrl.toLowerCase().includes("playwm"),
    source: "douyin_crawl",
  };
}

async function tryShareUrls(shareUrls, cookie, onProgress) {
  for (const share of shareUrls) {
    let candidates;
    let referer;
    try {
      ({ candidates, referer } = await resolveFromShareLink(share));
    } catch {
      continue;
    }
    for (const play of candidates) {
      if (!(await verifyVideoUrl(play, referer))) continue;
      try {
        const method = crawlMethodLabel(play);
        const { buffer, size, finalUrl } = await downloadBinary(
          play,
          mobileHeaders(referer),
          onProgress
        );
        return {
          buffer,
          size,
          aweme_id: awemeIdFromUrl(referer) || awemeIdFromUrl(finalUrl) || "douyin",
          caption: "",
          play_url: finalUrl,
          duration: 0,
          crawl_method: method,
          watermark_free: !play.toLowerCase().includes("playwm"),
          source: "douyin_crawl",
        };
      } catch {
        /* try next */
      }
    }
  }
  return null;
}

export async function tryCrawlDouyin(shareText, cookie, onProgress) {
  const urls = extractAllHttpUrls(shareText);
  if (!urls.length) return null;
  const shareUrls = urls.filter(isSharePageUrl);
  const targets = shareUrls.length ? shareUrls : urls.slice(0, 1);

  const got = await tryShareUrls(targets, cookie, onProgress);
  if (got) return got;

  if (cookie) {
    const meta = await crawlWithCookie(targets[0], cookie);
    const playUrl = meta.play_url;
    if (isHttpUrl(playUrl)) {
      const { buffer, size, finalUrl } = await downloadBinary(
        playUrl,
        mobileHeaders("https://www.douyin.com/"),
        onProgress
      );
      return { ...meta, buffer, size, play_url: finalUrl };
    }
  }
  return null;
}
