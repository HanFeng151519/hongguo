import { crawlAndDownload } from "./index.js";

window.HongguoCrawl = {
  crawlAndDownload,
  useLocalCrawl() {
    return Boolean(window.Capacitor?.isNativePlatform?.());
  },
};

window.dispatchEvent(new Event("hongguo-crawl-ready"));
