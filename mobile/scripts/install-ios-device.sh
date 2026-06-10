#!/bin/bash
# 同步前端并打开 Xcode，真机安装请用 Xcode ▶ Run（命令行 devicectl 易卡在 Developer Disk Image）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "→ 同步 Web 资源到 iOS…"
npm run cap:sync

echo ""
echo "→ 打开 Xcode 工程…"
open ios/App/App.xcworkspace

cat <<'EOF'

══════════════════════════════════════════════
  真机安装步骤（首次约 2 分钟）
══════════════════════════════════════════════

1. iPhone 用数据线连 Mac，解锁手机，点「信任此电脑」

2. iPhone 打开：设置 → 隐私与安全性 → 开发者模式 → 开启
   （若无此项，先在 Xcode 里对手机点一次 Run 才会出现）

3. Xcode 左侧点蓝色「App」→ Signing & Capabilities：
   ✓ Automatically manage signing
   Team 选：380483995@qq.com (4NZYNRM6BY)

4. 顶部设备选：韩丰的iPhone（不要选 Simulator）

5. 点左上角 ▶ Run，等待编译并安装

6. 若提示「Untrusted Developer」：
   iPhone → 设置 → 通用 → VPN与设备管理 → 信任开发者

7. 桌面出现「视频爬取」App 即成功

⚠ 不要用命令行 devicectl 安装，容易卡在
  “Enabling developer disk image services”
  用 Xcode ▶ Run 最可靠。

EOF
