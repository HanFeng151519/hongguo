import { downloadBinary, httpGet, httpPost, parseJsonPayload } from "./http.js";
import { extractAllHttpUrls, isHttpUrl } from "./utils.js";

const MOBILE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1";
const XYQ_HOSTS = ["xiaoyunque.jianying.com", "xyq.jianying.com"];
const LANDING_API =
  "https://xiaoyunque.jianying.com/luckycat/cn/jianying/campaign/v1/pippit/share/landing_page";
const XYQ_SHARE_URL_RE =
  /https?:\/\/(?:xiaoyunque|xyq)\.jianying\.com\/[^\s\]\)"'<>，。；;]+/gi;

function mobileHeaders(referer = "https://xiaoyunque.jianying.com/") {
  return {
    "User-Agent": MOBILE_UA,
    Accept: "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9",
    Referer: referer,
  };
}

export function isXiaoyunqueShareUrl(url) {
  const u = String(url || "").toLowerCase();
  return XYQ_HOSTS.some((h) => u.includes(h));
}

export function extractXiaoyunqueShareUrl(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  XYQ_SHARE_URL_RE.lastIndex = 0;
  const m = XYQ_SHARE_URL_RE.exec(raw);
  if (m) return m[0].replace(/[，。,.;；'"`]+$/g, "");
  for (const u of extractAllHttpUrls(raw)) {
    if (isXiaoyunqueShareUrl(u)) return u;
  }
  return "";
}

function normalizeMediaUrl(url) {
  let u = String(url || "").trim().replace(/\\u002F/g, "/").replace(/\\\//g, "/");
  if (!u) return "";
  if (u.startsWith("//")) u = `https:${u}`;
  return u;
}

function queryParamsFromUrl(pageUrl) {
  const out = {};
  try {
    const u = new URL(pageUrl);
    u.searchParams.forEach((value, key) => {
      out[key] = value;
    });
  } catch {
    return out;
  }
  if (!out.content_type) out.content_type = "video";
  if (!out.share_campaign_key) out.share_campaign_key = "pippit_invite_fission";
  return out;
}

async function resolveSharePageUrl(shareUrl) {
  const res = await httpGet(shareUrl, { headers: mobileHeaders() });
  const finalUrl = String(res.url || shareUrl);
  const qp = queryParamsFromUrl(finalUrl);
  if (qp.share_id || qp.inspiration_id || qp.template_id) {
    return finalUrl;
  }
  if (/\/s\/[A-Za-z0-9]+\/?$/i.test(shareUrl) && finalUrl === shareUrl) {
    throw new Error("小云雀短链未能解析，请粘贴完整分享文案后重试");
  }
  return finalUrl;
}

function pickVideoEntry(payload) {
  const pageInfo = payload?.data?.page_info || {};
  const block = pageInfo.inspiration_page || pageInfo.generate_page || {};
  const videos = block?.item_info?.video_info || [];
  if (!videos.length) return null;
  let best = videos[0];
  for (const item of videos) {
    const score = Number(item.width || 0) * Number(item.height || 0);
    const bestScore = Number(best.width || 0) * Number(best.height || 0);
    if (score > bestScore) best = item;
  }
  return {
    video: best,
    item: block.item_info || {},
    user: block.user_info || {},
  };
}

async function fetchLandingPayload(pageUrl) {
  const query_params = queryParamsFromUrl(pageUrl);
  if (!query_params.share_id && !query_params.inspiration_id && !query_params.template_id) {
    throw new Error("小云雀链接缺少 share_id / inspiration_id，无法解析");
  }

  const res = await httpPost(LANDING_API, {
    headers: mobileHeaders(pageUrl),
    body: JSON.stringify({ query_params }),
    responseType: "json",
  });
  const payload = parseJsonPayload(res.data);
  if (Number(payload?.err_no) !== 0) {
    throw new Error(payload?.err_tips || "小云雀接口返回错误");
  }
  return payload;
}

export async function tryCrawlXiaoyunque(shareText, onProgress) {
  let shareUrl = extractXiaoyunqueShareUrl(shareText);
  if (!shareUrl) {
    const urls = extractAllHttpUrls(shareText).filter(isXiaoyunqueShareUrl);
    shareUrl = urls[0] || "";
  }
  if (!shareUrl) return null;

  try {
    const pageUrl = await resolveSharePageUrl(shareUrl);
    const payload = await fetchLandingPayload(pageUrl);
    const picked = pickVideoEntry(payload);
    if (!picked?.video?.video_url) {
      throw new Error("小云雀未返回视频地址，可能不是视频分享或链接已失效");
    }

    const playUrl = normalizeMediaUrl(picked.video.video_url);
    if (!isHttpUrl(playUrl)) {
      throw new Error("小云雀视频地址无效");
    }

    const videoId =
      queryParamsFromUrl(pageUrl).inspiration_id ||
      queryParamsFromUrl(pageUrl).template_id ||
      queryParamsFromUrl(pageUrl).share_id ||
      "xiaoyunque";
    const caption = String(picked.item.desc || picked.item.title || "").slice(0, 200);
    const cover = normalizeMediaUrl(picked.video.cover_url || "");

    // 365yg CDN：带 xiaoyunque Referer 会 403，需裸拉或换 Referer
    const downloadHeaderSets = [
      { "User-Agent": MOBILE_UA, Accept: "*/*" },
      { "User-Agent": MOBILE_UA, Accept: "*/*", Referer: "https://www.douyin.com/" },
    ];
    let buffer;
    let size;
    let finalUrl = playUrl;
    let lastErr = null;
    for (const headers of downloadHeaderSets) {
      try {
        const got = await downloadBinary(playUrl, headers, onProgress);
        buffer = got.buffer;
        size = got.size;
        finalUrl = got.finalUrl;
        lastErr = null;
        break;
      } catch (err) {
        lastErr = err;
        const msg = String(err?.message || err);
        if (!/403|Forbidden/i.test(msg)) break;
      }
    }
    if (!buffer) {
      throw lastErr || new Error("小云雀视频下载失败（CDN 403）");
    }

    return {
      buffer,
      size,
      aweme_id: String(videoId),
      caption,
      play_url: finalUrl,
      duration: 0,
      cover_url: cover,
      crawl_method: "xiaoyunque_landing_page",
      watermark_free: true,
      source: "xiaoyunque_crawl",
    };
  } catch (err) {
    throw new Error(err?.message || "小云雀爬取失败");
  }
}

export function isXiaoyunqueShare(text) {
  return (
    Boolean(extractXiaoyunqueShareUrl(text)) ||
    extractAllHttpUrls(text).some(isXiaoyunqueShareUrl)
  );
}
