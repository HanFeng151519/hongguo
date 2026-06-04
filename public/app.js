const form = document.getElementById("search-form");
const input = document.getElementById("search-input");
const btn = document.getElementById("search-btn");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
let debounceTimer = null;
let currentQuery = "";
let lastSearchItems = [];
let hintAudio = null;

function searchHintAudioUrl(title) {
  return `/api/tts/search-hint.mp3?title=${encodeURIComponent((title || "").trim())}`;
}

function playSearchHint(title) {
  const t = (title || "").trim();
  if (!t) return;
  if (hintAudio) {
    hintAudio.pause();
    hintAudio = null;
  }
  hintAudio = new Audio(searchHintAudioUrl(t));
  hintAudio.play().catch(() => {});
}

function formatCopyText(item) {
  const title = item.title || "";
  const id = item.id || "";
  const intro = item.intro || "暂无简介";
  return `剧名：${title}\nID：${id}\n简介：${intro}`;
}

function textareaContent(text) {
  return String(text).replace(/<\/textarea/gi, "&lt;/textarea");
}

async function copyToClipboard(text) {
  const selection = window.getSelection();
  if (selection) {
    selection.removeAllRanges();
  }

  if (navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* 降级到 execCommand */
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.cssText =
    "position:fixed;top:0;left:0;width:2em;height:2em;padding:0;border:none;outline:none;box-shadow:none;background:transparent";
  document.body.appendChild(textarea);
  textarea.focus({ preventScroll: true });
  textarea.select();
  textarea.setSelectionRange(0, text.length);

  let ok = false;
  try {
    ok = document.execCommand("copy");
  } finally {
    document.body.removeChild(textarea);
    if (selection) {
      selection.removeAllRanges();
    }
  }
  return ok;
}

function showStatus(text, type = "") {
  statusEl.textContent = text;
  statusEl.className = `status ${type}`.trim();
  statusEl.classList.remove("hidden");
}

function hideStatus() {
  statusEl.classList.add("hidden");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function highlightTitle(title, query) {
  const safe = escapeHtml(title);
  const q = query.trim();
  if (!q) return safe;
  const idx = title.toLowerCase().indexOf(q.toLowerCase());
  if (idx === -1) return safe;
  const before = escapeHtml(title.slice(0, idx));
  const match = escapeHtml(title.slice(idx, idx + q.length));
  const after = escapeHtml(title.slice(idx + q.length));
  return `${before}<mark>${match}</mark>`;
}

function buildGenerateUrl(item) {
  const params = new URLSearchParams({
    series_id: item.id,
    title: item.title || "",
    cover: item.cover || "",
    intro: item.intro || "",
  });
  return `/generate.html?${params.toString()}`;
}

function officialUrl(item) {
  if (item.url) return item.url;
  if (item.id) {
    return `https://www.novelquickapp.com/detail?series_id=${encodeURIComponent(item.id)}`;
  }
  return "";
}

function renderCard(item, query, index) {
  const href = officialUrl(item);
  const cover = item.cover
    ? `<img src="${escapeHtml(item.cover)}" alt="${escapeHtml(item.title)}" loading="lazy" referrerpolicy="no-referrer" />`
    : `<div class="placeholder">🎬</div>`;

  const meta = [];
  if (item.sub_title) meta.push(`<span class="tag muted">${escapeHtml(item.sub_title)}</span>`);
  if (item.episodes) meta.push(`<span class="tag">${item.episodes} 集</span>`);
  if (item.score) meta.push(`<span class="tag muted">${escapeHtml(String(item.score))} 分</span>`);

  const intro = item.intro || "暂无简介";
  const linkAttrs = href
    ? `href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer"`
    : "";

  const mainBlock = href
    ? `<a class="card-main" ${linkAttrs} title="在红果短剧官网打开">
        <div class="card-cover">${cover}</div>
        <div class="card-body">
          <h2 class="card-title">${highlightTitle(item.title, query)}</h2>
          <div class="card-meta">${meta.join("")}</div>
          <p class="card-intro">${escapeHtml(intro)}</p>
        </div>
      </a>`
    : `<div class="card-main">
        <div class="card-cover">${cover}</div>
        <div class="card-body">
          <h2 class="card-title">${highlightTitle(item.title, query)}</h2>
          <div class="card-meta">${meta.join("")}</div>
          <p class="card-intro">${escapeHtml(intro)}</p>
        </div>
      </div>`;

  const copyText = formatCopyText(item);

  return `
    <article class="card" style="animation-delay: ${index * 50}ms">
      ${mainBlock}
      <div class="card-id">
        <span>ID: ${escapeHtml(item.id)}</span>
        <textarea class="copy-source" readonly tabindex="-1" aria-hidden="true">${textareaContent(copyText)}</textarea>
        <button type="button" class="copy-btn">复制</button>
      </div>
      <button type="button" class="btn-voice-hint card-voice" data-title="${escapeHtml(item.title || "")}" title="请搜索《剧名》在红果短剧观看原片">🔊 语音</button>
      <a class="btn-generate" href="${escapeHtml(buildGenerateUrl(item))}">一键生成钩子视频</a>
    </article>
  `;
}

function bindVoiceHintButtons(root) {
  if (!root) return;
  root.querySelectorAll(".btn-voice-hint").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      playSearchHint(el.dataset.title || "");
    });
  });
}

async function copyFromCard(btn) {
  const source = btn.closest(".card")?.querySelector(".copy-source");
  if (!source?.value) return false;
  return copyToClipboard(source.value);
}

async function doSearch(query, { playVoice = false } = {}) {
  const q = query.trim();
  if (!q) return;

  currentQuery = q;
  btn.disabled = true;
  resultsEl.innerHTML = "";
  showStatus("正在搜索…");

  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || "搜索失败");
    }

    if (data.items.length === 0) {
      showStatus(`未找到与「${q}」相关的短剧，请换个关键词试试`);
      return;
    }

    hideStatus();
    lastSearchItems = data.items;
    resultsEl.innerHTML = data.items
      .map((item, i) => renderCard(item, q, i))
      .join("");
    bindVoiceHintButtons(resultsEl);

    if (playVoice && data.items[0]) {
      playSearchHint(data.items[0].title || q);
    }

    resultsEl.querySelectorAll(".copy-btn").forEach((el) => {
      el.addEventListener("mousedown", (e) => {
        e.preventDefault();
      });
      el.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const ok = await copyFromCard(el);
        el.textContent = ok ? "已复制" : "失败";
        setTimeout(() => {
          el.textContent = "复制";
        }, 1500);
      });
    });
  } catch (err) {
    showStatus(err.message || "网络错误，请稍后重试", "error");
  } finally {
    btn.disabled = false;
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  doSearch(input.value, { playVoice: true });
});

input.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  const val = input.value.trim();
  if (val.length >= 2) {
    debounceTimer = setTimeout(() => doSearch(val), 500);
  }
});

/* —— 去水印 —— */
(function initWatermarkTool() {
  const wmForm = document.getElementById("wm-form");
  const wmFile = document.getElementById("wm-file");
  const wmFileLabel = document.getElementById("wm-file-label");
  const wmPosition = document.getElementById("wm-position");
  const wmStrength = document.getElementById("wm-strength");
  const wmMethod = document.getElementById("wm-method");
  const wmToggleCustom = document.getElementById("wm-toggle-custom");
  const wmCustom = document.getElementById("wm-custom");
  const wmSubmit = document.getElementById("wm-submit");
  const wmStatus = document.getElementById("wm-status");
  const wmResult = document.getElementById("wm-result");
  const wmPreview = document.getElementById("wm-preview");
  const wmDownload = document.getElementById("wm-download");
  const wmUpload = wmForm?.querySelector(".wm-upload");

  if (!wmForm || !wmFile) return;

  let selectedFile = null;

  function setWmStatus(text, type = "") {
    wmStatus.textContent = text;
    wmStatus.className = `wm-status ${type}`.trim();
    wmStatus.classList.remove("hidden");
  }

  function hideWmStatus() {
    wmStatus.classList.add("hidden");
  }

  function onFileChosen(file) {
    if (!file) {
      selectedFile = null;
      wmFileLabel.textContent = "点击选择视频，或拖拽到此处";
      wmSubmit.disabled = true;
      return;
    }
    if (!file.type.startsWith("video/") && !/\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(file.name)) {
      setWmStatus("请选择视频文件（MP4 / MOV 等）", "error");
      return;
    }
    selectedFile = file;
    const mb = (file.size / 1024 / 1024).toFixed(1);
    wmFileLabel.textContent = `${file.name}（${mb} MB）`;
    wmSubmit.disabled = false;
    hideWmStatus();
    wmResult.classList.add("hidden");
  }

  wmFile.addEventListener("change", () => {
    onFileChosen(wmFile.files?.[0] || null);
  });

  if (wmUpload) {
    wmUpload.addEventListener("dragover", (e) => {
      e.preventDefault();
      wmUpload.classList.add("wm-dragover");
    });
    wmUpload.addEventListener("dragleave", () => {
      wmUpload.classList.remove("wm-dragover");
    });
    wmUpload.addEventListener("drop", (e) => {
      e.preventDefault();
      wmUpload.classList.remove("wm-dragover");
      const file = e.dataTransfer?.files?.[0];
      if (file) onFileChosen(file);
    });
  }

  wmToggleCustom?.addEventListener("click", () => {
    wmCustom.classList.toggle("hidden");
    const visible = !wmCustom.classList.contains("hidden");
    wmToggleCustom.textContent = visible ? "收起自定义" : "自定义区域";
  });

  wmForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!selectedFile) {
      setWmStatus("请先选择视频", "error");
      return;
    }

    const fd = new FormData();
    fd.append("file", selectedFile);
    fd.append("position", wmPosition.value);
    fd.append("strength", wmStrength?.value || "normal");
    if (wmMethod?.value) {
      fd.append("wm_method", wmMethod.value);
    }

    const useCustom = wmCustom && !wmCustom.classList.contains("hidden");
    if (useCustom) {
      const x = document.getElementById("wm-x")?.value;
      const y = document.getElementById("wm-y")?.value;
      const w = document.getElementBy("wm-w")?.value;
      const h = document.getElementById("wm-h")?.value;
      if (x !== "" && y !== "" && w !== "" && h !== "") {
        fd.append("x", x);
        fd.append("y", y);
        fd.append("w", w);
        fd.append("h", h);
      }
    }

    wmSubmit.disabled = true;
    wmResult.classList.add("hidden");

    const started = Date.now();
    let pollTimer = null;

    function formatElapsed(sec) {
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`;
    }

    async function pollWmJob(jobId) {
      while (true) {
        const elapsed = Math.floor((Date.now() - started) / 1000);
        let res;
        try {
          res = await fetch(`/api/tools/remove-watermark/job/${encodeURIComponent(jobId)}`);
        } catch {
          throw new Error("与服务器连接中断，请查看终端是否在运行并重试");
        }
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "查询进度失败");
        }
        const base = data.progress || "处理中…";
        setWmStatus(`${base}（已等待 ${formatElapsed(elapsed)}）`);

        if (data.status === "completed") {
          const previewUrl = data.preview_url || data.download_url;
          wmPreview.src = previewUrl;
          wmDownload.href = data.download_url || previewUrl;
          wmDownload.download = `去水印_${selectedFile.name.replace(/\.[^.]+$/, "")}.mp4`;

          const regions = data.regions || [];
          const mb = ((data.size || 0) / 1024 / 1024).toFixed(1);
          const regionHint =
            regions.length > 1
              ? `已去除 ${regions.length} 个区域`
              : regions[0]
                ? `区域 ${regions[0].w}×${regions[0].h}`
                : "";
          setWmStatus(
            `完成（约 ${mb} MB，用时 ${formatElapsed(elapsed)}）${regionHint ? "。" + regionHint : ""}`,
            "ok"
          );
          wmResult.classList.remove("hidden");
          return;
        }
        if (data.status === "failed") {
          throw new Error(data.error || "去水印失败");
        }
        if (elapsed > 1200) {
          throw new Error("处理超过 20 分钟，请换更短视频或先试「仅右下角」单区域");
        }
        await new Promise((r) => {
          pollTimer = setTimeout(r, 1500);
        });
      }
    }

    try {
      setWmStatus("正在上传视频…");
      const res = await fetch("/api/tools/remove-watermark", {
        method: "POST",
        body: fd,
      });
      const data = await res.json();
      if (!res.ok) {
        const d = data.detail;
        throw new Error(
          typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg || x).join("；") : "处理失败"
        );
      }
      if (!data.job_id) {
        throw new Error("服务器未返回任务 ID");
      }
      await pollWmJob(data.job_id);
    } catch (err) {
      setWmStatus(err.message || "去水印失败", "error");
    } finally {
      if (pollTimer) clearTimeout(pollTimer);
      wmSubmit.disabled = !selectedFile;
    }
  });
})();

/* —— 抖音分享缓存 —— */
(function initDouyinCacheTool() {
  const dyForm = document.getElementById("dy-form");
  const dyShare = document.getElementById("dy-share");
  const dyCookie = document.getElementById("dy-cookie");
  const dySubmit = document.getElementById("dy-submit");
  const DY_COOKIE_KEY = "hongguo_dy_cookie";

  if (dyCookie) {
    try {
      const saved = localStorage.getItem(DY_COOKIE_KEY);
      if (saved) dyCookie.value = saved;
    } catch {
      /* ignore */
    }
  }
  const dyStatus = document.getElementById("dy-status");
  const dyResult = document.getElementById("dy-result");
  const dyPreview = document.getElementById("dy-preview");
  const dyDownload = document.getElementById("dy-download");
  const dyMeta = document.getElementById("dy-meta");

  if (!dyForm || !dyShare) return;

  function setDyStatus(text, type = "") {
    dyStatus.textContent = text;
    dyStatus.className = `wm-status ${type}`.trim();
    dyStatus.classList.remove("hidden");
  }

  dyForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = dyShare.value.trim();
    const cookie = dyCookie?.value.trim() || "";
    if (!raw) {
      setDyStatus("请粘贴抖音分享链接或分享文案", "error");
      return;
    }
    dySubmit.disabled = true;
    dyResult.classList.add("hidden");

    try {
      try {
        localStorage.setItem(DY_COOKIE_KEY, cookie);
      } catch {
        /* ignore */
      }
      setDyStatus("正在爬取（解析链接 → 获取直链 → 下载），请稍候…");
      const res = await fetch("/api/tools/douyin-cache", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ share_url: raw, douyin_cookie: cookie }),
      });
      const rawBody = await res.text();
      let data = {};
      if (rawBody) {
        try {
          data = JSON.parse(rawBody);
        } catch {
          throw new Error(
            res.ok
              ? "服务器返回了无效数据"
              : rawBody.slice(0, 200) || `请求失败（HTTP ${res.status}）`
          );
        }
      }
      if (!res.ok) {
        const d = data.detail;
        throw new Error(
          typeof d === "string"
            ? d
            : Array.isArray(d)
              ? d.map((x) => x.msg || x).join("；")
              : rawBody.slice(0, 200) || `请求失败（HTTP ${res.status}）`
        );
      }

      const previewUrl = data.preview_url || data.public_url;
      const downloadUrl = data.download_url || previewUrl;
      if (!previewUrl) {
        throw new Error("服务器未返回视频地址");
      }
      const bust = `${previewUrl}${previewUrl.includes("?") ? "&" : "?"}t=${Date.now()}`;
      dyPreview.removeAttribute("src");
      dyPreview.load();
      dyPreview.src = bust;
      dyDownload.href = downloadUrl;
      dyDownload.removeAttribute("download");
      const nameBase = String(data.aweme_id || "douyin").replace(/\D/g, "") || "douyin";
      dyDownload.setAttribute("download", `douyin_${nameBase}.mp4`);

      const mb = ((data.size || 0) / 1024 / 1024).toFixed(1);
      const dur = data.duration ? `${Math.round(data.duration)} 秒` : "";
      const wmHint = data.watermark_free ? "无水印" : "含水印/平台流";
      dyMeta.textContent = [`ID：${data.aweme_id || "—"}`, dur, `${mb} MB`, wmHint]
        .filter(Boolean)
        .join(" · ");

      const method = data.crawl_method ? `（${data.crawl_method}）` : "";
      setDyStatus(`爬取完成${method} · ${wmHint}，可预览或下载`, "ok");
      dyResult.classList.remove("hidden");
    } catch (err) {
      setDyStatus(err.message || "抖音解析失败", "error");
    } finally {
      dySubmit.disabled = false;
    }
  });
})();
