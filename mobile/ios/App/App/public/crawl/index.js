import { tryCrawlDouyin, isDouyinShare } from "./douyin.js";
import { tryCrawlKuaishou, isKuaishouShare } from "./kuaishou.js";
import { tryCrawlToutiao, isToutiaoShare } from "./toutiao.js";
import { tryCrawlXhs, isXhsShare } from "./xhs.js";
import { tryCrawlBaidu, isBaiduShare } from "./baidu.js";
import { extractAllHttpUrls } from "./utils.js";

const PLATFORM_LABEL = {
  douyin: "抖音",
  kuaishou: "快手",
  toutiao: "头条",
  xhs: "小红书",
  baidu: "百度",
};

function detectPlatform(text) {
  if (isBaiduShare(text)) return "baidu";
  if (isDouyinShare(text)) return "douyin";
  if (isKuaishouShare(text)) return "kuaishou";
  if (isToutiaoShare(text)) return "toutiao";
  if (isXhsShare(text)) return "xhs";
  return null;
}

/**
 * 识别平台后优先尝试对应爬虫；失败时给出该平台的具体错误。
 */
export async function crawlAndDownload(shareText, options = {}) {
  const { douyinCookie = "", xhsCookie = "", onProgress } = options;
  const text = String(shareText || "").trim();
  if (!text) throw new Error("请粘贴分享文案或链接");
  if (!extractAllHttpUrls(text).length) {
    throw new Error("文案中未找到 http 链接，请粘贴含 https:// 的分享内容");
  }

  const runners = {
    baidu: () => tryCrawlBaidu(text, onProgress),
    kuaishou: () => tryCrawlKuaishou(text, onProgress),
    toutiao: () => tryCrawlToutiao(text, onProgress),
    xhs: () => tryCrawlXhs(text, xhsCookie, onProgress),
    douyin: () => tryCrawlDouyin(text, douyinCookie, onProgress),
  };

  const platform = detectPlatform(text);
  const order = platform
    ? [platform, ...Object.keys(runners).filter((k) => k !== platform)]
    : Object.keys(runners);

  let lastErr = null;
  for (const key of order) {
    try {
      const result = await runners[key]();
      if (result) return result;
    } catch (err) {
      lastErr = err;
      console.warn(`[crawl:${key}]`, err);
      if (platform && key === platform) {
        throw err;
      }
    }
  }

  const label = platform ? PLATFORM_LABEL[platform] : "";
  const hint = douyinCookie || xhsCookie ? "" : " 可展开填写对应平台 Cookie 后重试。";
  const base = lastErr?.message || (label ? `${label}解析失败，请确认链接有效` : "无法获取视频");
  throw new Error(base + hint);
}

export { extractAllHttpUrls };
