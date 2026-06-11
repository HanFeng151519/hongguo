import { deleteSrLibraryItem, loadSrLibrary, shareSrLibraryItem } from "./sr-library.js";

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
      li.innerHTML = `
        <div class="sr-library-meta">
          <span class="sr-library-name">${item.filename || "video_sr.mp4"}</span>
          <span class="sr-library-sub">${backendLabel(item.backend)} · ${sizeHint} · ${formatTime(item.createdAt)}</span>
        </div>
        <div class="sr-library-actions">
          <button type="button" class="btn-library-save" data-id="${item.id}">存相册</button>
          <button type="button" class="btn-library-del" data-id="${item.id}">删除</button>
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
      if (!item) return;
      saveBtn.disabled = true;
      try {
        await shareSrLibraryItem(item);
      } finally {
        saveBtn.disabled = false;
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
