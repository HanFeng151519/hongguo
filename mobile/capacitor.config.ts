import type { CapacitorConfig } from "@capacitor/cli";

/** iOS 独立 App：前端 + 爬取逻辑均内置，无需连接电脑后端。 */
const config: CapacitorConfig = {
  appId: "com.hongguo.crawl",
  appName: "视频爬取",
  webDir: "www",
  ios: {
    contentInset: "automatic",
    allowsLinkPreview: true,
  },
};

export default config;
