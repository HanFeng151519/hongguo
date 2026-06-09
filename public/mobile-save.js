/**
 * 手机浏览器保存图片：优先系统分享，否则长按预览图保存（HTTP 局域网可用）。
 */
(function (global) {
  const previewUrls = [];

  function revokePreviews() {
    while (previewUrls.length) {
      const u = previewUrls.pop();
      try {
        URL.revokeObjectURL(u);
      } catch {
        /* ignore */
      }
    }
  }

  function stemName(filename) {
    const base = String(filename || "image.jpg").replace(/[/\\]/g, "_");
    const i = base.lastIndexOf(".");
    return i > 0 ? base.slice(0, i) : base;
  }

  function extFromBlob(blob, filename) {
    const t = (blob.type || "").toLowerCase();
    if (t.includes("png")) return ".png";
    if (t.includes("webp")) return ".webp";
    if (t.includes("jpeg") || t.includes("jpg")) return ".jpg";
    const m = String(filename || "").match(/\.[a-z0-9]+$/i);
    return m ? m[0].toLowerCase() : ".jpg";
  }

  function blobToFile(blob, originalName) {
    const ext = extFromBlob(blob, originalName);
    const name = `${stemName(originalName)}_wm${ext}`;
    const type = blob.type || "image/jpeg";
    return new File([blob], name, { type });
  }

  function canTryShare() {
    return typeof navigator.share === "function";
  }

  async function tryShareFiles(files, title) {
    if (!canTryShare() || !files.length) return false;

    const tryOnce = async (list) => {
      try {
        if (navigator.canShare && !navigator.canShare({ files: list })) {
          return false;
        }
      } catch {
        /* 部分浏览器 canShare 不可靠，继续尝试 share */
      }
      try {
        await navigator.share({
          files: list,
          title: title || list[0]?.name || "去水印图片",
        });
        return true;
      } catch (err) {
        if (err?.name === "AbortError") throw err;
        return false;
      }
    };

    if (await tryOnce(files)) return true;

    if (files.length <= 1) return false;

    for (const file of files) {
      const ok = await tryOnce([file]);
      if (!ok) return false;
    }
    return true;
  }

  function renderGallery(container, files) {
    if (!container) return;
    revokePreviews();
    container.innerHTML = "";

    const hint = document.createElement("p");
    hint.className = "wm-desc wm-save-hint";
    hint.textContent =
      "長按下方圖片 → 選「加入照片」/「儲存圖像」。若用微信內建瀏覽器，請點右上角 ⋯ 用 Safari 打開。";
    container.appendChild(hint);

    const grid = document.createElement("div");
    grid.className = "wm-img-save-grid";
    files.forEach((file, i) => {
      const url = URL.createObjectURL(file);
      previewUrls.push(url);
      const wrap = document.createElement("figure");
      wrap.className = "wm-img-save-item";
      const img = document.createElement("img");
      img.src = url;
      img.alt = file.name || `image_${i + 1}`;
      img.decoding = "async";
      const cap = document.createElement("figcaption");
      cap.textContent = file.name || `圖片 ${i + 1}`;
      wrap.appendChild(img);
      wrap.appendChild(cap);
      grid.appendChild(wrap);
    });
    container.appendChild(grid);
    container.classList.remove("hidden");
    container.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function saveFiles(files, options = {}) {
    if (!files?.length) throw new Error("沒有可保存的圖片");

    if (canTryShare()) {
      try {
        const shared = await tryShareFiles(files, options.title);
        if (shared) {
          return { mode: "share", count: files.length };
        }
      } catch (err) {
        if (err?.name === "AbortError") throw err;
      }
    }

    renderGallery(options.galleryEl, files);
    return { mode: "gallery", count: files.length };
  }

  async function saveVideoFromUrl(url, filename = "video.mp4") {
    const full = global.HongguoApi?.apiUrl ? global.HongguoApi.apiUrl(url) : url;
    const res = await fetch(full);
    if (!res.ok) throw new Error(`下載失敗（HTTP ${res.status}）`);
    let blob = await res.blob();
    if (!blob.size) throw new Error("視頻為空");
    const mime = (res.headers.get("content-type") || blob.type || "video/mp4").split(";")[0];
    if (!blob.type && mime) blob = new Blob([blob], { type: mime });
    const name = String(filename || "video.mp4").replace(/[/\\]/g, "_");
    const file = new File([blob], name.endsWith(".mp4") ? name : `${name}.mp4`, {
      type: mime.includes("video") ? mime : "video/mp4",
    });

    if (canTryShare()) {
      try {
        const shared = await tryShareFiles([file], name);
        if (shared) return { mode: "share", count: 1 };
      } catch (err) {
        if (err?.name === "AbortError") throw err;
      }
    }

    throw new Error(
      "無法彈出分享菜單。請用 Safari 打開，長按上方視頻預覽 →「儲存到照片」。"
    );
  }

  global.HongguoMobileSave = {
    blobToFile,
    canTryShare,
    renderGallery,
    saveFiles,
    saveVideoFromUrl,
    revokePreviews,
  };
})(window);
