/**
 * 剪映式时间轴：预览 + 轨道拖拽裁切（入点/时长）。
 * 依赖页面提供 absoluteUrl、seriesId。
 */
(function initManualTimeline(global) {
  const MIN_CLIP_SEC = 2;
  const COLORS = ["#ff4d4f", "#ff9f43", "#54a0ff", "#5f27cd", "#10ac84", "#ee5a24"];

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  function round1(v) {
    return Math.round(v * 10) / 10;
  }

  function formatTime(sec) {
    const s = Math.max(0, sec || 0);
    const m = Math.floor(s / 60);
    const r = Math.floor(s % 60);
    const ms = Math.floor((s % 1) * 10);
    return `${m}:${String(r).padStart(2, "0")}.${ms}`;
  }

  function formatTimeShort(sec) {
    const s = Math.max(0, sec || 0);
    if (s < 60) return `${s.toFixed(1)}s`;
    const m = Math.floor(s / 60);
    const r = Math.round((s % 60) * 10) / 10;
    return `${m}:${String(Math.floor(r)).padStart(2, "0")}`;
  }

  function clipMeta(ci, c) {
    const start = round1(c.trim_start_sec || 0);
    const dur = round1(c.duration_sec || MIN_CLIP_SEC);
    const end = round1(start + dur);
    return {
      index: ci + 1,
      start,
      dur,
      end,
      label: c.reason || `高光${ci + 1}`,
    };
  }

  class ManualTimelineEditor {
    constructor(rootEl, options = {}) {
      this.root = rootEl;
      this.seriesId = options.seriesId || "";
      this.absoluteUrl = options.absoluteUrl || ((p) => p);
      this.onChange = options.onChange || (() => {});
      this.onCacheEpisode = options.onCacheEpisode || null;
      this.segments = [];
      this.itemIds = [];
      this.durations = [];
      this.activeIndex = 0;
      this.sourceDuration = 120;
      this.pxPerSec = 10;
      this.selectedClipIndex = 0;
      this._drag = null;
      this._videoUrl = "";
      this._previewAll = null;
      this._previewAllOnTime = null;
    }

    setData({ segments, itemIds, durations }) {
      this.segments = segments || [];
      this.itemIds = itemIds || [];
      this.durations = durations || [];
    }

    mount() {
      if (!this.root) return;
      this.root.innerHTML = `
        <div class="mt-editor">
          <div class="mt-tabs" id="mt-tabs"></div>
          <div class="mt-video-wrap">
            <video id="mt-video" class="mt-video" playsinline preload="metadata"></video>
            <p id="mt-video-hint" class="mt-video-hint hidden"></p>
          </div>
          <div class="mt-toolbar">
            <button type="button" class="btn-ghost" id="mt-btn-play">播放</button>
            <button type="button" class="btn-ghost" id="mt-btn-preview-clip">预览选中片段</button>
            <button type="button" class="btn-ghost" id="mt-btn-preview-all">预览全部片段</button>
            <button type="button" class="btn-ghost" id="mt-btn-split">在播放头切开</button>
            <button type="button" class="btn-ghost" id="mt-btn-add">+ 增加片段</button>
            <button type="button" class="btn-ghost" id="mt-btn-del">删除选中</button>
            <button type="button" class="btn-ghost" id="mt-btn-cache">后台缓存本集</button>
            <span id="mt-playhead-label" class="mt-playhead-label">0:00.0</span>
          </div>
          <div class="mt-clip-list" id="mt-clip-list" aria-label="片段时长列表"></div>
          <div class="mt-track-scroll">
            <div class="mt-timeline-inner" id="mt-timeline-inner">
              <div class="mt-ruler" id="mt-ruler"></div>
              <div class="mt-track" id="mt-track">
                <div class="mt-track-bg" id="mt-track-bg"></div>
                <div class="mt-clips" id="mt-clips"></div>
                <div class="mt-playhead" id="mt-playhead"></div>
              </div>
            </div>
          </div>
          <p class="mt-help">拖动色块调整入点；拖左右边缘改时长；点击轨道定位播放头。需本地已缓存正片 MP4。</p>
        </div>`;

      this.tabsEl = this.root.querySelector("#mt-tabs");
      this.video = this.root.querySelector("#mt-video");
      this.videoHint = this.root.querySelector("#mt-video-hint");
      this.trackScroll = this.root.querySelector(".mt-track-scroll");
      this.timelineInner = this.root.querySelector("#mt-timeline-inner");
      this.track = this.root.querySelector("#mt-track");
      this.trackBg = this.root.querySelector("#mt-track-bg");
      this.clipsEl = this.root.querySelector("#mt-clips");
      this.clipListEl = this.root.querySelector("#mt-clip-list");
      this.playheadEl = this.root.querySelector("#mt-playhead");
      this.rulerEl = this.root.querySelector("#mt-ruler");
      this.playheadLabel = this.root.querySelector("#mt-playhead-label");

      this.root.querySelector("#mt-btn-play")?.addEventListener("click", () => this.togglePlay());
      this.root.querySelector("#mt-btn-preview-clip")?.addEventListener("click", () => {
        this.stopPreviewAll();
        this.previewSelectedClip();
      });
      this.root.querySelector("#mt-btn-preview-all")?.addEventListener("click", () =>
        this.togglePreviewAllClips()
      );
      this.root.querySelector("#mt-btn-split")?.addEventListener("click", () => this.splitAtPlayhead());
      this.root.querySelector("#mt-btn-add")?.addEventListener("click", () => this.addClipAtPlayhead());
      this.root.querySelector("#mt-btn-del")?.addEventListener("click", () => this.deleteSelectedClip());
      this.root.querySelector("#mt-btn-cache")?.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        void this.cacheCurrentEpisode();
      });

      this.video?.addEventListener("timeupdate", () => this._onVideoTime());
      this.video?.addEventListener("error", () => this._onVideoError());
      this.video?.addEventListener("loadedmetadata", () => {
        if (this.video.duration > 1) {
          this.sourceDuration = this.video.duration;
          this._renderTrack();
        }
      });

      this.track?.addEventListener("mousedown", (e) => this._onTrackMouseDown(e));
      window.addEventListener("mousemove", (e) => this._onWindowMouseMove(e));
      window.addEventListener("mouseup", () => this._onWindowMouseUp());

      this._renderTabs();
      this.loadSegment(0);
    }

    syncToPlan() {
      this._renderClipList();
      this.onChange();
    }

    _seg() {
      return this.segments[this.activeIndex];
    }

    _clips() {
      const seg = this._seg();
      if (!seg) return [];
      if (!seg.clips?.length) {
        seg.clips = [{ trim_start_sec: 8, duration_sec: 12, reason: "高光1" }];
      }
      return seg.clips;
    }

    _renderTabs() {
      if (!this.tabsEl) return;
      if (this.segments.length <= 1) {
        this.tabsEl.innerHTML = "";
        return;
      }
      this.tabsEl.innerHTML = this.segments
        .map((seg, i) => {
          const label = seg.label || `第${seg.episode_index || i + 1}集`;
          return `<button type="button" class="mt-tab${i === this.activeIndex ? " active" : ""}" data-i="${i}">${label}</button>`;
        })
        .join("");
      this.tabsEl.querySelectorAll(".mt-tab").forEach((btn) => {
        btn.addEventListener("click", () => {
          const i = Number(btn.dataset.i);
          if (i !== this.activeIndex) this.loadSegment(i);
        });
      });
    }

    async resolveVideoUrl(itemId) {
      if (!this.seriesId || !itemId) return { url: "", message: "缺少剧集 ID" };
      try {
        const res = await fetch(
          `/api/material/fq-koc/playback?series_id=${encodeURIComponent(this.seriesId)}&item_id=${encodeURIComponent(itemId)}`
        );
        const data = await res.json();
        if (!data.ok || !data.playback_url) {
          return {
            url: "",
            message: data.message || "未找到可播放素材，请点「缓存本集到本地」",
          };
        }
        const raw = data.playback_url;
        const url = raw.startsWith("http") ? raw : this.absoluteUrl(raw);
        return {
          url,
          proxied: Boolean(data.proxied),
          source: data.source || "",
          message: "",
        };
      } catch (e) {
        return { url: "", message: String(e.message || e) };
      }
    }

    _onVideoError() {
      if (!this.videoHint) return;
      const code = this.video?.error?.code;
      const codeHint =
        code === 4
          ? "（格式不支持或文件损坏）"
          : code === 2
            ? "（网络中断）"
            : code === 3
              ? "（解码失败，请重新下载完整 MP4）"
              : "";
      this.videoHint.textContent =
        `视频无法播放${codeHint}。请点「缓存本集到本地」后重试。`;
      this.videoHint.classList.remove("hidden");
      this.videoHint.hidden = false;
    }

    async cacheCurrentEpisode() {
      const itemId = this.itemIds[this.activeIndex];
      if (!itemId) return;
      if (this.onCacheEpisode) {
        if (this.videoHint) {
          this.videoHint.textContent = "正在缓存本集到本地，请稍候…";
          this.videoHint.classList.remove("hidden");
          this.videoHint.hidden = false;
        }
        const ok = await this.onCacheEpisode(itemId);
        if (ok) await this.loadSegment(this.activeIndex);
        return;
      }
      if (this.videoHint) {
        this.videoHint.textContent = "请先在生成页配置缓存逻辑";
        this.videoHint.classList.remove("hidden");
        this.videoHint.hidden = false;
      }
    }

    async loadSegment(index) {
      this.stopPreviewAll();
      this.activeIndex = clamp(index, 0, Math.max(0, this.segments.length - 1));
      this.selectedClipIndex = 0;
      this._renderTabs();

      const itemId = this.itemIds[this.activeIndex];
      const seg = this._seg();
      this.sourceDuration = this.durations[this.activeIndex] || 120;

      if (this.videoHint) {
        this.videoHint.classList.add("hidden");
        this.videoHint.hidden = true;
        this.videoHint.textContent = "";
      }

      const resolved = await this.resolveVideoUrl(itemId);
      const url = resolved.url;
      this._videoUrl = url;
      if (!url) {
        if (this.video) this.video.removeAttribute("src");
        if (this.videoHint) {
          this.videoHint.textContent =
            resolved.message ||
            "未找到可播放的正片：请点「缓存本集到本地」。";
          this.videoHint.classList.remove("hidden");
          this.videoHint.hidden = false;
        }
        this._renderTrack();
        return;
      }

      if (this.video) {
        this.video.src = url;
        this.video.load();
        this.video.onloadeddata = () => {
          if (this.videoHint) {
            this.videoHint.classList.add("hidden");
            this.videoHint.hidden = true;
          }
        };
      }
      this._renderTrack();
      if (seg && this._clips().length) {
        this.seekTo(this._clips()[0].trim_start_sec || 0);
      }
    }

    _timelineDuration() {
      const clips = this._clips();
      let end = 0;
      for (const c of clips) {
        end = Math.max(end, (c.trim_start_sec || 0) + (c.duration_sec || 0));
      }
      if (end < 1) {
        return Math.min(this.sourceDuration, 60);
      }
      const pad = Math.max(6, end * 0.06);
      return clamp(end + pad, 20, this.sourceDuration);
    }

    _trackWidth() {
      const dur = this._timelineDuration();
      const w = dur * this.pxPerSec;
      return Math.max(320, Math.min(1400, w));
    }

    _applyTimelineWidth(w) {
      const px = `${w}px`;
      if (this.timelineInner) this.timelineInner.style.width = px;
      if (this.track) this.track.style.width = px;
      if (this.trackBg) this.trackBg.style.width = px;
      if (this.rulerEl) this.rulerEl.style.width = px;
    }

    _secFromClientX(clientX) {
      const rect = this.track.getBoundingClientRect();
      const x = clamp(clientX - rect.left, 0, rect.width);
      const dur = this._timelineDuration();
      return clamp(x / this.pxPerSec, 0, dur);
    }

    _updatePlayheadUI(sec) {
      const left = sec * this.pxPerSec;
      if (this.playheadEl) this.playheadEl.style.left = `${left}px`;
      if (this.playheadLabel) this.playheadLabel.textContent = formatTime(sec);
    }

    seekTo(sec) {
      const t = clamp(sec, 0, this.sourceDuration);
      if (this.video && this._videoUrl) {
        try {
          this.video.currentTime = t;
        } catch {
          /* ignore */
        }
      }
      this._updatePlayheadUI(t);
    }

    _onVideoTime() {
      if (!this.video) return;
      this._updatePlayheadUI(this.video.currentTime || 0);
    }

    togglePlay() {
      if (!this.video || !this._videoUrl) return;
      if (this._previewAll) {
        this.stopPreviewAll();
        return;
      }
      if (this.video.paused) this.video.play();
      else this.video.pause();
    }

    _previewAllBtn() {
      return this.root?.querySelector("#mt-btn-preview-all");
    }

    stopPreviewAll() {
      if (this._previewAllOnTime && this.video) {
        this.video.removeEventListener("timeupdate", this._previewAllOnTime);
      }
      this._previewAllOnTime = null;
      if (this._previewAll) {
        this._previewAll.cancelled = true;
        this._previewAll = null;
      }
      const btn = this._previewAllBtn();
      if (btn) {
        btn.textContent = "预览全部片段";
        btn.classList.remove("mt-btn-active");
      }
      this.video?.pause();
    }

    _finishPreviewAllUi() {
      this.stopPreviewAll();
      this._onVideoTime();
    }

    togglePreviewAllClips() {
      if (this._previewAll) {
        this.stopPreviewAll();
        return;
      }
      this.previewAllClips();
    }

    previewAllClips() {
      const clips = this._clips();
      if (!clips.length || !this.video || !this._videoUrl) return;

      this.stopPreviewAll();
      const totalDur = clips.reduce((s, c) => s + (c.duration_sec || 0), 0);
      this._previewAll = { index: 0, cancelled: false };
      const btn = this._previewAllBtn();
      if (btn) {
        btn.textContent = "停止预览";
        btn.classList.add("mt-btn-active");
      }

      const playCurrent = () => {
        if (!this._previewAll || this._previewAll.cancelled) {
          this._finishPreviewAllUi();
          return;
        }
        const i = this._previewAll.index;
        if (i >= clips.length) {
          if (this.playheadLabel) {
            this.playheadLabel.textContent = `已播完 ${clips.length} 段（共 ${totalDur.toFixed(1)}s）`;
          }
          this._finishPreviewAllUi();
          return;
        }

        const c = clips[i];
        const m = clipMeta(i, c);
        this.selectedClipIndex = i;
        this._renderTrack();

        const start = m.start;
        const end = m.end;
        if (this.playheadLabel) {
          this.playheadLabel.textContent = `预览 ${i + 1}/${clips.length} · 本段 ${m.dur.toFixed(1)}s`;
        }

        this.seekTo(start);
        this.video.play();

        if (this._previewAllOnTime) {
          this.video.removeEventListener("timeupdate", this._previewAllOnTime);
        }
        this._previewAllOnTime = () => {
          if (!this._previewAll || this._previewAll.cancelled) return;
          if (this.video.currentTime >= end - 0.05) {
            this.video.removeEventListener("timeupdate", this._previewAllOnTime);
            this._previewAllOnTime = null;
            this._previewAll.index += 1;
            playCurrent();
          }
        };
        this.video.addEventListener("timeupdate", this._previewAllOnTime);
      };

      playCurrent();
    }

    previewSelectedClip() {
      const clips = this._clips();
      const c = clips[this.selectedClipIndex];
      if (!c) return;
      this.seekTo(c.trim_start_sec || 0);
      if (!this.video) return;
      this.video.play();
      const end = (c.trim_start_sec || 0) + (c.duration_sec || 0);
      const onTime = () => {
        if (this.video.currentTime >= end - 0.05) {
          this.video.pause();
          this.video.removeEventListener("timeupdate", onTime);
        }
      };
      this.video.addEventListener("timeupdate", onTime);
    }

    splitAtPlayhead() {
      const t = this.video?.currentTime ?? 0;
      const clips = this._clips();
      const idx = clips.findIndex(
        (c) => t > (c.trim_start_sec || 0) + 0.3 && t < (c.trim_start_sec || 0) + (c.duration_sec || 0) - 0.3
      );
      if (idx < 0) return;
      const c = clips[idx];
      const start = c.trim_start_sec || 0;
      const end = start + (c.duration_sec || 0);
      const leftDur = t - start;
      const rightDur = end - t;
      if (leftDur < MIN_CLIP_SEC || rightDur < MIN_CLIP_SEC) return;
      c.duration_sec = round1(leftDur);
      clips.splice(idx + 1, 0, {
        trim_start_sec: round1(t),
        duration_sec: round1(rightDur),
        reason: `高光${clips.length + 1}`,
      });
      this._renderTrack();
      this.syncToPlan();
    }

    addClipAtPlayhead() {
      const clips = this._clips();
      const t = round1(this.video?.currentTime || 0);
      const dur = round1(clamp(10, MIN_CLIP_SEC, this.sourceDuration - t));
      if (dur < MIN_CLIP_SEC) return;
      clips.push({
        trim_start_sec: t,
        duration_sec: dur,
        reason: `高光${clips.length + 1}`,
      });
      this.selectedClipIndex = clips.length - 1;
      this._renderTrack();
      this.syncToPlan();
    }

    deleteSelectedClip() {
      const clips = this._clips();
      if (clips.length <= 1) return;
      clips.splice(this.selectedClipIndex, 1);
      this.selectedClipIndex = clamp(this.selectedClipIndex, 0, clips.length - 1);
      this._renderTrack();
      this.syncToPlan();
    }

    _renderClipList() {
      if (!this.clipListEl) return;
      const clips = this._clips();
      if (!clips.length) {
        this.clipListEl.innerHTML = "";
        return;
      }
      const total = clips.reduce((s, c) => s + (c.duration_sec || 0), 0);
      this.clipListEl.innerHTML = `
        <div class="mt-clip-list-head">
          <span>本集共 ${clips.length} 段</span>
          <span class="mt-clip-list-total">合计 <strong>${total.toFixed(1)}</strong> 秒</span>
        </div>
        <div class="mt-clip-list-items">
          ${clips
            .map((c, ci) => {
              const m = clipMeta(ci, c);
              const sel = ci === this.selectedClipIndex ? " selected" : "";
              const color = COLORS[ci % COLORS.length];
              return `
              <button type="button" class="mt-clip-card${sel}" data-ci="${ci}" style="--clip-color:${color}">
                <span class="mt-clip-card-title">${escapeHtml(m.label)}</span>
                <span class="mt-clip-card-dur">时长 <strong>${m.dur.toFixed(1)}</strong> 秒</span>
                <span class="mt-clip-card-range">入点 ${formatTimeShort(m.start)} → 结束 ${formatTimeShort(m.end)}</span>
                <span class="mt-clip-card-src">源片 ${m.start.toFixed(1)}s – ${m.end.toFixed(1)}s</span>
              </button>`;
            })
            .join("")}
        </div>`;

      this.clipListEl.querySelectorAll(".mt-clip-card").forEach((btn) => {
        btn.addEventListener("click", () => {
          const ci = Number(btn.dataset.ci);
          this.selectedClipIndex = ci;
          this._renderTrack();
          const c = this._clips()[ci];
          if (c) this.seekTo(c.trim_start_sec || 0);
        });
      });
    }

    _renderRuler() {
      if (!this.rulerEl) return;
      const dur = this._timelineDuration();
      const step = dur > 180 ? 30 : dur > 90 ? 15 : dur > 45 ? 10 : 5;
      let html = "";
      for (let t = 0; t <= dur + 0.01; t += step) {
        const left = t * this.pxPerSec;
        html += `<span class="mt-ruler-tick" style="left:${left}px">${formatTime(t)}</span>`;
      }
      this.rulerEl.innerHTML = html;
    }

    _renderTrack() {
      const w = this._trackWidth();
      this._applyTimelineWidth(w);
      this._renderRuler();

      const clips = this._clips();
      this._renderClipList();
      if (!this.clipsEl) return;
      this.clipsEl.innerHTML = clips
        .map((c, ci) => {
          const m = clipMeta(ci, c);
          const left = m.start * this.pxPerSec;
          const width = Math.max(MIN_CLIP_SEC * this.pxPerSec, m.dur * this.pxPerSec);
          const color = COLORS[ci % COLORS.length];
          const sel = ci === this.selectedClipIndex ? " selected" : "";
          const wide = width >= 72;
          return `
          <div class="mt-clip${sel}" data-ci="${ci}" style="left:${left}px;width:${width}px;background:${color}"
            title="${escapeHtml(m.label)}：入点${m.start.toFixed(1)}s，时长${m.dur.toFixed(1)}s，至${m.end.toFixed(1)}s">
            <span class="mt-clip-handle mt-clip-handle-l" data-handle="l"></span>
            <span class="mt-clip-body" data-handle="move">
              <span class="mt-clip-idx">${escapeHtml(m.label)}</span>
              <span class="mt-clip-dur">${m.dur.toFixed(1)}秒</span>
              ${wide ? `<span class="mt-clip-range">${m.start.toFixed(1)}–${m.end.toFixed(1)}s</span>` : ""}
            </span>
            <span class="mt-clip-handle mt-clip-handle-r" data-handle="r"></span>
          </div>`;
        })
        .join("");

      this.clipsEl.querySelectorAll(".mt-clip").forEach((el) => {
        const ci = Number(el.dataset.ci);
        el.addEventListener("mousedown", (e) => {
          e.stopPropagation();
          this.selectedClipIndex = ci;
          this._renderTrack();
          const handle = e.target.closest("[data-handle]")?.dataset.handle || "move";
          this._startDrag(e, ci, handle);
        });
      });

      const t = this.video?.currentTime || 0;
      this._updatePlayheadUI(t);
    }

    _startDrag(e, clipIndex, mode) {
      this.stopPreviewAll();
      const clips = this._clips();
      const c = clips[clipIndex];
      if (!c) return;
      this._drag = {
        mode,
        clipIndex,
        startX: e.clientX,
        origStart: c.trim_start_sec || 0,
        origDur: c.duration_sec || MIN_CLIP_SEC,
      };
      e.preventDefault();
    }

    _onTrackMouseDown(e) {
      if (e.target.closest(".mt-clip")) return;
      const sec = this._secFromClientX(e.clientX);
      this.seekTo(sec);
      this._drag = { mode: "playhead", startX: e.clientX, sec };
    }

    _onWindowMouseMove(e) {
      if (!this._drag) return;
      const clips = this._clips();
      const c = clips[this._drag.clipIndex];
      const dx = e.clientX - this._drag.startX;
      const ds = dx / this.pxPerSec;

      if (this._drag.mode === "playhead") {
        const sec = this._secFromClientX(e.clientX);
        this.seekTo(sec);
        return;
      }

      if (!c) return;
      const maxEnd = this.sourceDuration;

      if (this._drag.mode === "move") {
        let ns = this._drag.origStart + ds;
        ns = clamp(ns, 0, maxEnd - MIN_CLIP_SEC);
        if (ns + c.duration_sec > maxEnd) {
          c.duration_sec = round1(maxEnd - ns);
        }
        c.trim_start_sec = round1(ns);
      } else if (this._drag.mode === "l") {
        let ns = this._drag.origStart + ds;
        const end = this._drag.origStart + this._drag.origDur;
        ns = clamp(ns, 0, end - MIN_CLIP_SEC);
        c.trim_start_sec = round1(ns);
        c.duration_sec = round1(end - ns);
      } else if (this._drag.mode === "r") {
        let nd = this._drag.origDur + ds;
        nd = clamp(nd, MIN_CLIP_SEC, maxEnd - (c.trim_start_sec || 0));
        c.duration_sec = round1(nd);
      }

      this._renderTrack();
    }

    _onWindowMouseUp() {
      if (!this._drag) return;
      this._drag = null;
      this.syncToPlan();
    }
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  global.ManualTimelineEditor = ManualTimelineEditor;
})(window);
