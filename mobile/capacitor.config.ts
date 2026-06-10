import type { CapacitorConfig } from "@capacitor/cli";

/** iOS 独立 App：前端 + 爬取逻辑均内置，无需连接电脑后端。 */
const config: CapacitorConfig = {
  appId: "com.hongguo.crawl",
  appName: "视频爬取",
  webDir: "www",
  /** cap sync 会重写此列表；patch-ios-plugins.js 会在 sync 后补回 RealEsrganPlugin */
  packageClassList: ["RealEsrganPlugin"],
  ios: {
    contentInset: "automatic",
    allowsLinkPreview: true,
  },
  plugins: {
    CapacitorHttp: {
      enabled: true,
    },
  },
};

export default config;
