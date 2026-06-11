/** 将调色 / 锐化 / 暗角写入视频文件（Canvas + MediaRecorder，保留原声） */
import { buildFilterCss } from "./video-player.js";

function pickRecorderMime() {
  for (const t of ["video/mp4", "video/webm;codecs=vp9", "video/webm"]) {
    if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

function unsharpCanvas(ctx, w, h, amount = 0.22) {
  const img = ctx.getImageData(0, 0, w, h);
  const src = img.data;
  const blur = new Uint8ClampedArray(src.length);
  blur.set(src);
  for (let y = 1; y < h - 1; y += 1) {
    for (let x = 1; x < w - 1; x += 1) {
      for (let c = 0; c < 3; c += 1) {
        let sum = 0;
        for (let dy = -1; dy <= 1; dy += 1) {
          for (let dx = -1; dx <= 1; dx += 1) {
            sum += src[((y + dy) * w + (x + dx)) * 4 + c];
          }
        }
        blur[(y * w + x) * 4 + c] = sum / 9;
      }
    }
  }
  for (let i = 0; i < src.length; i += 4) {
    for (let c = 0; c < 3; c += 1) {
      const o = src[i + c];
      const b = blur[i + c];
      const v = o + amount * (o - b);
      src[i + c] = v < 0 ? 0 : v > 255 ? 255 : v;
    }
  }
  ctx.putImageData(img, 0, 0);
}

function drawVignette(ctx, w, h) {
  const g = ctx.createRadialGradient(w / 2, h / 2, h * 0.15, w / 2, h / 2, h * 0.72);
  g.addColorStop(0, "rgba(0,0,0,0)");
  g.addColorStop(1, "rgba(0,0,0,0.38)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, w, h);
}

function drawEnhancedFrame(ctx, video, w, h, settings) {
  const filter = buildFilterCss({ ...settings, sharpen: false });
  ctx.filter = filter === "none" ? "none" : filter;
  ctx.drawImage(video, 0, 0, w, h);
  ctx.filter = "none";
  if (settings.sharpen) unsharpCanvas(ctx, w, h);
  if (settings.vignette) drawVignette(ctx, w, h);
}

export async function exportEnhancedVideoBuffer(arrayBuffer, settings, onProgress) {
  const mimeType = pickRecorderMime();
  if (!mimeType) {
    throw new Error("当前设备不支持导出增强视频，已改为保存原片");
  }

  const blob = new Blob([arrayBuffer], { type: "video/mp4" });
  const url = URL.createObjectURL(blob);

  try {
    return await transcodeWithEnhance(url, settings, mimeType, onProgress);
  } finally {
    URL.revokeObjectURL(url);
  }
}

function transcodeWithEnhance(videoUrl, settings, mimeType, onProgress) {
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.preload = "auto";
    video.src = videoUrl;

    let audioCtx = null;
    let recorder = null;
    let rafId = 0;

    video.onerror = () => reject(new Error("无法读取视频进行增强"));

    video.onloadedmetadata = async () => {
      const w = video.videoWidth;
      const h = video.videoHeight;
      const duration = video.duration || 0;
      if (!w || !h || !Number.isFinite(duration) || duration <= 0) {
        reject(new Error("视频元数据无效，无法增强"));
        return;
      }

      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d", { alpha: false });
      if (!ctx) {
        reject(new Error("无法创建画布"));
        return;
      }

      const canvasStream = canvas.captureStream(30);
      let combined;
      try {
        audioCtx = new AudioContext();
        const source = audioCtx.createMediaElementSource(video);
        const mute = audioCtx.createGain();
        mute.gain.value = 0;
        const dest = audioCtx.createMediaStreamDestination();
        source.connect(mute);
        mute.connect(audioCtx.destination);
        source.connect(dest);
        combined = new MediaStream([
          ...canvasStream.getVideoTracks(),
          ...dest.stream.getAudioTracks(),
        ]);
      } catch {
        combined = canvasStream;
      }

      const chunks = [];
      recorder = new MediaRecorder(combined, {
        mimeType,
        videoBitsPerSecond: 6_000_000,
      });
      recorder.ondataavailable = (e) => {
        if (e.data?.size) chunks.push(e.data);
      };
      recorder.onstop = async () => {
        try {
          await audioCtx?.close();
        } catch {
          /* ignore */
        }
        if (!chunks.length) {
          reject(new Error("增强导出未产生数据"));
          return;
        }
        const out = new Blob(chunks, { type: mimeType.split(";")[0] });
        resolve(await out.arrayBuffer());
      };
      recorder.onerror = () => reject(new Error("增强导出失败"));

      const draw = () => {
        if (video.ended) return;
        drawEnhancedFrame(ctx, video, w, h, settings);
        if (onProgress && duration > 0) {
          onProgress(Math.min(video.currentTime, duration), duration);
        }
        rafId = requestAnimationFrame(draw);
      };

      const seekT = duration > 0.3 ? Math.min(0.2, duration * 0.03) : 0;

      const startRecord = async () => {
        recorder.start(200);
        try {
          await video.play();
        } catch {
          reject(new Error("无法播放视频以进行增强（请重试）"));
          return;
        }
        draw();
      };

      if (seekT > 0) {
        video.onseeked = () => {
          video.onseeked = null;
          startRecord();
        };
        video.currentTime = seekT;
      } else {
        await startRecord();
      }

      video.onended = () => {
        cancelAnimationFrame(rafId);
        drawEnhancedFrame(ctx, video, w, h, settings);
        setTimeout(() => {
          try {
            recorder.stop();
          } catch (err) {
            reject(err);
          }
        }, 120);
      };
    };
  });
}
