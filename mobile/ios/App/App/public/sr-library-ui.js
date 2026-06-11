import { deleteSrLibraryItem, formatFileSize, loadSrLibrary, shareSrLibraryItem } from "./sr-library.js";

function formatTime(ts) {
  const d = new Date(ts || Date.now());
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function backendLabel(backend) {
  return backend === "native" ? "本机" : "Mac";
}

export function bindSrLibraryUI({
  panel,
  listEl,
  countEl,
  onChange,
} = {}) {
  if (!listEl) return async () => {};

  async function render() {
    const items = await loadSrLibrary();
    if (countEl) countEl.textContent = String(items.length);
    listEl.innerHTML = "";
    if (!items.length) {
      const empty = document.createElement("li");
      empty.className = "sr-library-empty";
      empty.textContent = "暂无超分成片";
      listEl.appendChild(empty);
      if (onChange) onChange(items);
      return items;
    }
    for (const item of items) {
      const li = document.createElement("li");
      li.className = "sr-library-item";
      const sizeHint =
        item.width && item.height ? `${item.width}×${item.height}` : "1080×1920";
      const fileSizeStr = formatFileSize(item.fileSize);
      
      // Debug: log thumbnail status
      console.log('Item thumbnail:', item.id, item.thumbnail ? 'exists' : 'missing', item.thumbnail?.substring(0, 50));
      
      // Build thumbnail HTML
      const thumbnailHtml = item.thumbnail && item.thumbnail.length > 100
        ? `<img src="${item.thumbnail}" alt="" class="sr-library-thumb" loading="lazy" />`
        : `<div class="sr-library-thumb-placeholder">🎬</div>`;
      
      li.innerHTML = `
        <div class="sr-library-content">
          ${thumbnailHtml}
          <div class="sr-library-info">
            <div class="sr-library-meta">
              <span class="sr-library-name">${item.filename || "video_sr.mp4"}</span>
              <span class="sr-library-sub">${backendLabel(item.backend)} · ${sizeHint} · ${fileSizeStr} · ${formatTime(item.createdAt)}</span>
            </div>
            <div class="sr-library-actions">
              <button type="button" class="btn-library-save" data-id="${item.id}">存相册</button>
              <button type="button" class="btn-library-del" data-id="${item.id}">删除</button>
            </div>
          </div>
        </div>
      `;
      listEl.appendChild(li);
    }
    if (onChange) onChange(items);
    return items;
  }

  listEl.addEventListener("click", async (e) => {
    const saveBtn = e.target.closest(".btn-library-save");
    const delBtn = e.target.closest(".btn-library-del");
    const items = await loadSrLibrary();
    if (saveBtn) {
      const item = items.find((x) => x.id === saveBtn.dataset.id);
      if (!item) {
        console.warn('Item not found:', saveBtn.dataset.id);
        return;
      }
      
      // Check if URI exists
      if (!item.uri) {
        alert('视频文件不存在，可能已被删除');
        return;
      }
      
      saveBtn.disabled = true;
      saveBtn.textContent = '保存中…';
      
      try {
        console.log('Sharing video:', item.uri);
        await shareSrLibraryItem(item);
        console.log('Share succeeded');
        // Show success feedback
        const originalText = saveBtn.textContent;
        saveBtn.textContent = '✓ 已保存';
        saveBtn.style.background = '#4CAF50';
        saveBtn.style.color = '#fff';
        setTimeout(() => {
          saveBtn.textContent = originalText;
          saveBtn.style.background = '';
          saveBtn.style.color = '';
        }, 2000);
      } catch (error) {
        console.error('Failed to share video:', error);
        alert(`保存失败：${error.message || '未知错误'}\n\n请检查：\n1. 是否授予相册权限\n2. 视频文件是否存在`);
      } finally {
        saveBtn.disabled = false;
        if (!saveBtn.textContent.includes('✓')) {
          saveBtn.textContent = '存相册';
        }
      }
      return;
    }
    if (delBtn) {
      const id = delBtn.dataset.id;
      if (!id) return;
      if (!confirm("确定删除这条超分成片？")) return;
      await deleteSrLibraryItem(id);
      await render();
    }
  });

  // Initialize panel visibility based on library items
  (async () => {
    if (panel) {
      const items = await loadSrLibrary();
      panel.open = items.length > 0;
    }
  })();
  
  return render;
}
