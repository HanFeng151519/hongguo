# 视频爬取 · iOS App（方案 2）

**独立 iOS App**：爬取逻辑运行在 App 内（JavaScript + Capacitor 原生 HTTP），**不需要**电脑上的 Python 后端。

支持平台：**抖音 · 快手 · 今日头条 · 小红书**

> 此目录仅在 `ios` 分支用于打包自用 App。日常使用完整功能请切回 `dev` 分支 + 浏览器访问。

## 架构

```
mobile/src/
  app.js              # UI
  crawl/
    http.js           # CapacitorHttp（绕过 CORS）
    douyin.js         # 抖音
    kuaishou.js       # 快手
    toutiao.js        # 头条
    xhs.js            # 小红书
    index.js          # 统一入口 crawlAndDownload
    storage.js        # 本地缓存 + 分享存相册
```

## 环境要求

- **macOS** + **Xcode**（编译 iOS 必需）
- Node.js 18+
- Apple 开发者账号（免费账号可 sideload 到自己设备，证书约 7 天续签）

## 打包步骤

```bash
cd mobile
npm install
npm run cap:sync      # 复制 src → www，同步 iOS 工程
npm run cap:ios       # 用 Xcode 打开
```

在 Xcode 中：

1. 选择你的 **Team**（Signing & Capabilities）
2. 连接 iPhone，选真机目标
3. **Product → Run**（或 ⌘R）

首次安装：iPhone **设置 → 通用 → VPN 与设备管理** → 信任开发者。

## 使用说明

1. 粘贴分享文案（含短链即可）
2. 点 **爬取视频**（解析 → 下载到 App 缓存）
3. 预览后点 **保存到相册** → 系统菜单选「储存视频」

### Cookie（可选）

- **抖音**：短链页面无法直访时在「Cookie」里粘贴浏览器 Cookie
- **小红书**：`xhslink` 解析失败时粘贴 `xiaohongshu.com` Cookie

Cookie 保存在 App 本地（Preferences），不会上传。

## 浏览器调试（可选）

在 `mobile/src` 改完后：

```bash
npm run build:web
npx serve www -p 5173
```

浏览器里 CORS 会限制部分平台，**以真机 App 为准**。

## 与 dev 分支的区别

| | dev（浏览器 + Python） | ios 分支（本 App） |
|--|----------------------|-------------------|
| 后端 | 电脑 Python | 无，逻辑在 App 内 |
| 去水印 / 剪辑 | ✅ | ❌ 未包含 |
| 视频爬取 | ✅ | ✅ |
| 需同一 WiFi | 是 | 否 |

## 限制

- 大视频（>100MB）下载占内存，建议 WiFi 下使用
- 部分抖音 / 小红书链接仍需 Cookie
- 未上架 App Store，仅自用 sideload
