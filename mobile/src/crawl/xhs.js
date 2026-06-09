import { downloadBinary, httpGet } from "./http.js";
import { extractAllHttpUrls } from "./utils.js";

const PC_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
const XHS_HOSTS = ["xhslink.com", "xiaohongshu.com", "xhs.cn"];
const XHS_SHARE_URL_RE =
  /https?:\/\/(?:www\.)?xhslink\.com\/[^\s\]\)"'<>，。；;]+|https?:\/\/(?:www\.)?xiaohongshu\.com\/[^\s\]\)"'<>，。；;]+/gi;
const NOTE_ID_RE = /\/(?:discovery\/item|explore)\/([a-f0-9]{24})/i;

export function isXhsShareUrl(url) {
  const u = String(url || "").toLowerCase();
  return XHS_HOSTS.some((h) => u.includes(h));
}

export function extractXhsShareUrl(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  XHS_SHARE_URL_RE.lastIndex = 0;
  const m = XHS_SHARE_URL_RE.exec(raw);
  if (m) return m[0].replace(/[，。,.;；'"`]+$/g, "");
  for (const u of extractAllHttpUrls(raw)) {
    if (isXhsShareUrl(u)) return u;
  }
  return "";
}

function noteIdFromUrl(url) {
  const m = String(url || "").match(NOTE_ID_RE);
  return m ? m[1] : "";
}

function parseInitialState(html) {
  const m = String(html || "").match(/window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*<\/script>/s);
  if (!m) throw new Error("小红书页面未返回笔记数据");
  const raw = m[1].replace(/undefined/g, "null");
  try {
    return JSON.parse(raw);
  } catch {
    throw new Error("小红书页面数据解析失败");
  }
}

function scoreVideoUrl(url) {
  const u = String(url || "").toLowerCase();
  if (!u.includes(".mp4") && !u.includes("/stream/")) return -100;
  if (u.includes("webpic") || u.endsWith(".js") || u.includes("!nd_")) return -100;
  let score = 0;
  if (u.includes(".mp4")) score += 50;
  if (u.includes("sns-video")) score += 30;
  if (u.includes("/stream/")) score += 20;
  if (u.includes("sign=")) score += 5;
  if (u.includes("sns-bak")) score -= 10;
  return score;
}

function collectVideoUrls(state) {
  const found = [];
  const walk = (obj, depth = 0) => {
    if (!obj || depth > 16) return;
    if (Array.isArray(obj)) {
      obj.forEach((item) => walk(item, depth + 1));
      return;
    }
    if (typeof obj !== "object") return;
    for (const [key, val] of Object.entries(obj)) {
      if ((key === "masterUrl" || key === "master_url") && typeof val === "string" && val.startsWith("http")) {
        found.push(val.replace(/\\u002F/g, "/"));
      }
      if (key === "backupUrls" && Array.isArray(val)) {
        val.forEach((item) => {
          if (typeof item === "string" && item.startsWith("http")) found.push(item.replace(/\\u002F/g, "/"));
        });
      }
      if (key === "stream" && val && typeof val === "object") {
        for (const codecVal of Object.values(val)) {
          if (Array.isArray(codecVal)) {
            codecVal.forEach((item) => {
              const u = item?.masterUrl;
              if (typeof u === "string" && u.startsWith("http")) found.push(u.replace(/\\u002F/g, "/"));
            });
          }
        }
      }
      walk(val, depth + 1);
    }
  };
  walk(state);
  const seen = new Set();
  const out = [];
  for (const u of found) {
    if (!seen.has(u) && scoreVideoUrl(u) >= 0) {
      seen.add(u);
      out.push(u);
    }
  }
  out.sort((a, b) => scoreVideoUrl(b) - scoreVideoUrl(a));
  return out;
}

function metaFromState(state, noteId) {
  const noteMap = state?.note?.noteDetailMap || {};
  const detail = noteMap[noteId] || {};
  const note = detail.note || {};
  const title = String(note.title || note.desc || "").slice(0, 200);
  let durationMs = 0;
  const video = note.video || {};
  const consumer = video.consumer || {};
  let origin = consumer.originVideoKey || consumer.origin_video_key;
  if (origin && typeof origin === "object") durationMs = Number(origin.duration || 0);
  return {
    caption: title,
    duration: durationMs > 100 ? durationMs / 1000 : 0,
    note_type: String(note.type || ""),
    cover_url:
      note.cover && typeof note.cover === "object" ? String(note.cover.urlDefault || "") : "",
  };
}

async function fetchNotePage(shareUrl, cookie) {
  const headers = {
    "User-Agent": PC_UA,
    Referer: "https://www.xiaohongshu.com/",
    Accept: "text/html,application/xhtml+xml,*/*;q=0.8",
  };
  if (cookie) headers.Cookie = cookie;
  const res = await httpGet(shareUrl.trim(), { headers });
  const final = res.url;
  let noteId = noteIdFromUrl(final) || noteIdFromUrl(shareUrl);
  const state = parseInitialState(String(res.data || ""));
  if (!noteId) {
    const noteMap = state?.note?.noteDetailMap || {};
    const keys = Object.keys(noteMap);
    if (keys.length === 1) noteId = keys[0];
  }
  if (!noteId) throw new Error("无法解析小红书笔记 ID");
  return { finalUrl: final, noteId, state };
}

export async function tryCrawlXhs(shareText, xhsCookie, onProgress) {
  let shareUrl = extractXhsShareUrl(shareText);
  if (!shareUrl) {
    const urls = extractAllHttpUrls(shareText).filter(isXhsShareUrl);
    shareUrl = urls[0] || "";
  }
  if (!shareUrl) return null;

  try {
    const { noteId, state } = await fetchNotePage(shareUrl, xhsCookie);
    const meta = metaFromState(state, noteId);
    if (meta.note_type === "normal") {
      throw new Error("该笔记为图文，不含视频。请换一条视频笔记链接。");
    }
    const plays = collectVideoUrls(state);
    if (!plays.length) throw new Error("未能从小红书笔记解析出视频地址");

    const headers = {
      "User-Agent": PC_UA,
      Referer: "https://www.xiaohongshu.com/",
      Accept: "*/*",
    };
    if (xhsCookie) headers.Cookie = xhsCookie;

    let lastErr = null;
    for (const playUrl of plays.slice(0, 4)) {
      try {
        const { buffer, size, finalUrl } = await downloadBinary(playUrl, headers, onProgress);
        return {
          buffer,
          size,
          aweme_id: noteId,
          caption: meta.caption,
          play_url: finalUrl,
          duration: meta.duration,
          cover_url: meta.cover_url,
          crawl_method: "xhs_page",
          watermark_free: true,
          source: "xhs_crawl",
        };
      } catch (err) {
        lastErr = err;
      }
    }
    throw lastErr || new Error("小红书视频下载失败");
  } catch (err) {
    console.info("小红书爬取失败:", err);
    return null;
  }
}

export function isXhsShare(text) {
  return Boolean(extractXhsShareUrl(text)) || extractAllHttpUrls(text).some(isXhsShareUrl);
}
