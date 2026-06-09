import type { CapacitorConfig } from "@capacitor/cli";

/**
 * 两种用法（二选一）：
 *
 * 1) 远程加载（推荐调试）
 *    取消注释 server.url，填电脑局域网 IP，手机与电脑同一 WiFi。
 *    App 内直接打开 http://192.168.x.x:8000，无需改前端。
 *
 * 2) 打包内置页面
 *    保持 server.url 注释，执行 npm run cap:sync。
 *    在手机 App 设置里填「服务器地址」指向后端。
 */
const config: CapacitorConfig = {
  appId: "com.hongguo.drama",
  appName: "红果短剧",
  webDir: "www",
  server: {
    url: "http://172.31.2.23:8000",
    cleartext: true,
    androidScheme: "https",
  },
  ios: {
    contentInset: "automatic",
    allowsLinkPreview: true,
  },
};

export default config;
