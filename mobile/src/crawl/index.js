import { tryCrawlDouyin } from "./douyin.js";
import { tryCrawlKuaishou } from "./kuaishou.js";
import { tryCrawlToutiao } from "./toutiao.js";
import { tryCrawlXhs } from "./xhs.js";
import { extractAllHttpUrls } from "./utils.js";

/**
 * 与 Python crawl_and_download 相同顺序：快手 → 头条 → 小红书 → 抖音
 */
export async function crawlAndDownload(shareText, options = {}) {
  const { douyinCookie = "", xhsCookie = "", onProgress } = options;
  const text = String(shareText || "").trim();
  if (!text) throw new Error("请粘贴分享文案或链接");
  if (!extractAllHttpUrls(text).length) {
    throw new Error("文案中未找到 http 链接，请粘贴含 https:// 的分享内容");
  }

  const ks = await tryCrawlKuaishou(text, onProgress);
  if (ks) return ks;

  const tt = await tryCrawlToutiao(text, onProgress);
  if (tt) return tt;

  const xhs = await tryCrawlXhs(text, xhsCookie, onProgress);
  if (xhs) return xhs;

  const dy = await tryCrawlDouyin(text, douyinCookie, onProgress);
  if (dy) return dy;

  throw new Error("无法获取视频。请确认链接有效，或填写可选 Cookie 后重试。");
}

export { extractAllHttpUrls };
