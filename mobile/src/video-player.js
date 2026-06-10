/** 画质预设 + 对比度/饱和度/亮度滑杆 + 预览滤镜 */
const ENHANCE_KEY = "hongguo_video_enhance";
const ADJUST_KEY = "hongguo_video_adjust";

export const PRESETS = {
  off: { contrast: 100, saturate: 100, brightness: 100 },
  clear: { contrast: 106, saturate: 110, brightness: 104 },
  sharp: { contrast: 103, saturate: 105, brightness: 101 },
  cinema: { contrast: 110, saturate: 115, brightness: 98 },
};

export const ENHANCE_MODES = ["off", "clear", "sharp", "cinema"];

let adjustState = { preset: "off", ...PRESETS.off };

function loadAdjustState() {
  try {
    const raw = localStorage.getItem(ADJUST_KEY);
    if (raw) {
      const s = JSON.parse(raw);
      if (s && typeof s.contrast === "number") {
        return {
          preset: ENHANCE_MODES.includes(s.preset) ? s.preset : "custom",
          contrast: clamp(s.contrast, 50, 150),
          saturate: clamp(s.saturate, 50, 150),
          brightness: clamp(s.brightness, 50, 150),
        };
      }
    }
  } catch {
    /* ignore */
  }
  const mode = getEnhanceMode();
  return { preset: mode, ...PRESETS[mode] };
}

function saveAdjustState(state) {
  adjustState = { ...state };
  try {
    localStorage.setItem(ADJUST_KEY, JSON.stringify(adjustState));
    if (ENHANCE_MODES.includes(state.preset)) {
      localStorage.setItem(ENHANCE_KEY, state.preset);
    }
  } catch {
    /* ignore */
  }
}

function clamp(n, min, max) {
  return Math.min(max, Math.max(min, Number(n) || min));
}

export function getEnhanceMode() {
  return adjustState.preset === "custom" ? "custom" : adjustState.preset || "off";
}

export function getEnhanceSettings() {
  return {
    preset: adjustState.preset,
    contrast: adjustState.contrast,
    saturate: adjustState.saturate,
    brightness: adjustState.brightness,
    sharpen: adjustState.preset === "sharp",
    vignette: adjustState.preset === "cinema",
  };
}

export function needsEnhanceExport(settings = getEnhanceSettings()) {
  if (settings.sharpen || settings.vignette) return true;
  return (
    settings.contrast !== 100 ||
    settings.saturate !== 100 ||
    settings.brightness !== 100
  );
}

export function buildFilterCss({ contrast, saturate, brightness, sharpen }) {
  const parts = [];
  if (sharpen) parts.push("url(#video-sharpen-filter)");
  const c = contrast / 100;
  const s = saturate / 100;
  const b = brightness / 100;
  if (c !== 1 || s !== 1 || b !== 1) {
    parts.push(`contrast(${c}) saturate(${s}) brightness(${b})`);
  }
  return parts.length ? parts.join(" ") : "none";
}

function applyAdjustToPreview(stage, video, state) {
  if (video) video.style.filter = buildFilterCss({ ...state, sharpen: state.preset === "sharp" });
  stage?.classList.toggle("enhance-cinema", state.preset === "cinema");
}

function syncSliderUi(root, state) {
  const map = [
    ["adj-contrast", "adj-contrast-val", state.contrast],
    ["adj-saturate", "adj-saturate-val", state.saturate],
    ["adj-brightness", "adj-brightness-val", state.brightness],
  ];
  for (const [id, valId, v] of map) {
    const el = root?.querySelector(`#${id}`);
    const lab = root?.querySelector(`#${valId}`);
    if (el) el.value = String(v);
    if (lab) lab.textContent = String(v);
  }
}

export function bindVideoAdjust(stage, video) {
  const root = stage?.closest(".wm-result") || stage?.parentElement;
  if (!root || root.dataset.adjustBound) return;
  root.dataset.adjustBound = "1";

  const tabs = root.querySelectorAll(".enhance-tab");
  const resetBtn = root.querySelector("#adj-reset");
  const sliders = {
    contrast: root.querySelector("#adj-contrast"),
    saturate: root.querySelector("#adj-saturate"),
    brightness: root.querySelector("#adj-brightness"),
  };

  let state = loadAdjustState();
  saveAdjustState(state);

  const apply = () => {
    saveAdjustState(state);
    applyAdjustToPreview(stage, video, state);
    syncSliderUi(root, state);
    tabs.forEach((t) => {
      const m = t.dataset.enhance || "off";
      t.classList.toggle("active", state.preset !== "custom" && m === state.preset);
    });
  };

  const setPreset = (mode) => {
    const p = PRESETS[mode] || PRESETS.off;
    state = { preset: mode, ...p };
    apply();
  };

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => setPreset(tab.dataset.enhance || "off"));
  });

  for (const [key, el] of Object.entries(sliders)) {
    el?.addEventListener("input", () => {
      state = {
        ...state,
        preset: "custom",
        [key]: Number(el.value),
      };
      apply();
    });
  }

  resetBtn?.addEventListener("click", () => setPreset("off"));

  apply();
}

function applyPoster(stage, stageBg, posterLayer, posterUrl) {
  if (posterUrl) {
    if (posterLayer) {
      posterLayer.style.backgroundImage = `url(${posterUrl})`;
      posterLayer.classList.remove("hidden");
    }
    if (stageBg) {
      stageBg.style.backgroundImage = `url(${posterUrl})`;
      stageBg.classList.remove("hidden");
    }
  } else {
    posterLayer?.classList.add("hidden");
    stageBg?.classList.add("hidden");
  }
}

export function extractPosterDataUrl(videoSrc) {
  return new Promise((resolve) => {
    const v = document.createElement("video");
    v.muted = true;
    v.playsInline = true;
    v.setAttribute("playsinline", "");
    v.preload = "auto";
    v.src = videoSrc;
    let done = false;
    const finish = (url) => {
      if (done) return;
      done = true;
      v.src = "";
      resolve(url || "");
    };
    const timer = setTimeout(() => finish(""), 8000);
    v.onerror = () => {
      clearTimeout(timer);
      finish("");
    };
    v.onloadedmetadata = () => {
      const d = v.duration || 0;
      v.currentTime = d > 0.3 ? Math.min(0.2, d * 0.03) : 0;
    };
    v.onseeked = () => {
      try {
        const w = v.videoWidth;
        const h = v.videoHeight;
        if (!w || !h) {
          clearTimeout(timer);
          finish("");
          return;
        }
        const c = document.createElement("canvas");
        c.width = w;
        c.height = h;
        c.getContext("2d")?.drawImage(v, 0, 0, w, h);
        clearTimeout(timer);
        finish(c.toDataURL("image/jpeg", 0.82));
      } catch {
        clearTimeout(timer);
        finish("");
      }
    };
  });
}

export function setupVideoPreview({
  video,
  stage,
  stageBg,
  posterLayer,
  badge,
  fsBtn,
  coverUrl = "",
}) {
  if (!video) return;

  stage?.classList.add("is-loading");
  stage?.classList.remove("video-portrait", "video-landscape");
  badge?.classList.add("hidden");
  fsBtn?.classList.add("hidden");

  applyPoster(stage, stageBg, posterLayer, coverUrl);
  if (coverUrl) video.poster = coverUrl;
  else video.removeAttribute("poster");

  applyAdjustToPreview(stage, video, adjustState);

  const showVideo = () => {
    stage?.classList.remove("is-loading");
    posterLayer?.classList.add("hidden");
  };

  const onMeta = () => {
    const w = video.videoWidth || 0;
    const h = video.videoHeight || 0;
    if (w && h) {
      stage?.classList.add(h >= w ? "video-portrait" : "video-landscape");
      if (badge) {
        const hd = Math.max(w, h) >= 1080 ? " · HD" : Math.max(w, h) >= 720 ? " · 720p" : "";
        badge.textContent = `${w}×${h}${hd}`;
        badge.classList.remove("hidden");
      }
      fsBtn?.classList.remove("hidden");
    }
  };

  video.addEventListener("loadeddata", showVideo, { once: true });
  video.addEventListener("canplay", showVideo, { once: true });
  video.addEventListener("loadedmetadata", onMeta, { once: true });

  if (!coverUrl && stageBg) {
    video.addEventListener("loadeddata", () => captureBlurFrame(video, stageBg), { once: true });
  }

  if (fsBtn && !fsBtn.dataset.bound) {
    fsBtn.dataset.bound = "1";
    fsBtn.addEventListener("click", () => {
      if (typeof video.webkitEnterFullscreen === "function") {
        video.webkitEnterFullscreen();
      } else if (video.requestFullscreen) {
        video.requestFullscreen().catch(() => {});
      }
    });
  }
}

function captureBlurFrame(video, bgEl) {
  try {
    const c = document.createElement("canvas");
    c.width = 48;
    c.height = 48;
    const ctx = c.getContext("2d");
    if (!ctx || video.readyState < 2) return;
    ctx.drawImage(video, 0, 0, 48, 48);
    bgEl.style.backgroundImage = `url(${c.toDataURL("image/jpeg", 0.55)})`;
    bgEl.classList.remove("hidden");
  } catch {
    /* ignore */
  }
}

export function revealResult(resultEl) {
  if (!resultEl) return;
  resultEl.classList.remove("hidden", "wm-result-reveal");
  void resultEl.offsetWidth;
  resultEl.classList.add("wm-result-reveal");
  resultEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

export async function prepareVideoPreview({
  video,
  stage,
  stageBg,
  posterLayer,
  badge,
  fsBtn,
  videoSrc,
  coverUrl = "",
}) {
  let poster = coverUrl || "";
  if (!poster && videoSrc) poster = await extractPosterDataUrl(videoSrc);
  setupVideoPreview({ video, stage, stageBg, posterLayer, badge, fsBtn, coverUrl: poster });
  if (poster) video.poster = poster;
  video.preload = "auto";
  video.src = videoSrc;
  video.load();
}

/** @deprecated 使用 bindVideoAdjust */
export function bindVideoEnhance(stage) {
  bindVideoAdjust(stage, stage?.querySelector(".wm-preview"));
}
