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

const PREFER_QUALITY = true;

function playApiUrl(vid, watermark, ratio = "720p") {
  const path = watermark ? "playwm" : "play";
  return `https://aweme.snssdk.com/aweme/v1/${path}/?video_id=${vid}&ratio=${ratio}&line=0`;
}

function urlQualityScore(url) {
  const u = String(url || "").toLowerCase();
  let score = 0;
  for (const [token, pts] of [
    ["2160", 400],
    ["1080", 300],
    ["720", 200],
    ["540", 120],
    ["480", 80],
  ]) {
    if (u.includes(token)) score = Math.max(score, pts);
  }
  if (u.includes("ratio=1080") || u.includes("1080p")) score = Math.max(score, 300);
  if (u.includes("ratio=720") || u.includes("720p")) score = Math.max(score, 200);
  const br = u.match(/br=(\d+)/);
  if (br) score = Math.max(score, Math.floor(Number(br[1]) / 8000));
  return score;
}

function bitRateUrlsFromVideo(video) {
  const ranked = [];
  for (const entry of video?.bit_rate || []) {
    if (!entry || typeof entry !== "object") continue;
    const br = Number(entry.bit_rate || 0);
    const gear = String(entry.gear_name || "");
    let gearScore = 0;
    for (const [token, pts] of [
      ["1080", 300],
      ["720", 200],
      ["540", 120],
    ]) {
      if (gear.includes(token)) gearScore = Math.max(gearScore, pts);
    }
    const q = Math.max(Math.floor(br / 1000), gearScore);
    for (const u of entry.play_addr?.url_list || []) {
      if (isHttpUrl(u)) ranked.push([String(u).trim(), q + urlQualityScore(u)]);
    }
  }
  ranked.sort((a, b) => b[1] - a[1]);
  return ranked;
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

function sortCandidates(urls) {
  if (!PREFER_QUALITY) return [...urls].sort((a, b) => scoreCandidate(a) - scoreCandidate(b));
  return [...urls].sort((a, b) => {
    const qa = urlQualityScore(a);
    const qb = urlQualityScore(b);
    if (qb !== qa) return qb - qa;
    const wma = a.toLowerCase().includes("playwm") ? 1 : 0;
    const wmb = b.toLowerCase().includes("playwm") ? 1 : 0;
    if (wma !== wmb) return wma - wmb;
    return scoreCandidate(a) - scoreCandidate(b);
  });
}

function urlsFromJson(obj, out, preferDownload = true) {
  if (!obj || typeof obj !== "object") return;
  if (Array.isArray(obj)) {
    obj.forEach((v) => urlsFromJson(v, out, preferDownload));
    return;
  }
  const video = obj.video;
  if (video && typeof video === "object") {
    if (PREFER_QUALITY) {
      for (const [u] of bitRateUrlsFromVideo(video)) out.push(u);
    }
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
  const ratios = PREFER_QUALITY ? ["1080p", "720p"] : ["720p"];
  for (const m of html.matchAll(/video_id=(v[0-9a-zA-Z]+)/g)) {
    for (const ratio of ratios) add(playApiUrl(m[1], false, ratio));
    add(playApiUrl(m[1], true));
  }
  for (const m of html.matchAll(/"uri"\s*:\s*"(v[0-9][^"]+)"/g)) {
    for (const ratio of ratios) add(playApiUrl(m[1], false, ratio));
    add(playApiUrl(m[1], true));
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

  return sortCandidates(found);
}

async function fetchShareHtml(shareLink) {
  const res = await httpGet(shareLink.trim(), { headers: mobileHeaders() });
  return { html: String(res.data || ""), finalUrl: res.url };
}

async function verifyVideoUrl(url, referer) {
  const u = String(url || "").toLowerCase();
  if (isCdnVideoUrl(url) || u.includes("aweme/v1/play")) return true;
  try {
    const res = await httpHeadRange(url, mobileHeaders(referer));
    if (res.status !== 200 && res.status !== 206) return true;
    const ct = String(res.headers["Content-Type"] || res.headers["content-type"] || "").toLowerCase();
    if (ct.includes("video") || ct.includes("octet-stream")) return true;
    if (res.data?.byteLength > 500) return true;
    const final = String(res.url || "").toLowerCase();
    if (CDN_HOST_HINTS.some((h) => final.includes(h))) return true;
  } catch {
    /* 预检失败仍尝试完整下载 */
  }
  return true;
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

  candidates = sortCandidates(candidates);
  return { candidates, referer: finalUrl };
}

function playUrlFromAwemeDetail(aweme) {
  const video = aweme?.video || {};
  if (PREFER_QUALITY) {
    const ranked = bitRateUrlsFromVideo(video);
    if (ranked.length) return ranked[0][0];
  }
  for (const key of ["download_addr", "play_addr"]) {
    for (const u of video[key]?.url_list || []) {
      if (isHttpUrl(u)) return String(u).trim();
    }
  }
  const ranked = bitRateUrlsFromVideo(video);
  if (ranked.length) return ranked[0][0];
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
  const payload = typeof res.data === "object" && res.data ? res.data : {};
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
      } catch (err) {
        console.warn("[douyin] 候选下载失败:", play.slice(0, 80), err?.message);
      }
    }
  }
  return null;
}

export function isDouyinShare(text) {
  return extractAllHttpUrls(text).some(isSharePageUrl);
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

  if (shareUrls.length || targets.some(isSharePageUrl)) {
    throw new Error(
      cookie
        ? "抖音解析失败，Cookie 可能已过期"
        : "抖音短链无法直访，请展开填写抖音 Cookie（登录 douyin.com 后复制）"
    );
  }
  return null;
}
