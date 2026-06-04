# 红果短剧检索网站

输入短剧名称进行模糊搜索，展示短剧封面、简介和短剧 ID。

## 功能

- 关键词模糊搜索红果短剧
- 卡片展示：封面图、剧名、分类/集数、简介、短剧 ID
- 一键复制剧名、ID、简介
- 点击搜索结果卡片，新标签页打开红果短剧官网详情页
- **一键生成钩子视频**：选择分集 + 填写开场白/关键词，自动合成竖屏推广短视频并下载
- 输入防抖，自动搜索

## 快速启动

### macOS / Linux

```bash
./start.sh
# 或：
cd server
pip3 install -r requirements.txt
pip3 install -r requirements-browser.txt   # Playwright 自动登录达人中心（可选）
python3 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

未安装系统 ffmpeg 时，会自动使用 `imageio-ffmpeg` 自带二进制；也可 `brew install ffmpeg`。

### Windows（PowerShell）

```powershell
.\start.ps1
# 或：
cd server
py -3.12 -m pip install -r requirements.txt
py -3.12 -m pip install -r requirements-browser.txt
py -3.12 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

说明：Windows 上请用 `py -3.12 -m pip` / `py -3.12 -m uvicorn`（不要直接用 `pip`/`uvicorn`，以免找不到命令）。ffmpeg 由 `imageio-ffmpeg` 提供，无需单独加入 PATH。

生成视频依赖 `imageio-ffmpeg`（会自动下载 ffmpeg 二进制），首次生成需联网下载剧集片段，耗时约 1～3 分钟。

浏览器打开 http://localhost:8000

## 配置

通过环境变量 `HONGGUO_API_BASE` 可更换上游搜索接口地址（默认为公开代理接口）。

```bash
export HONGGUO_API_BASE="http://your-proxy/api/index.php"
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 正片素材（推广中心，推荐）

成片正片**仅**使用 [番茄达人中心](https://koc.fqopenplatform.com) 下载的**明文 MP4**（或你手动放到 `public/materials/fq_koc/{series_id}_{item_id}.mp4`）。不再使用红果加密源，也不会用预览雪碧图拼画面。

下载失败时，接口会按项提示：Cookie 未配置、msToken/a_bogus 过期、缺少 `HONGGUO_FQ_KOC_DOWNLOAD_BODY` 等。生成页顶部也会显示 `/api/config/status` 返回的说明。

### AI 一键剪辑（LM Studio，默认 gemma-4-e4b-it）

生成前会调用本机 **LM Studio**（OpenAI 兼容接口）分析剧名、简介、开场白与分集时长，输出剪辑方案并自动执行：

- 优化片头钩子文案、片尾搜索词
- 正片按 `trim_start_sec` + `duration_sec` 裁剪（推广向 30–90 秒，不再默认整集）

1. 安装并打开 [LM Studio](https://lmstudio.ai/)，加载模型 **gemma-4-e4b-it**（名称需与 `.env` 中 `QWEN_MODEL` 一致）。
2. 开启 **Local Server**（默认 `http://127.0.0.1:1234`）。
3. 项目根目录 `.env` 示例：

```bash
QWEN_API_BASE=http://127.0.0.1:1234/v1
QWEN_MODEL=gemma-4-e4b-it
QWEN_CODE_MODEL=gemma-4-e4b-it
QWEN_API_KEY=lm-studio
QWEN_TEMPERATURE=0
```

`./start.sh` 会自动加载 `.env`。生成页勾选「AI 一键剪辑」；也可单独预览方案：

若改回阿里云 DashScope，将 `QWEN_API_BASE` 与 `QWEN_API_KEY` 换成百炼控制台配置即可。

```bash
curl -X POST http://localhost:8000/api/ai/edit-plan \
  -H 'Content-Type: application/json' \
  -d '{"drama_title":"聚宝仙盆","opening":"开局就被退婚…","keyword":"聚宝仙盆","episode_item_ids":["7615470191303986238"],"series_id":"7615465407347952664"}'
```

说明：当前为**文本模型**方案（根据元数据推断节奏），未接入视频多模态；后续可扩展字幕/镜头分析。

### 番茄达人中心明文下载（推荐）

在 [番茄达人中心](https://koc.fqopenplatform.com/page/member/content?tab_type=6) 选剧后，可用官方 `batch_download` 接口下载**未加密**正片（例如《聚宝仙盆之杂灵根才是真BOSS》第 1 集）。

1. 浏览器登录 [koc.fqopenplatform.com](https://koc.fqopenplatform.com)，F12 → Network。
2. 在内容库点击某一集的「下载」，复制该请求的 **Cookie**，以及 URL 里的 **msToken**、**a_bogus**。
3. 启动前设置：

```bash
export HONGGUO_FQ_KOC_COOKIE='你的 Cookie'
export HONGGUO_FQ_KOC_MS_TOKEN='URL 中的 msToken'
export HONGGUO_FQ_KOC_A_BOGUS='URL 中的 a_bogus'
# 可选：整段下载 URL（过期后需重新复制）
# export HONGGUO_FQ_KOC_CREATE_URL='https://koc.fqopenplatform.com/api/platform/content/batch_download/create/v1?...'
```

4. 生成页默认勾选「推广中心素材」。若仍失败，可勾选「快手/抖音」作为备选，或手动放入本地 MP4。

单集测试（`series_id` / `item_id` 来自本站搜索分集接口）：

```bash
curl -X POST http://localhost:8000/api/material/fq-koc \
  -H 'Content-Type: application/json' \
  -d '{"series_id":"7615465407347952664","item_id":"7615470191303986238","drama_title":"聚宝仙盆之杂灵根才是真BOSS"}'
```

若 POST Body 与默认猜测不一致，在 Network 里复制 **Request Payload** 到环境变量（支持 `{book_id}`、`{item_id}` 占位符）：

```bash
export HONGGUO_FQ_KOC_DOWNLOAD_BODY='{"book_id":"{book_id}","item_id_list":["{item_id}"]}'
```

### 站外片源（快手 → 抖音，可选）

推广中心不可用时的备选：快手/抖音上的同剧剪辑。生成时会**先搜快手**，无结果再**自动搜抖音**。

**快手 Cookie**

1. 浏览器打开 [kuaishou.com](https://www.kuaishou.com) 并登录。
2. F12 → Network → 复制任意请求的 **Cookie**。
3. `export HONGGUO_KUAISHOU_COOKIE='…'`

**抖音 Cookie**（快手搜不到时使用）

1. 浏览器打开 [douyin.com](https://www.douyin.com) 并登录。
2. 同样复制 **Cookie**。
3. `export HONGGUO_DOUYIN_COOKIE='…'`

4. 生成页勾选「正片优先站外素材」，或粘贴快手/抖音 App 分享链接。

单独下载素材（先快手后抖音）：

```bash
curl -X POST http://localhost:8000/api/material/kuaishou \
  -H 'Content-Type: application/json' \
  -d '{"drama_title":"聚宝仙盆之杂灵根才是真BOSS","keyword":"聚宝仙盆"}'
```

仅抖音：`POST /api/material/douyin`。文件保存在 `public/materials/` 下对应子目录。

## 技术栈

- 后端：FastAPI + httpx
- 前端：原生 HTML / CSS / JavaScript
