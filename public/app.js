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
