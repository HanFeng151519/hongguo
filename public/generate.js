const params = new URLSearchParams(location.search);
const seriesId = params.get("series_id") || "";
const dramaTitle = params.get("title") || "短剧";
const dramaIntro = params.get("intro") || "";
const coverUrl = params.get("cover") || "";
const MAX_SELECT = 6;

const titleEl = document.getElementById("drama-title");
const metaEl = document.getElementById("drama-meta");
const panelTitleEl = document.getElementById("episode-panel-title");
const keywordEl = document.getElementById("keyword");
const btnPreviewSplash = document.getElementById("btn-preview-splash");
const splashPreviewEl = document.getElementById("splash-preview");
const splashTitleFontEl = document.getElementById("splash-title-font");
const splashSubtitleFontEl = document.getElementById("splash-subtitle-font");
const splashBadgeEl = document.getElementById("splash-badge");

const SPLASH_FONT_LS_KEY = "hongguo_splash_fonts";
const SPLASH_TITLE_FONT_DEFAULT = 120;
const SPLASH_SUBTITLE_FONT_DEFAULT = 70;

function loadSplashFontsFromStorage() {
  try {
    const raw = localStorage.getItem(SPLASH_FONT_LS_KEY);
    if (!raw) return;
    const o = JSON.parse(raw);
    const t = o.splash_title_font ?? o.title_font;
    const s = o.splash_subtitle_font ?? o.subtitle_font;
    if (splashTitleFontEl && t != null) {
      splashTitleFontEl.value = String(t);
    }
    if (splashSubtitleFontEl && s != null) {
      splashSubtitleFontEl.value = String(s);
    }
    if (splashBadgeEl && o.splash_badge != null) {
      splashBadgeEl.value = String(o.splash_badge);
    }
  } catch {
    /* ignore */
  }
}

function saveSplashFontsToStorage() {
  const fonts = getSplashFontSizes();
  try {
    localStorage.setItem(
      SPLASH_FONT_LS_KEY,
      JSON.stringify({ ...fonts, splash_badge: getSplashBadge() })
    );
  } catch {
    /* ignore */
  }
}

function getSplashBadge() {
  return (splashBadgeEl?.value || "").trim().slice(0, 24);
}

function getSplashFontSizes() {
  const title = parseInt(splashTitleFontEl?.value, 10);
  const subtitle = parseInt(splashSubtitleFontEl?.value, 10);
  return {
    splash_title_font: Number.isFinite(title) ? title : SPLASH_TITLE_FONT_DEFAULT,
    splash_subtitle_font: Number.isFinite(subtitle)
      ? subtitle
      : SPLASH_SUBTITLE_FONT_DEFAULT,
    splash_badge: getSplashBadge(),
  };
}

const useAiEditEl = document.getElementById("use-ai-edit");
const useFqKocEl = document.getElementById("use-fq-koc");
const episodeUrlStatus = new Map();
const fqKocImportStatusEl = document.getElementById("fq-koc-import-status");
const configStatusEl = document.getElementById("config-status");

// 防止同一集被并发重复缓存（点击过快/多次触发）。
const cacheInProgress = new Set();

const episodeStatusEl = document.getElementById("episode-status");
const episodeGridEl = document.getElementById("episode-grid");
const btnGenerate = document.getElementById("btn-generate");
const generateHint = document.getElementById("generate-hint");
const resultPanel = document.getElementById("result-panel");
const resultMsg = document.getElementById("result-msg");
const postCaptionBox = document.getElementById("post-caption-box");
const postCaptionEl = document.getElementById("post-caption");
const btnCopyCaption = document.getElementById("btn-copy-caption");
const previewEl = document.getElementById("preview");
const downloadLink = document.getElementById("download-link");
let episodes = [];
const selected = new Set();
let lastPreviewUrl = "";
let lastDownloadUrl = "";
let previewObjectUrl = "";

titleEl.textContent = dramaTitle;
if (keywordEl && !keywordEl.value.trim()) {
  keywordEl.value = "";
}
metaEl.textContent = dramaIntro
  ? dramaIntro.slice(0, 120) + (dramaIntro.length > 120 ? "…" : "")
  : `ID: ${seriesId}`;

async function loadServiceConfig(itemId = "") {
  if (!configStatusEl) return;
  try {
    const params = new URLSearchParams();
    if (seriesId) params.set("series_id", seriesId);
    const epId =
      itemId || (selected.size === 1 ? [...selected][0] : "");
    if (epId) params.set("item_id", epId);
    const qs = params.toString() ? `?${params}` : "";
    const res = await fetch(`/api/config/status${qs}`);
    const data = await res.json();
    const llm = data.llm || {};
    const parts = [];
    if (llm.configured) {
      parts.push(
        `${llm.provider || "LLM"} 已配置（${llm.model || "默认模型"} @ ${llm.base || ""}）`
      );
    } else {
      parts.push("LLM 未配置：请启动 LM Studio 或填写 .env 的 QWEN_API_KEY");
    }
    const sess = data.fq_koc?.session;
    if (data.fq_koc?.auto_sync && data.fq_koc?.browser_sync) {
      parts.push(
        data.fq_koc?.browser_profile_ready
          ? "Playwright 已开启（AUTO_SYNC=1），失败时会自动拉片"
          : "Playwright 已开启：可点「自动登录并同步权限」"
      );
    } else if (data.fq_koc?.ready) {
      parts.push("推荐：用 .env / 导入 F12 抓包下载，不弹浏览器（AUTO_SYNC=0）");
    }
    if (sess?.loaded) {
      parts.push(
        `已自动加载抓包配置（${sess.updated_at || "已保存"}，msToken 请求后会刷新）`
      );
    }
    // 极简状态：不展示抓包细节/手动兜底指引（页面已移除相关功能）。
    const fq = data.fq_koc || {};
    const auto = fq.auto_sync ? "自动缓存：开" : "自动缓存：关";
    const ready = fq.ready ? "Cookie：已配置" : "Cookie：未配置";
    const browser = fq.browser_sync ? "浏览器：可用" : "浏览器：不可用";
    configStatusEl.textContent = [auto, ready, browser].filter(Boolean).join("；");
  } catch {
    configStatusEl.textContent = "";
  }
}

loadSplashFontsFromStorage();
loadServiceConfig();
refreshSplashPreview();
useFqKocEl?.addEventListener("change", loadServiceConfig);
keywordEl?.addEventListener("input", () => {
  updateGenerateState();
  scheduleSplashPreview();
});

let splashPreviewTimer = 0;
function splashPreviewUrl() {
  const kw = (keywordEl?.value || "").trim();
  const { splash_title_font, splash_subtitle_font } = getSplashFontSizes();
  const badge = getSplashBadge();
  const q = new URLSearchParams({
    keyword: kw,
    title_font: String(splash_title_font),
    subtitle_font: String(splash_subtitle_font),
    _: String(Date.now()),
  });
  if (badge) q.set("badge", badge);
  return `/api/generate/splash-preview.png?${q}`;
}

function refreshSplashPreview() {
  if (!splashPreviewEl) return;
  const kw = (keywordEl?.value || "").trim();
  if (!kw) {
    splashPreviewEl.classList.add("hidden");
    splashPreviewEl.removeAttribute("src");
    return;
  }
  splashPreviewEl.src = splashPreviewUrl();
  splashPreviewEl.classList.remove("hidden");
}

function scheduleSplashPreview() {
  clearTimeout(splashPreviewTimer);
  splashPreviewTimer = setTimeout(refreshSplashPreview, 400);
}

btnPreviewSplash?.addEventListener("click", refreshSplashPreview);
splashTitleFontEl?.addEventListener("input", () => {
  saveSplashFontsToStorage();
  scheduleSplashPreview();
});
splashSubtitleFontEl?.addEventListener("input", () => {
  saveSplashFontsToStorage();
  scheduleSplashPreview();
});
splashBadgeEl?.addEventListener("input", () => {
  saveSplashFontsToStorage();
  scheduleSplashPreview();
});

function updateGenerateState() {
  const hasKeyword = Boolean(keywordEl?.value.trim());
  const hasEpisodes = selected.size > 0;
  const ready = seriesId && hasEpisodes && hasKeyword;
  btnGenerate.disabled = !ready;
  if (generateHint) {
    if (!hasKeyword) {
      generateHint.textContent = "请填写片头关键词（1 秒《关键词》片头）后再生成";
    } else if (!hasEpisodes) {
      generateHint.textContent = "请选择至少 1 集";
    } else {
      generateHint.textContent =
        "AI 只留最精彩：片头 → 解说卡 → 多集快切正片（约2分30–3分30）→ 片尾";
    }
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

async function readJsonResponse(res) {
  const text = await res.text();
  if (!text) {
    return { data: {}, raw: "" };
  }
  try {
    return { data: JSON.parse(text), raw: text };
  } catch {
    const snippet = text.replace(/\s+/g, " ").slice(0, 280);
    if (!res.ok) {
      throw new Error(snippet || `服务器错误 HTTP ${res.status}`);
    }
    throw new Error(`服务器返回非 JSON：${snippet}`);
  }
}

function absoluteUrl(path) {
  if (!path) return "";
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  return `${location.origin}${path.startsWith("/") ? "" : "/"}${path}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollGenerateJob(jobId) {
  const started = Date.now();
  while (true) {
    let res;
    try {
      res = await fetch(`/api/generate/job/${encodeURIComponent(jobId)}`);
    } catch (e) {
      throw new Error(
        "与服务器连接中断（生成可能仍在后台进行）。请稍等 1–2 分钟后刷新页面重试查看，或减少选集数量。"
      );
    }
    const { data } = await readJsonResponse(res);
    if (!res.ok) {
      throw new Error(data.detail || "查询生成进度失败");
    }
    if (data.progress && resultMsg) {
      resultMsg.textContent = data.progress;
    }
    if (data.status === "completed") {
      return data;
    }
    if (data.status === "failed") {
      throw new Error(data.error || "生成失败");
    }
    if (Date.now() - started > 7200_000) {
      throw new Error("生成超时（超过 2 小时），请减少集数后重试");
    }
    await sleep(2500);
  }
}

function showPostCaption(plan) {
  const text = (plan?.post_caption || "").trim();
  if (!text || !postCaptionBox || !postCaptionEl) {
    postCaptionBox?.classList.add("hidden");
    return;
  }
  let memeHint = "";
  if (plan?.edit_style === "meme") {
    const caps = (plan.body_segments || [])
      .flatMap((s) => s.meme_captions || [])
      .slice(0, 6)
      .map((c) => (typeof c === "string" ? c : c.text))
      .filter(Boolean);
    if (caps.length) {
      memeHint = `\n\n【Meme 字幕】${caps.join(" · ")}`;
    }
  }
  postCaptionEl.textContent = text + memeHint;
  postCaptionBox.classList.remove("hidden");
}

function triggerFileDownload(url, filename) {
  const a = document.createElement("a");
  a.href = absoluteUrl(url);
  a.download = filename || "hook_video.mp4";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function loadPreviewVideo(previewUrl) {
  if (previewObjectUrl) {
    URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl = "";
  }

  const full = absoluteUrl(previewUrl);
  try {
    const res = await fetch(full);
    if (!res.ok) throw new Error(`预览加载失败 (${res.status})`);
    const blob = await res.blob();
    if (!blob.size || blob.size < 50_000) {
      throw new Error("预览视频为空，正片可能未合成成功");
    }
    previewObjectUrl = URL.createObjectURL(blob);
    previewEl.src = previewObjectUrl;
    previewEl.load();
    previewEl.classList.remove("hidden");
    previewEl.onerror = () => {
      resultMsg.textContent +=
        " 当前浏览器可能无法解码，请点下载后用 QuickTime 打开。";
    };
  } catch (err) {
    previewEl.src = full;
    previewEl.classList.remove("hidden");
    throw err;
  }
}

function renderEpisodes() {
  if (!episodes.length) {
    episodeGridEl.innerHTML = "";
    return;
  }

  episodeGridEl.innerHTML = episodes
    .map(
      (ep) => {
        const st = episodeUrlStatus.get(ep.item_id) || "";
        const badge =
          st === "local"
            ? " · 已缓存"
            : st === "url"
              ? " · 有地址"
              : st === "pending"
                ? " · …"
                : "";
        return `
    <button type="button" class="episode-chip${selected.has(ep.item_id) ? " active" : ""}"
      data-id="${escapeHtml(ep.item_id)}" title="分集 ID: ${escapeHtml(ep.item_id)}">
      ${escapeHtml(ep.title || "未命名")}${badge}
    </button>
  `;
      }
    )
    .join("");

  episodeGridEl.querySelectorAll(".episode-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.id;
      if (selected.has(id)) {
        selected.delete(id);
        btn.classList.remove("active");
      } else {
        if (selected.size >= MAX_SELECT) {
          generateHint.textContent = `最多选择 ${MAX_SELECT} 集`;
          return;
        }
        selected.add(id);
        btn.classList.add("active");
      }
      generateHint.textContent =
        selected.size === 1
          ? "已选 1 集：AI 剪辑大师只留最精彩片段"
          : `已选 ${selected.size} 集：AI 只留最精彩，拼成约 2分30–3分30 钩子`;
      if (selected.size === 1) {
        loadServiceConfig(id);
        // 选中 1 集时尝试自动把该集 CDN MP4 缓存到本地（失败则提示手动/导入）。
        resolveEpisodeUrl(id, { quiet: false, autoCache: true });
      }
      updateGenerateState();
    });
  });
}

async function cacheEpisodeFromMp4Url(itemId, mp4Url) {
  if (!seriesId || !itemId) return null;
  if (!mp4Url || !mp4Url.startsWith("http")) return null;
  if (episodeUrlStatus.get(itemId) === "local") return null;
  if (cacheInProgress.has(itemId)) return null;

  cacheInProgress.add(itemId);
  try {
    if (fqKocImportStatusEl) {
      fqKocImportStatusEl.textContent = `正在缓存本集到本地（第 ${itemId} 集…）`;
    }
    const res = await fetch("/api/material/fq-koc/cache-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        series_id: seriesId,
        item_id: itemId,
        mp4_url: mp4Url,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.message || "缓存失败");

    episodeUrlStatus.set(itemId, "local");
    renderEpisodes();
    await loadServiceConfig(itemId);

    if (fqKocImportStatusEl) {
      const mb = data.size ? (data.size / (1024 * 1024)).toFixed(1) : "";
      fqKocImportStatusEl.textContent = mb
        ? `已缓存本集：${mb}MB，可直接生成`
        : "已缓存本集，可直接生成";
    }
    return data;
  } catch (e) {
    if (fqKocImportStatusEl) {
      fqKocImportStatusEl.textContent = String(e.message || e);
    }
    return null;
  } finally {
    cacheInProgress.delete(itemId);
  }
}

async function resolveEpisodeUrl(itemId, { quiet = false, autoCache = false } = {}) {
  if (!seriesId || !itemId) return null;
  episodeUrlStatus.set(itemId, "pending");
  renderEpisodes();
  try {
    let url = `/api/material/fq-koc/download-url?series_id=${encodeURIComponent(seriesId)}&item_id=${encodeURIComponent(itemId)}`;
    // autoCache 时，为了尽量“浏览器能下载就自动缓存”，允许接口失败后尝试 Playwright 抓取 CDN MP4 地址。
    if (autoCache) url += `&try_browser=1`;
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "解析失败");
    if (data.ok && data.cached) {
      episodeUrlStatus.set(itemId, "local");
      if (!quiet && fqKocImportStatusEl) {
        fqKocImportStatusEl.textContent = "本集已有本地缓存，可直接生成";
      }
      await loadServiceConfig(itemId);
      renderEpisodes();
      return data;
    }
    if (data.ok && data.download_url) {
      episodeUrlStatus.set(itemId, "url");
      if (!quiet && fqKocImportStatusEl) {
        fqKocImportStatusEl.textContent = `已获取下载地址（${data.source}）`;
      }
      renderEpisodes();
      if (autoCache) {
        // 直接把该集 CDN MP4 缓存到本地；后续生成会复用。
        await cacheEpisodeFromMp4Url(itemId, data.download_url);
      }
      return data;
    }
    episodeUrlStatus.delete(itemId);
    renderEpisodes();
    if (!quiet && fqKocImportStatusEl) {
      fqKocImportStatusEl.textContent =
        data.message ||
        "未拿到可用的 MP4 地址，请稍后重试";
    }
    return data;
  } catch (e) {
    episodeUrlStatus.delete(itemId);
    renderEpisodes();
    if (!quiet && fqKocImportStatusEl) {
      fqKocImportStatusEl.textContent = String(e.message || e);
    }
    return null;
  }
}

async function fetchEpisodeList() {
  const urls = [
    `/api/episodes?series_id=${encodeURIComponent(seriesId)}`,
    `/api/series/${encodeURIComponent(seriesId)}/episodes`,
  ];

  let lastError = "加载分集失败";
  for (const url of urls) {
    try {
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok) {
        lastError = data.detail || lastError;
        continue;
      }
      return data;
    } catch (err) {
      lastError = err.message || lastError;
    }
  }
  throw new Error(lastError);
}

async function loadEpisodes() {
  if (!seriesId) {
    episodeStatusEl.innerHTML = "缺少短剧 ID，请从检索页点击「一键生成」进入";
    return;
  }

  selected.clear();
  episodeUrlStatus.clear();

  episodeStatusEl.textContent = "正在从红果拉取全部分集列表…";
  episodeGridEl.innerHTML = "";

  try {
    const data = await fetchEpisodeList();
    episodes = data.episodes || [];

    if (!episodes.length) {
      episodeStatusEl.textContent = "未获取到分集，请稍后重试";
      if (panelTitleEl) panelTitleEl.textContent = "选择剧集";
      return;
    }

    if (panelTitleEl) {
      panelTitleEl.textContent = `选择剧集（共 ${episodes.length} 集，可多选，最多 ${MAX_SELECT} 集）`;
    }
    episodeStatusEl.textContent = `已加载 ${episodes.length} 集；选集后 AI 自动剪辑并写推文`;
    renderEpisodes();
    generateHint.textContent = "请选择要用于推广成片的分集";
    updateGenerateState();
  } catch (err) {
    episodeStatusEl.innerHTML = `${escapeHtml(err.message || "加载分集失败")} <button type="button" class="btn-ghost" id="retry-load">重试</button>`;
    document.getElementById("retry-load")?.addEventListener("click", loadEpisodes);
  }
}

document.getElementById("select-all").addEventListener("click", () => {
  selected.clear();
  const limit = Math.min(episodes.length, MAX_SELECT);
  episodes.slice(0, limit).forEach((ep) => selected.add(ep.item_id));
  renderEpisodes();
  generateHint.textContent =
    episodes.length > MAX_SELECT
      ? `已选前 ${MAX_SELECT} 集（本剧共 ${episodes.length} 集）`
      : `已全选 ${selected.size} 集`;
  updateGenerateState();
});

document.getElementById("clear-all").addEventListener("click", () => {
  selected.clear();
  renderEpisodes();
  generateHint.textContent = "请选择至少 1 集";
  updateGenerateState();
});

btnCopyCaption?.addEventListener("click", async () => {
  const text = postCaptionEl?.textContent?.trim();
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    btnCopyCaption.textContent = "已复制";
    setTimeout(() => {
      btnCopyCaption.textContent = "复制文案";
    }, 2000);
  } catch {
    resultMsg.textContent += " 复制失败，请手动选中文案复制。";
  }
});

downloadLink.addEventListener("click", (e) => {
  if (!lastDownloadUrl) return;
  e.preventDefault();
  triggerFileDownload(lastDownloadUrl, `${dramaTitle}_钩子.mp4`);
});

btnGenerate.addEventListener("click", async () => {
  if (btnGenerate.disabled) return;

  const keyword = (keywordEl?.value || "").trim();
  if (!keyword) {
    resultPanel.classList.remove("hidden");
    resultMsg.textContent = "请先填写片头关键词（显示为《关键词》的 1 秒片头）";
    keywordEl?.focus();
    return;
  }

  btnGenerate.disabled = true;
  btnGenerate.textContent = "正在生成，请稍候…";
  resultPanel.classList.remove("hidden");
  postCaptionBox?.classList.add("hidden");
  resultMsg.textContent =
    "AI 正在写 Meme 梗字幕与裁剪正片，请耐心等待…";
  previewEl.classList.add("hidden");
  previewEl.removeAttribute("src");
  downloadLink.classList.add("hidden");
  lastPreviewUrl = "";
  lastDownloadUrl = "";

  try {
    const res = await fetch("/api/generate/hook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        series_id: seriesId,
        drama_title: dramaTitle,
        cover_url: coverUrl,
        keyword,
        ...getSplashFontSizes(),
        episode_item_ids: Array.from(selected),
        use_ai_edit: Boolean(useAiEditEl?.checked),
        drama_intro: dramaIntro,
        use_fq_koc_material: useFqKocEl ? useFqKocEl.checked !== false : true,
      }),
    });

    const { data: startData } = await readJsonResponse(res);
    if (!res.ok) {
      const d = startData.detail;
      const detail = Array.isArray(d)
        ? d.map((x) => x.msg || x).join("；")
        : d || "生成失败";
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }

    if (!startData.ok || !startData.job_id) {
      throw new Error("服务器未返回任务 ID，请重试");
    }

    resultMsg.textContent =
      startData.progress || "已提交生成任务，正在后台处理（请勿关闭页面）…";

    const data = await pollGenerateJob(startData.job_id);

    if (!data.ok || !data.preview_url) {
      throw new Error("服务器未返回预览地址，请重试");
    }
    if (!data.size || data.size < 100_000) {
      throw new Error(`视频过小（${data.size || 0} 字节），剧集可能未下载成功`);
    }

    lastPreviewUrl = data.preview_url;
    lastDownloadUrl = data.download_url || data.preview_url;

    await loadPreviewVideo(data.preview_url);

    downloadLink.href = absoluteUrl(lastDownloadUrl);
    downloadLink.download = `${dramaTitle}_钩子.mp4`;
    downloadLink.classList.remove("hidden");

    showPostCaption(data.edit_plan);

    const mb = (data.size / 1024 / 1024).toFixed(1);
    let msg = `生成完成（约 ${mb} MB）。下方可预览；可点绿色按钮下载 MP4。`;
    if (
      data.edit_plan?.hook_summary &&
      !data.edit_plan.hook_summary.includes("未配置 LLM")
    ) {
      msg += ` AI：${data.edit_plan.hook_summary}`;
    }
    if (data.warning) {
      msg += ` 注意：${data.warning}`;
    }
    resultMsg.textContent = msg;
  } catch (err) {
    const msg = err.message || "生成失败";
    resultMsg.textContent =
      msg === "Failed to fetch"
        ? "网络连接中断（生成可能仍在后台进行）。请等 1–2 分钟后刷新页面重试，或先只选 1–2 集。"
        : msg;
  } finally {
    btnGenerate.disabled = false;
    btnGenerate.textContent = "重新生成钩子视频";
    updateGenerateState();
  }
});

loadEpisodes();
