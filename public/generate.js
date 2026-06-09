const params = new URLSearchParams(location.search);
const seriesId = params.get("series_id") || "";
const dramaTitle = params.get("title") || "短剧";
const dramaIntro = params.get("intro") || "";
const coverUrl = params.get("cover") || "";
const MAX_SELECT = 6;

const titleEl = document.getElementById("drama-title");
const metaEl = document.getElementById("drama-meta");
const panelTitleEl = document.getElementById("episode-panel-title");
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
const useManualEditEl = document.getElementById("use-manual-edit");
const manualEditPanel = document.getElementById("manual-edit-panel");
const manualEditSegmentsEl = document.getElementById("manual-edit-segments");
const manualEditStatusEl = document.getElementById("manual-edit-status");
const manualEditTotalEl = document.getElementById("manual-edit-total");
const btnReloadManualDraft = document.getElementById("btn-reload-manual-draft");
const useFqKocEl = document.getElementById("use-fq-koc");
const episodeUrlStatus = new Map();
const fqKocImportStatusEl = document.getElementById("fq-koc-import-status");
const configStatusEl = document.getElementById("config-status");

// 防止同一集被并发重复缓存（点击过快/多次触发）。
const cacheInProgress = new Set();

const episodeStatusEl = document.getElementById("episode-status");
const episodeGridEl = document.getElementById("episode-grid");
const btnGenerate = document.getElementById("btn-generate");
const btnInterruptEdit = document.getElementById("btn-interrupt-edit");
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
let manualEditPlan = null;
let manualDraftTimer = 0;
let manualDraftLoading = false;
let manualTimeline = null;
/** 已中断并载入时间轴，下一次生成携带 edit_plan */
let manualAwaitingContinue = false;
let activeGenerateJobId = "";
const MANUAL_EDIT_LS_KEY = "hongguo_manual_edit_state";

function saveManualEditState() {
  if (!seriesId || !manualAwaitingContinue || !manualEditPlan) return;
  try {
    localStorage.setItem(
      MANUAL_EDIT_LS_KEY,
      JSON.stringify({
        series_id: seriesId,
        episode_item_ids: Array.from(selected),
        manualEditPlan,
      })
    );
  } catch {
    /* ignore */
  }
}

function restoreManualEditState() {
  try {
    const raw = localStorage.getItem(MANUAL_EDIT_LS_KEY);
    if (!raw) return;
    const o = JSON.parse(raw);
    if (!o || o.series_id !== seriesId) return;
    if (!Array.isArray(o.episode_item_ids) || !o.manualEditPlan) return;
    const ids = o.episode_item_ids.filter((id) =>
      episodes.some((ep) => ep.item_id === id)
    );
    if (!ids.length) return;
    selected.clear();
    ids.forEach((id) => selected.add(id));
    renderEpisodes();
    manualEditPlan = o.manualEditPlan;
    manualAwaitingContinue = true;
    bindManualSegmentItemIds();
    renderManualEditor();
    btnGenerate.textContent = "继续生成成片";
    if (useManualEditEl) useManualEditEl.checked = true;
    syncEditModeCheckboxes(true);
    manualEditPanel?.classList.remove("hidden");
    setManualDraftStatus("已恢复上次中断的手动剪辑方案，可继续调整或点「继续生成成片」。");
    updateGenerateState();
  } catch {
    /* ignore */
  }
}

function clearManualEditState() {
  manualAwaitingContinue = false;
  try {
    localStorage.removeItem(MANUAL_EDIT_LS_KEY);
  } catch {
    /* ignore */
  }
}

function buildEditPlanPayload() {
  if (!manualEditPlan) return null;
  const segs = (manualEditPlan.body_segments || []).map((seg) => ({
    episode_index: seg.episode_index,
    label: seg.label,
    reason: seg.reason,
    item_id: seg.item_id,
    duration_sec: (seg.clips || []).reduce(
      (sum, c) => sum + (Number(c.duration_sec) || 0),
      0
    ),
    clips: (seg.clips || []).map((c) => ({
      trim_start_sec: Number(c.trim_start_sec) || 0,
      duration_sec: Number(c.duration_sec) || 0,
      reason: c.reason || "",
    })),
  }));
  return {
    hook_summary: manualEditPlan.hook_summary || "手動多段剪輯",
    opening_text: manualEditPlan.opening_text,
    opening_seconds: manualEditPlan.opening_seconds,
    outro_keyword: manualEditPlan.outro_keyword,
    outro_seconds: manualEditPlan.outro_seconds,
    post_caption: manualEditPlan.post_caption,
    edit_style: manualEditPlan.edit_style,
    body_segments: segs,
  };
}

titleEl.textContent = dramaTitle;
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
          ? "Playwright 已开启（AUTO_SYNC=1），生成时自动登录并拉片"
          : "Playwright 已开启：生成时将自动打开浏览器登录"
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
let splashPreviewTimer = 0;
function splashPreviewUrl() {
  const { splash_title_font, splash_subtitle_font } = getSplashFontSizes();
  const badge = getSplashBadge();
  const q = new URLSearchParams({
    title_font: String(splash_title_font),
    subtitle_font: String(splash_subtitle_font),
    _: String(Date.now()),
  });
  if (badge) q.set("badge", badge);
  return `/api/generate/splash-preview.png?${q}`;
}

function refreshSplashPreview() {
  if (!splashPreviewEl) return;
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

function isManualEditMode() {
  return Boolean(useManualEditEl?.checked);
}

function syncEditModeCheckboxes(fromManual = false) {
  if (!useManualEditEl || !useAiEditEl) return;
  if (fromManual && useManualEditEl.checked) {
    useAiEditEl.checked = false;
    useAiEditEl.disabled = true;
  } else if (!useManualEditEl.checked) {
    useAiEditEl.disabled = false;
  }
  if (useAiEditEl.checked) {
    useManualEditEl.checked = false;
    useManualEditEl.disabled = true;
    if (!manualAwaitingContinue) manualEditPanel?.classList.add("hidden");
  } else {
    useManualEditEl.disabled = false;
    if (useManualEditEl.checked && manualAwaitingContinue) {
      manualEditPanel?.classList.remove("hidden");
    } else if (!manualAwaitingContinue) {
      manualEditPanel?.classList.add("hidden");
    }
  }
}

function episodeTitlesMap() {
  const m = {};
  for (const ep of episodes) {
    if (selected.has(ep.item_id)) {
      m[ep.item_id] = ep.title || "";
    }
  }
  return m;
}

function updateManualEditTotal() {
  if (!manualEditTotalEl || !manualEditPlan?.body_segments) return;
  const parts = [];
  let total = 0;
  for (const seg of manualEditPlan.body_segments) {
    const clips = seg.clips || [];
    clips.forEach((c, i) => {
      const dur = Number(c.duration_sec) || 0;
      const start = Number(c.trim_start_sec) || 0;
      total += dur;
      parts.push(`片段${i + 1} ${dur.toFixed(1)}秒（${start.toFixed(1)}s起）`);
    });
  }
  const detail = parts.length ? `：${parts.join(" + ")}` : "";
  const target = Number(manualEditPlan?.target_body_sec) || 0;
  const hookTotal = Number(manualEditPlan?.target_hook_total_sec) || 0;
  const overhead = Number(manualEditPlan?.hook_overhead_sec) || 0;
  const hookRange = (manualEditPlan?.hook_range_text || "").trim();
  let targetHint = "";
  if (target > 0 && hookTotal > 0) {
    targetHint = ` / 目标正片约 ${target.toFixed(0)} 秒 + 片头片尾约 ${overhead.toFixed(0)} 秒 ≈ 成片 ${hookTotal.toFixed(0)} 秒`;
  } else if (target > 0) {
    targetHint = ` / 目标正片约 ${target.toFixed(0)} 秒${hookRange ? `（成片 ${hookRange}）` : ""}`;
  } else if (hookRange) {
    targetHint = `（成片 ${hookRange}）`;
  }
  const prefill =
    manualEditPlan?.prefill_source === "fallback"
      ? "（未分析到音画轴，当前为占位高光位，请先缓存正片后点「重新载入草稿」）"
      : "；两段为音画高能高光（打斗/音效等），可拖拽修改";
  manualEditTotalEl.textContent = `正片合计约 ${total.toFixed(1)} 秒${targetHint}${detail}${prefill}`;
}

async function cacheEpisodeForTimeline(itemId) {
  if (!seriesId || !itemId) return false;
  setManualDraftStatus("正在后台缓存本集（不弹浏览器）…");
  try {
    const res = await fetch("/api/material/fq-koc/cache-episode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        series_id: seriesId,
        item_id: itemId,
        drama_title: dramaTitle,
      }),
    });
    const { data } = await readJsonResponse(res);
    if (!res.ok || !data.ok) {
      const detail = formatApiErrorDetail(data.detail) || data.message || "缓存失败";
      setManualDraftStatus(detail);
      return false;
    }
    episodeUrlStatus.set(itemId, "local");
    renderEpisodes();
    setManualDraftStatus(
      data.size
        ? `缓存完成（${(data.size / 1024 / 1024).toFixed(1)} MB），正在加载预览…`
        : "缓存完成，正在加载预览…"
    );
    return true;
  } catch (e) {
    setManualDraftStatus(String(e.message || e));
    return false;
  }
}

function bindManualSegmentItemIds() {
  if (!manualEditPlan?.body_segments) return;
  const ids = Array.from(selected);
  manualEditPlan.body_segments.forEach((seg, si) => {
    const idx = (seg.episode_index || si + 1) - 1;
    seg.item_id = seg.item_id || ids[si] || ids[idx] || ids[0] || "";
  });
}

function renderManualEditor() {
  if (!manualEditSegmentsEl || !manualEditPlan?.body_segments?.length) {
    if (manualEditSegmentsEl) manualEditSegmentsEl.innerHTML = "";
    manualTimeline = null;
    return;
  }
  bindManualSegmentItemIds();

  if (typeof ManualTimelineEditor === "undefined") {
    manualEditSegmentsEl.innerHTML =
      '<p class="episode-status">时间轴组件加载失败，请硬刷新页面（Ctrl+Shift+R）</p>';
    return;
  }

  manualEditSegmentsEl.innerHTML = '<div id="manual-timeline-mount"></div>';
  const mount = document.getElementById("manual-timeline-mount");
  manualTimeline = new ManualTimelineEditor(mount, {
    seriesId,
    absoluteUrl,
    onChange: () => {
      updateManualEditTotal();
      saveManualEditState();
    },
    onCacheEpisode: cacheEpisodeForTimeline,
  });
  manualTimeline.setData({
    segments: manualEditPlan.body_segments,
    itemIds: manualEditPlan.body_segments.map((s) => s.item_id),
    durations: manualEditPlan.episode_durations || [],
  });
  manualTimeline.mount();
  updateManualEditTotal();
}

function collectManualEditPlanFromDom() {
  if (manualTimeline) manualTimeline.syncToPlan();
  if (!manualEditPlan?.body_segments) return manualEditPlan;
  for (const seg of manualEditPlan.body_segments) {
    seg.duration_sec = (seg.clips || []).reduce(
      (sum, c) => sum + (Number(c.duration_sec) || 0),
      0
    );
  }
  saveManualEditState();
  return manualEditPlan;
}

function validateManualEditPlan(plan) {
  const segs = plan?.body_segments || [];
  if (!segs.length) return "请至少为一集添加剪辑片段";
  for (const seg of segs) {
    if (!seg.clips?.length) {
      return `${seg.label || "某一集"} 至少需要 1 个片段`;
    }
    for (const c of seg.clips) {
      if ((c.duration_sec || 0) < 2) {
        return "每段时长至少 2 秒";
      }
      if ((c.trim_start_sec || 0) < 0) {
        return "入点不能为负数";
      }
    }
  }
  return "";
}

function formatApiErrorDetail(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((x) => x.msg || x.message || JSON.stringify(x)).join("；");
  }
  return String(detail);
}

function setManualDraftStatus(text) {
  if (manualEditStatusEl) manualEditStatusEl.textContent = text;
}

async function loadManualDraft(options = {}) {
  const force = Boolean(options.force);
  const prefill = options.prefill || "simple";

  if (!isManualEditMode()) {
    setManualDraftStatus("请先勾选「手动多段剪辑」");
    return false;
  }
  if (!seriesId) {
    setManualDraftStatus("缺少短剧 ID，请从检索页重新进入本页");
    return false;
  }
  if (selected.size === 0) {
    setManualDraftStatus("请先选择分集");
    return false;
  }
  if (manualDraftLoading && !force) {
    setManualDraftStatus("正在载入中，请稍候…");
    return false;
  }

  clearTimeout(manualDraftTimer);
  manualDraftLoading = true;
  if (btnReloadManualDraft) btnReloadManualDraft.disabled = true;
  setManualDraftStatus("正在载入剪辑草稿…");

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 120_000);

  try {
    const res = await fetch("/api/manual/edit-plan-draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        series_id: seriesId,
        drama_title: dramaTitle,
        episode_item_ids: Array.from(selected),
        episode_titles: episodeTitlesMap(),
        prefill,
      }),
    });
    const { data } = await readJsonResponse(res);
    if (!res.ok) {
      throw new Error(formatApiErrorDetail(data.detail) || "载入草稿失败");
    }
    manualEditPlan = data.edit_plan || null;
    if (!manualEditPlan?.body_segments?.length) {
      throw new Error("服务器返回的草稿为空，请确认已选分集并重试");
    }
    bindManualSegmentItemIds();
    renderManualEditor();
    setManualDraftStatus(
      manualEditPlan.hook_summary ||
        "已载入草稿：可修改各段入点/时长，或增删片段后生成。"
    );
    return true;
  } catch (e) {
    const msg =
      e.name === "AbortError"
        ? "载入超时（超过 2 分钟），请减少选集或稍后重试"
        : e.message === "Failed to fetch"
          ? "无法连接服务器：请确认后端已启动并已包含 /api/manual/edit-plan-draft 接口"
          : String(e.message || e);
    setManualDraftStatus(msg);
    return false;
  } finally {
    clearTimeout(timeoutId);
    manualDraftLoading = false;
    if (btnReloadManualDraft) btnReloadManualDraft.disabled = false;
    updateGenerateState();
  }
}

function scheduleManualDraftReload() {
  if (!isManualEditMode()) return;
  clearTimeout(manualDraftTimer);
  manualDraftTimer = setTimeout(loadManualDraft, 500);
}

function updateGenerateState() {
  const hasEpisodes = selected.size > 0;
  const manual = isManualEditMode();
  const ready =
    seriesId &&
    hasEpisodes &&
    (!manual ||
      (manualAwaitingContinue
        ? Boolean(manualEditPlan?.body_segments?.length)
        : true));
  btnGenerate.disabled = !ready;
  if (generateHint) {
    if (!hasEpisodes) {
      generateHint.textContent = "请选择至少 1 集";
    } else if (manual) {
      generateHint.textContent = manualAwaitingContinue
        ? "已在时间轴调整？点「继续生成成片」按方案输出。"
        : "手动模式：点「生成钩子视频」下载正片，完成后将自动进入时间轴。";
    } else {
      generateHint.textContent =
        "点「生成钩子视频」：自动登录达人中心后开始下载与成片（两段高光直剪，约 30 秒）。";
    }
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function escapeAttr(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
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
  if (window.HongguoApi?.apiUrl) return window.HongguoApi.apiUrl(path);
  return `${location.origin}${path.startsWith("/") ? "" : "/"}${path}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function showInterruptButton(show) {
  if (!btnInterruptEdit) return;
  btnInterruptEdit.classList.toggle("hidden", !show);
}

async function requestInterruptForEdit(jobId) {
  const res = await fetch(
    `/api/generate/job/${encodeURIComponent(jobId)}/interrupt-for-edit`,
    { method: "POST" }
  );
  const { data } = await readJsonResponse(res);
  if (!res.ok) {
    throw new Error(formatApiErrorDetail(data.detail) || "中断请求失败");
  }
  return data;
}

function applyAwaitingManualEditJob(data) {
  manualAwaitingContinue = true;
  manualEditPlan = data.edit_plan || null;
  activeGenerateJobId = "";
  showInterruptButton(false);
  syncEditModeCheckboxes(true);
  bindManualSegmentItemIds();
  renderManualEditor();
  saveManualEditState();
  if (manualEditStatusEl) {
    manualEditStatusEl.textContent =
      data.progress || "正片已在本地，请在时间轴调整各段后点「继续生成成片」。";
  }
  btnGenerate.disabled = false;
  btnGenerate.textContent = "继续生成成片";
  resultMsg.textContent =
    data.progress || "已进入时间轴。请调整各段后点「继续生成成片」。";
  updateGenerateState();
}

async function pollGenerateJob(jobId, { manualInterruptFlow = false } = {}) {
  activeGenerateJobId = jobId;
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
    if (manualInterruptFlow && data.status === "running") {
      showInterruptButton(data.phase === "starting");
    }
    if (data.status === "awaiting_manual_edit") {
      applyAwaitingManualEditJob(data);
      return data;
    }
    if (data.status === "completed") {
      activeGenerateJobId = "";
      showInterruptButton(false);
      return data;
    }
    if (data.status === "failed") {
      activeGenerateJobId = "";
      showInterruptButton(false);
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
      generateHint.textContent = isManualEditMode()
        ? `已选 ${selected.size} 集：请在下方编辑各段剪辑点`
        : selected.size === 1
          ? "已选 1 集：取 2 段最高光直剪"
          : `已选 ${selected.size} 集：每集 2 段高光，拼成约 30 秒`;
      if (selected.size === 1) {
        loadServiceConfig(id);
      }
      updateGenerateState();
    });
  });
}

btnInterruptEdit?.addEventListener("click", async () => {
  const jobId = activeGenerateJobId;
  if (!jobId || btnInterruptEdit.disabled) return;
  btnInterruptEdit.disabled = true;
  const waitStarted = Date.now();
  try {
    resultMsg.textContent = "正在发送中断请求…";
    const ack = await requestInterruptForEdit(jobId);
    resultMsg.textContent =
      ack?.progress ||
      (ack?.phase === "materials_ready"
        ? "正在载入时间轴草稿，请稍候…"
        : "已请求中断：正片下载/分析完成后会自动进入时间轴，请勿关闭页面。");
    while (Date.now() - waitStarted < 600_000) {
      const res = await fetch(`/api/generate/job/${encodeURIComponent(jobId)}`);
      const { data } = await readJsonResponse(res);
      if (data.status === "awaiting_manual_edit") {
        applyAwaitingManualEditJob(data);
        break;
      }
      if (data.status === "failed") {
        throw new Error(data.error || "中断失败");
      }
      if (data.status === "completed") {
        resultMsg.textContent =
          "任务已直接完成（未能中断，可能点击过晚）。请重新勾选「手动多段剪辑」再试。";
        showInterruptButton(false);
        break;
      }
      if (data.progress) resultMsg.textContent = data.progress;
      await sleep(1200);
    }
    if (Date.now() - waitStarted >= 600_000) {
      throw new Error("等待中断超时（10 分钟），请刷新页面查看任务状态");
    }
  } catch (e) {
    resultMsg.textContent = String(e.message || e);
  } finally {
    btnInterruptEdit.disabled = false;
  }
});

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

async function refreshLocalEpisodeStatus(episodeList) {
  if (!seriesId || !episodeList?.length) return;
  const ids = episodeList.map((ep) => ep.item_id).filter(Boolean);
  if (!ids.length) return;
  try {
    const res = await fetch(
      `/api/material/fq-koc/local-status?series_id=${encodeURIComponent(seriesId)}&item_ids=${encodeURIComponent(ids.join(","))}`
    );
    const data = await res.json();
    if (!res.ok || !data.ok || !data.episodes) return;
    for (const [itemId, cached] of Object.entries(data.episodes)) {
      if (cached) episodeUrlStatus.set(itemId, "local");
    }
    renderEpisodes();
    if (data.cached_count > 0 && fqKocImportStatusEl) {
      fqKocImportStatusEl.textContent = `已有 ${data.cached_count}/${data.total} 集本地缓存，生成时将跳过重复下载`;
    }
  } catch {
    /* ignore */
  }
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
    await refreshLocalEpisodeStatus(episodes);
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
  manualEditPlan = null;
  clearManualEditState();
  renderManualEditor();
  renderEpisodes();
  generateHint.textContent = "请选择至少 1 集";
  updateGenerateState();
});

useManualEditEl?.addEventListener("change", () => {
  clearManualEditState();
  manualEditPlan = null;
  syncEditModeCheckboxes(true);
  if (!isManualEditMode()) {
    renderManualEditor();
    showInterruptButton(false);
  }
  updateGenerateState();
});

useAiEditEl?.addEventListener("change", () => {
  syncEditModeCheckboxes(false);
  updateGenerateState();
});

function onReloadManualDraftClick(e) {
  e.preventDefault();
  e.stopPropagation();
  loadManualDraft({ force: true, prefill: "simple" });
}

if (btnReloadManualDraft) {
  btnReloadManualDraft.addEventListener("click", onReloadManualDraftClick);
} else {
  console.warn("未找到 #btn-reload-manual-draft，请硬刷新页面（Ctrl+Shift+R）");
}

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

  btnGenerate.disabled = true;
  btnGenerate.textContent = "正在生成，请稍候…";
  resultPanel.classList.remove("hidden");
  postCaptionBox?.classList.add("hidden");
  const useFqKoc = useFqKocEl ? useFqKocEl.checked !== false : true;
  resultMsg.textContent = useFqKoc
    ? "已提交：将先确认达人中心登录（可能弹出浏览器），再下载素材并生成成片。若提示手动下载，请在达人页点目标集「下载」一次。"
    : "正在生成，请稍候…";
  previewEl.classList.add("hidden");
  previewEl.removeAttribute("src");
  downloadLink.classList.add("hidden");
  lastPreviewUrl = "";
  lastDownloadUrl = "";

  const manual = isManualEditMode();
  let editPlanPayload = null;
  let pauseForManualEdit = false;
  if (manual) {
    if (manualAwaitingContinue) {
      collectManualEditPlanFromDom();
      const err = validateManualEditPlan(manualEditPlan);
      if (err) {
        resultMsg.textContent = err;
        btnGenerate.disabled = false;
        btnGenerate.textContent = "继续生成成片";
        updateGenerateState();
        return;
      }
      editPlanPayload = buildEditPlanPayload();
      if (!editPlanPayload?.body_segments?.length) {
        resultMsg.textContent = "手动剪辑方案为空，请先在时间轴调整片段";
        btnGenerate.disabled = false;
        btnGenerate.textContent = "继续生成成片";
        updateGenerateState();
        return;
      }
    } else {
      pauseForManualEdit = true;
      manualEditPlan = null;
      renderManualEditor();
    }
  }

  try {
    const res = await fetch("/api/generate/hook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        series_id: seriesId,
        drama_title: dramaTitle,
        cover_url: coverUrl,
        ...getSplashFontSizes(),
        episode_item_ids: Array.from(selected),
        use_ai_edit: manual ? false : Boolean(useAiEditEl?.checked),
        edit_plan: editPlanPayload,
        manual_edit: Boolean(editPlanPayload),
        pause_for_manual_edit: pauseForManualEdit,
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

    const data = await pollGenerateJob(startData.job_id, {
      manualInterruptFlow: pauseForManualEdit,
    });

    if (data.status === "awaiting_manual_edit") {
      return;
    }

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
    clearManualEditState();
    btnGenerate.textContent = "重新生成钩子视频";
  } catch (err) {
    const msg = err.message || "生成失败";
    resultMsg.textContent =
      msg === "Failed to fetch"
        ? "网络连接中断（生成可能仍在后台进行）。请等 1–2 分钟后刷新页面重试，或先只选 1–2 集。"
        : msg;
  } finally {
    btnGenerate.disabled = false;
    if (!manualAwaitingContinue) {
      btnGenerate.textContent = "重新生成钩子视频";
    }
    showInterruptButton(false);
    updateGenerateState();
  }
});

syncEditModeCheckboxes();

async function loadEpisodesAndRestoreManual() {
  await loadEpisodes();
  if (isManualEditMode()) {
    restoreManualEditState();
  }
}

loadEpisodesAndRestoreManual();
