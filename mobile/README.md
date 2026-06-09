# 红果短剧 · 浏览器访问（推荐）

**无需安装 App。** 在电脑启动 Python 后端，手机 / 电脑用浏览器直接打开即可。

## 电脑端（Windows / macOS）

```bash
cd server
py -3.12 -m uvicorn main:app --host 0.0.0.0 --port 8000
```

浏览器打开：`http://localhost:8000`

## 手机端（iPhone / Android）

1. 手机与电脑连接 **同一 WiFi**
2. 查电脑局域网 IP（Windows：`ipconfig`，macOS：`ifconfig` 或系统设置）
3. 手机 Safari / Chrome 打开：`http://192.168.x.x:8000`（换成你的 IP）
4. 可选：分享 → **加入主屏幕**，下次像 App 一样打开

## 各端功能

| 平台 | 图片批次 | 抖音视频 |
|------|----------|----------|
| Windows / macOS 浏览器 | 选文件夹 → 下载 ZIP | 下载视频 |
| 手机浏览器 | 多选图片 → **保存到相册**（系统分享菜单） | 下载后「存储到相册」 |

页头会显示当前平台（如 `Windows`、`iOS 浏览器`）。

## 关于 `mobile/` 目录

此为早期 Capacitor iOS 壳，**已不再需要**。日常使用请用浏览器访问，可忽略本目录。
