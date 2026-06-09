import { downloadBinary, httpGet } from "./http.js";
import { extractAllHttpUrls } from "./utils.js";

const MOBILE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1";
const TOU_TIAO_HOSTS = ["m.toutiao.com", "www.toutiao.com", "toutiao.com", "www.toutiaocdn.com"];
const TOU_TIAO_SHARE_URL_RE = /https?:\/\/(?:m\.|www\.)?toutiao\.com\/[^\s\]\)"'<>，。；;]+/gi;
const SHORT_CODE_RE = /(?:m\.toutiao\.com\/is\/|toutiao\.com\/is\/)([A-Za-z0-9]+)/i;
const GROUP_ID_RE = /(?:\/video\/|\/i|item\/)(\d{8,})/i;

function mobileHeaders(referer = "https://www.toutiao.com/") {
  return {
    "User-Agent": MOBILE_UA,
    Accept: "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    Referer: referer,
  };
}

export function isToutiaoShareUrl(url) {
  const u = String(url || "").toLowerCase();
  return TOU_TIAO_HOSTS.some((h) => u.includes(h));
}

export function extractToutiaoShareUrl(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  TOU_TIAO_SHARE_URL_RE.lastIndex = 0;
  const m = TOU_TIAO_SHARE_URL_RE.exec(raw);
  if (m) return m[0].replace(/[，。,.;；'"`]+$/g, "");
  const m2 = raw.match(SHORT_CODE_RE);
  if (m2) return `https://m.toutiao.com/is/${m2[1]}/`;
  return "";
}

function groupIdFromUrl(url) {
  const m = String(url || "").match(GROUP_ID_RE);
  return m ? m[1] : "";
}

async function resolveShareUrl(shareUrl) {
  const res = await httpGet(shareUrl.trim(), { headers: mobileHeaders() });
  return res.url;
}

async function fetchArticleInfo(groupId, referer) {
  const res = await httpGet(`https://m.toutiao.com/i${groupId}/info/`, {
    headers: mobileHeaders(referer),
    responseType: "json",
  });
  const data = res.data?.data;
  if (!data) throw new Error("头条接口未返回作品数据");
  return data;
}

function vodParamsFromToken(tokenB64) {
  if (!tokenB64) throw new Error("头条未返回播放令牌");
  let tokenJson;
  try {
    tokenJson = JSON.parse(atob(tokenB64));
  } catch {
    throw new Error("头条播放令牌解析失败");
  }
  const inner = String(tokenJson.GetPlayInfoToken || "");
  if (!inner) throw new Error("头条播放令牌无效");
  const params = {};
  for (const part of inner.split("&")) {
    const [k, v] = part.split("=");
    if (k && v) params[k] = decodeURIComponent(v);
  }
  return params;
}

function pickPlayUrl(vodPayload) {
  const items = vodPayload?.Result?.Data?.PlayInfoList;
  if (!Array.isArray(items) || !items.length) throw new Error("头条未返回可播放地址");
  const best = items
    .filter((x) => x?.MainPlayUrl)
    .sort((a, b) => Number(b.Bitrate || 0) - Number(a.Bitrate || 0))[0];
  if (!best) throw new Error("头条播放列表为空");
  return String(best.MainPlayUrl);
}

async function resolvePlayUrl(article, referer) {
  const token = String(article.play_auth_token_v2 || "");
  const params = vodParamsFromToken(token);
  const qs = new URLSearchParams(params).toString();
  const res = await httpGet(`https://vod.bytedanceapi.com/?${qs}`, {
    headers: mobileHeaders(referer),
    responseType: "json",
  });
  const playUrl = pickPlayUrl(res.data);
  const duration = Number(article.video_duration || 0);
  const cover = String(article.poster_url || "");
  return { playUrl, duration, cover };
}

export async function tryCrawlToutiao(shareText, onProgress) {
  let shareUrl = extractToutiaoShareUrl(shareText);
  if (!shareUrl) {
    const urls = extractAllHttpUrls(shareText).filter(isToutiaoShareUrl);
    shareUrl = urls[0] || "";
  }
  if (!shareUrl) return null;

  try {
    const finalUrl = await resolveShareUrl(shareUrl);
    const groupId = groupIdFromUrl(finalUrl) || groupIdFromUrl(shareUrl);
    if (!groupId) throw new Error("无法解析头条作品 ID");

    const article = await fetchArticleInfo(groupId, finalUrl);
    const { playUrl, duration, cover } = await resolvePlayUrl(article, finalUrl);
    const { buffer, size, finalUrl: dlUrl } = await downloadBinary(
      playUrl,
      mobileHeaders(finalUrl),
      onProgress
    );
    return {
      buffer,
      size,
      aweme_id: groupId,
      caption: "",
      play_url: dlUrl,
      duration,
      cover_url: cover,
      crawl_method: "toutiao_vod",
      watermark_free: true,
      source: "toutiao_crawl",
    };
  } catch (err) {
    console.info("头条爬取失败:", err);
    return null;
  }
}

export function isToutiaoShare(text) {
  return Boolean(extractToutiaoShareUrl(text)) || extractAllHttpUrls(text).some(isToutiaoShareUrl);
}
