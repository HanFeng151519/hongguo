"""OpenCV 去水印：逐帧 inpaint，遮罩内全替换，外缘宽羽化（无贴块）。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def opencv_available() -> bool:
    try:
        import cv2  # noqa: F401

        return True
    except ImportError:
        return False


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def build_mask(
    height: int,
    width: int,
    rects: list[tuple[int, int, int, int]],
    *,
    dilate_px: int = 5,
):
    import numpy as np

    mask = np.zeros((height, width), dtype=np.uint8)
    for x, y, w, h in rects:
        x2 = min(width, x + w)
        y2 = min(height, y + h)
        if x2 > x and y2 > y:
            mask[y:y2, x:x2] = 255
    if dilate_px > 0:
        import cv2

        k = max(1, dilate_px)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        mask = cv2.dilate(mask, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask


def _core_mask(hard_mask, erode_px: int):
    """文字核心（略小于遮罩），用于消字，不整块替换。"""
    import cv2

    if erode_px <= 0:
        return hard_mask
    k = max(3, erode_px)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    core = cv2.erode(hard_mask, kernel, iterations=1)
    if cv2.countNonZero(core) < 16:
        return hard_mask
    return core


def _feather_mask(mask, feather_px: int):
    import cv2

    if feather_px <= 0:
        return mask
    k = feather_px * 2 + 1
    return cv2.GaussianBlur(mask, (k, k), 0)


def _inpaint_frame(frame, mask, *, radius: int):
    import cv2

    r = max(3, radius)
    out = cv2.inpaint(frame, mask, r, cv2.INPAINT_TELEA)
    out = cv2.inpaint(out, mask, max(3, r // 2), cv2.INPAINT_NS)
    return out


def _ring_mask(hard_mask, ring_px: int):
    import cv2

    ring_px = max(4, ring_px)
    k_out = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ring_px * 2 + 1, ring_px * 2 + 1))
    k_in = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    outer = cv2.dilate(hard_mask, k_out)
    inner = cv2.erode(hard_mask, k_in)
    return cv2.subtract(outer, inner)


def _color_harmonize(repaired, reference, hard_mask, *, ring_px: int):
    import cv2
    import numpy as np

    ring = _ring_mask(hard_mask, ring_px)
    if cv2.countNonZero(ring) < 24 or cv2.countNonZero(hard_mask) < 12:
        return repaired

    rep_lab = cv2.cvtColor(repaired, cv2.COLOR_BGR2LAB).astype(np.float32)
    ref_lab = cv2.cvtColor(reference, cv2.COLOR_BGR2LAB).astype(np.float32)
    core = hard_mask > 0
    ring_b = ring > 0

    for c in range(3):
        ref_mean = float(ref_lab[:, :, c][ring_b].mean())
        ref_std = float(ref_lab[:, :, c][ring_b].std()) + 1e-6
        rep_mean = float(rep_lab[:, :, c][core].mean())
        rep_std = float(rep_lab[:, :, c][core].std()) + 1e-6
        channel = rep_lab[:, :, c].copy()
        channel[core] = (channel[core] - rep_mean) * (ref_std / rep_std) + ref_mean
        rep_lab[:, :, c] = channel

    return cv2.cvtColor(np.clip(rep_lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


def _outward_alpha(hard_mask, feather_px: int):
    """
    遮罩内 alpha=1（全用 inpaint）；
    仅在遮罩外缘做宽羽化，避免贴块 / 画中画感。
    """
    import cv2
    import numpy as np

    inside = (hard_mask > 0).astype(np.float32)
    if feather_px <= 0:
        return inside

    k = feather_px * 2 + 1
    expanded = cv2.dilate(
        hard_mask,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)),
    )
    soft = cv2.GaussianBlur(expanded.astype(np.float32), (k, k), 0) / 255.0
    return np.maximum(soft, inside)


def _touch_core_text(repaired, core_mask, *, sigma: float):
    """仅在文字核心对修复结果轻模糊，消残留笔画，不贴新块。"""
    import cv2
    import numpy as np

    if sigma <= 0 or cv2.countNonZero(core_mask) < 8:
        return repaired
    soft = cv2.GaussianBlur(repaired, (0, 0), sigma)
    alpha = (core_mask.astype(np.float32) / 255.0)[:, :, None]
    out = repaired.astype(np.float32) * (1.0 - alpha) + soft.astype(np.float32) * alpha
    return np.clip(out, 0, 255).astype(np.uint8)


def _nearest_outside_color(frame, repair_mask, y: int, x: int, direction: str):
    import numpy as np

    h, w = frame.shape[:2]
    if direction == "left":
        ref_x = x - 1
        while ref_x >= 0:
            if repair_mask[y, ref_x] == 0:
                return frame[y, ref_x].astype(np.float32)
            ref_x -= 1
        return None
    if direction == "right":
        ref_x = x + 1
        while ref_x < w:
            if repair_mask[y, ref_x] == 0:
                return frame[y, ref_x].astype(np.float32)
            ref_x += 1
        return None
    ref_y = y - 1
    while ref_y >= 0:
        if repair_mask[ref_y, x] == 0:
            return frame[ref_y, x].astype(np.float32)
        ref_y -= 1
    return None


def _bgr_lightness(bgr) -> float:
    import cv2
    import numpy as np

    lab = cv2.cvtColor(np.uint8([[bgr.astype(np.uint8)]]), cv2.COLOR_BGR2LAB)[0, 0]
    return float(lab[0])


def _fill_tile_from_side(frame, repair_mask, *, corner: str = "bottom-right"):
    """
    按行把遮罩区域替换为紧邻外侧同宽像素条（复制纹理，不是纯色）。
    右下/抖音：从左侧复制；左下：从右侧复制。
    """
    import cv2
    import numpy as np

    if cv2.countNonZero(repair_mask) < 8:
        return frame

    out = frame.copy()
    ys, xs = np.where(repair_mask > 0)
    if len(xs) < 1:
        return out

    y1, y2 = int(ys.min()), int(ys.max())
    corner = (corner or "bottom-right").strip().lower()
    from_right = corner == "bottom-left"
    h, w = frame.shape[:2]

    for y in range(y1, y2 + 1):
        cols = np.where(repair_mask[y] > 0)[0]
        if cols.size == 0:
            continue
        c0, c1 = int(cols[0]), int(cols[-1])
        seg_w = c1 - c0 + 1
        row = np.zeros((seg_w, 3), dtype=frame.dtype)
        band_w = max(4, min(24, _env_int("HONGGUO_WM_TILE_BAND", 10)))
        if from_right:
            ref = frame[y, c1 + 1 : min(w, c1 + 1 + band_w)]
            if ref.size == 0:
                continue
            for i in range(seg_w):
                idx = min(i % ref.shape[0], ref.shape[0] - 1)
                row[i] = ref[idx]
        else:
            ref = frame[y, max(0, c0 - band_w) : c0]
            if ref.size == 0:
                continue
            for i in range(seg_w):
                idx = ref.shape[0] - 1 - (i % ref.shape[0])
                row[i] = ref[idx]
        out[y, c0 : c1 + 1] = row

    return out


def build_douyin_text_mask(
    frame,
    rects: list[tuple[int, int, int, int]],
) -> "object":
    """在搜索框内只标出亮色水印笔画（小云雀AI），不涂整块地板。"""
    import cv2
    import numpy as np

    vh, vw = frame.shape[:2]
    mask = np.zeros((vh, vw), dtype=np.uint8)
    if not rects:
        return mask

    for x, y, rw, rh in rects:
        x2 = min(vw, x + rw)
        y2 = min(vh, y + rh)
        if x2 <= x or y2 <= y:
            continue
        roi = frame[y:y2, x:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        k15 = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
        tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, k15)
        _, text = cv2.threshold(tophat, 8, 255, cv2.THRESH_BINARY)
        k3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        text = cv2.morphologyEx(text, cv2.MORPH_CLOSE, k3)
        mask[y:y2, x:x2] = cv2.bitwise_or(mask[y:y2, x:x2], text)

    return mask


def _erase_text_pixels(frame, text_mask, *, reference=None):
    """仅替换水印像素：复制左侧纹理条，避免整行同色造成条带。"""
    import cv2
    import numpy as np

    if cv2.countNonZero(text_mask) < 4:
        return frame

    ref_frame = reference if reference is not None else frame
    out = frame.copy()
    band_w = max(6, min(18, _env_int("HONGGUO_WM_TILE_BAND", 12)))
    for y in np.unique(np.where(text_mask > 0)[0]):
        cols = np.where(text_mask[y] > 0)[0]
        if cols.size == 0:
            continue
        c0 = int(cols.min())
        ref = ref_frame[y, max(0, c0 - band_w) : c0]
        if ref.size == 0:
            continue
        for x in cols:
            i = int(x) - c0
            out[y, x] = ref[ref.shape[0] - 1 - (i % ref.shape[0])]

    return out


def _deblend_white_overlay(frame, rects, text_mask):
    """
    白字半透明水印反混合：observed = floor*(1-a) + 255*a
    在遮罩附近估计 a，还原地板色。
    """
    import cv2
    import numpy as np

    out = frame.copy()
    vh, vw = frame.shape[:2]

    for gy, gx in zip(*np.where(text_mask > 0)):
        band = frame[gy, max(0, gx - 10) : gx]
        if band.size < 1:
            continue
        ref = np.median(band, axis=0).astype(np.float32)
        ref_l = float(
            cv2.cvtColor(np.uint8([[ref.astype(np.uint8)]]), cv2.COLOR_BGR2LAB)[0, 0, 0]
        )
        px = frame[gy, gx].astype(np.float32)
        px_l = float(cv2.cvtColor(np.uint8([[px.astype(np.uint8)]]), cv2.COLOR_BGR2LAB)[0, 0, 0])
        if px_l <= ref_l + 2.0:
            continue
        alpha = (px_l - ref_l) / max(6.0, 255.0 - ref_l)
        alpha = float(np.clip(alpha, 0.04, 0.92))
        restored = (px - 255.0 * alpha) / (1.0 - alpha)
        out[gy, gx] = np.clip(restored, 0, 255).astype(np.uint8)

    return out


def detect_douyin_vertices(frame) -> dict | None:
    """识别右下白色角标外接矩形与四角坐标。"""
    import cv2
    import numpy as np

    if frame is None:
        return None

    vh, vw = frame.shape[:2]
    zsy = max(0, int(vh * 0.91))
    zsx = max(0, int(vw * 0.72))
    zone = frame[zsy:vh, zsx:vw]
    if zone.size < 64:
        return None

    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 198, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 3))
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k, iterations=2)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None

    pick: tuple[int, int, int, int] | None = None
    for c in cnts:
        bx, by, bw, bh = cv2.boundingRect(c)
        if bw * bh < 100 or bw < 40 or bh < 8 or bh > 80:
            continue
        gx = zsx + bx
        if gx < int(vw * 0.68):
            continue
        if pick is None or gx + bw > pick[0]:
            pick = (gx + bw, bx, by, bw, bh)

    if pick is None:
        return None

    pad = max(0, _env_int("HONGGUO_WM_VERTEX_PAD", 2))
    _, bx, by, bw, bh = pick
    nx = max(0, zsx + bx - pad)
    ny = max(0, zsy + by - pad)
    x2 = min(vw, zsx + bx + bw + pad)
    y2 = min(vh, zsy + by + bh + pad)
    return {
        "rect": (nx, ny, x2 - nx, y2 - ny),
        "corners": {
            "tl": (nx, ny),
            "tr": (x2 - 1, ny),
            "bl": (nx, y2 - 1),
            "br": (x2 - 1, y2 - 1),
        },
    }


def fill_watermark_corner_color(
    frame,
    rect: tuple[int, int, int, int],
    *,
    above_px: int | None = None,
) -> tuple["object", dict]:
    """
    用户方案：识别水印四角后，从矩形上边正上方取参考色填满区域。
    默认 grid：矩形上、下外侧各取一行，逐列垂直插值（保留左右+上下渐变）。
    HONGGUO_WM_FILL_MODE=column 仅用上边一行；solid 仅用右上角上一像素。
    """
    import numpy as np

    vh, vw = frame.shape[:2]
    x, y, rw, rh = rect
    x2 = min(vw, x + rw)
    y2 = min(vh, y + rh)
    if x2 <= x or y2 <= y:
        return frame, {}

    gap = max(1, above_px if above_px is not None else _env_int("HONGGUO_WM_FILL_ABOVE", 1))
    ref_y = max(0, y - gap)
    mode = os.getenv("HONGGUO_WM_FILL_MODE", "grid").strip().lower()

    out = frame.copy()
    if mode == "solid":
        ref_x = min(vw - 1, x2 - 1)
        color = frame[ref_y, ref_x].copy()
        out[y:y2, x:x2] = color
        meta = {
            "fill_mode": "solid",
            "ref_pixel": {"x": int(ref_x), "y": int(ref_y)},
            "fill_bgr": [int(c) for c in color],
        }
    elif mode == "column":
        strip = frame[ref_y, x:x2].copy()
        out[y:y2, x:x2] = strip[np.newaxis, :, :]
        meta = {
            "fill_mode": "column",
            "ref_row_y": int(ref_y),
            "fill_bgr_left": [int(c) for c in strip[0]],
            "fill_bgr_right": [int(c) for c in strip[-1]],
        }
    else:
        # 默认 grid：外侧四边取色 + Coons 插值，四边亮度与周围地板严格对齐
        import cv2

        bot_y = y2 if y2 < vh else min(vh - 1, y2 - 1)
        lx0 = max(0, x - 3)
        rx1 = min(vw, x2 + 3)
        lab_full = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB).astype(np.float32)

        def _smooth_col(col_lab: np.ndarray) -> np.ndarray:
            if col_lab.shape[0] < 3:
                return col_lab
            k = min(7, col_lab.shape[0] | 1)
            out = np.empty_like(col_lab)
            for c in range(3):
                out[:, c] = cv2.GaussianBlur(col_lab[:, c].reshape(-1, 1), (1, k), 0).reshape(-1)
            return out

        left_x = max(0, x - 1)
        right_x = min(vw - 1, x2)
        left_edge = _smooth_col(lab_full[y:y2, left_x].copy())
        right_edge = _smooth_col(lab_full[y:y2, right_x].copy())

        rows = max(1, y2 - y)
        cols = max(1, x2 - x)
        patch_lab = np.zeros((rows, cols, 3), dtype=np.float32)
        for i in range(rows):
            lr = left_edge[i]
            rr = right_edge[i]
            for j in range(cols):
                tx = j / max(1, cols - 1)
                patch_lab[i, j] = (1.0 - tx) * lr + tx * rr

        patch = cv2.cvtColor(np.clip(patch_lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)

        if x >= 3:
            blur = cv2.GaussianBlur(frame, (0, 0), 1.0)
            grain = frame.astype(np.float32) - blur.astype(np.float32)
            ref_g = grain[y:y2, lx0:x].mean(axis=1, keepdims=True)
            patch = np.clip(patch.astype(np.float32) + ref_g * 0.15, 0, 255).astype(np.uint8)
            # 颗粒补偿后锁定左右边，亮度与外侧列完全一致
            patch[:, 0] = frame[y:y2, left_x]
            patch[:, -1] = frame[y:y2, right_x]

        out[y:y2, x:x2] = patch
        meta = {
            "fill_mode": "grid",
            "ref_top_y": int(ref_y),
            "ref_bot_y": int(bot_y),
            "fill_bgr_tl": [int(c) for c in cv2.cvtColor(np.uint8([[left_edge[0].astype(np.uint8)]]), cv2.COLOR_LAB2BGR)[0, 0]],
            "fill_bgr_tr": [int(c) for c in cv2.cvtColor(np.uint8([[right_edge[0].astype(np.uint8)]]), cv2.COLOR_LAB2BGR)[0, 0]],
            "fill_bgr_bl": [int(c) for c in cv2.cvtColor(np.uint8([[left_edge[-1].astype(np.uint8)]]), cv2.COLOR_LAB2BGR)[0, 0]],
            "fill_bgr_br": [int(c) for c in cv2.cvtColor(np.uint8([[right_edge[-1].astype(np.uint8)]]), cv2.COLOR_LAB2BGR)[0, 0]],
        }

    meta["corners"] = {
        "tl": [x, y],
        "tr": [x2 - 1, y],
        "bl": [x, y2 - 1],
        "br": [x2 - 1, y2 - 1],
    }
    return out, meta


def repair_douyin_floor(
    frame,
    rects: list[tuple[int, int, int, int]],
    *,
    text_mask=None,
):
    """
    抖音角标 + 平滑地板：按行拟合左侧亮度梯度，只改水印笔画及光晕像素；
    纹理自左侧同位移采样并校正亮度，避免整块矩形替换与 inpaint 糊块。
    """
    import cv2
    import numpy as np

    if not rects:
        return frame

    vh, vw = frame.shape[:2]
    out = frame.copy()
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB).astype(np.float32)
    L = lab[:, :, 0]

    if text_mask is None:
        text_mask = build_douyin_text_mask(frame, rects)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    text_hit = cv2.dilate(text_mask, k, iterations=1)

    band = max(12, _env_int("HONGGUO_WM_FLOOR_BAND", 36))
    bright_d = _env_float("HONGGUO_WM_FLOOR_BRIGHT", 0.5)
    dark_d = _env_float("HONGGUO_WM_FLOOR_DARK", 2.0)

    for x, y, rw, rh in rects:
        x2 = min(vw, x + rw)
        y2 = min(vh, y + rh)
        if x2 <= x or y2 <= y or x < rw:
            continue
        for gy in range(y, y2):
            x0 = max(0, x - band)
            cols = np.arange(x0, x, dtype=np.float32)
            if cols.size < 4:
                continue
            a, b = np.polyfit(cols, L[gy, x0:x].astype(np.float32), 1)
            for cx in range(x, x2):
                l_pred = float(a * cx + b)
                l_obs = float(L[gy, cx])
                if (
                    text_hit[gy, cx] == 0
                    and l_obs <= l_pred + bright_d
                    and l_obs >= l_pred - dark_d
                ):
                    continue
                sx = cx - rw
                plab = lab[gy, sx].copy()
                l_pred_sx = float(a * sx + b)
                plab[0] = np.clip(l_pred + (float(plab[0]) - l_pred_sx), 0, 255)
                out[gy, cx] = cv2.cvtColor(
                    np.uint8([[plab.astype(np.uint8)]]),
                    cv2.COLOR_LAB2BGR,
                )[0, 0]

    return out


def _restore_grain(out, frame, text_mask):
    """把原图左侧颗粒感补回修复区，减轻「糊一块」。"""
    import cv2
    import numpy as np

    if cv2.countNonZero(text_mask) < 4:
        return out

    blur = cv2.GaussianBlur(frame, (0, 0), 1.1)
    grain = frame.astype(np.float32) - blur.astype(np.float32)
    result = out.copy()
    for gy, gx in zip(*np.where(text_mask > 0)):
        ref_x = max(0, gx - 1)
        while ref_x >= 0 and text_mask[gy, ref_x]:
            ref_x -= 1
        if ref_x < 0:
            ref_x = max(0, gx - 1)
        result[gy, gx] = np.clip(
            result[gy, gx].astype(np.float32) + grain[gy, ref_x] * 0.9,
            0,
            255,
        ).astype(np.uint8)
    return result


def _cleanup_residual_text(frame, rects):
    """搜索区内仍明显亮于同行的像素，用左侧地板像素覆盖。"""
    import cv2
    import numpy as np

    out = frame.copy()
    vh, vw = frame.shape[:2]
    for x, y, rw, rh in rects:
        x2, y2 = min(vw, x + rw), min(vh, y + rh)
        for ry in range(y, y2):
            row = out[ry, x:x2]
            gray = cv2.cvtColor(row.reshape(1, -1, 3), cv2.COLOR_BGR2GRAY).flatten()
            med = float(np.median(gray))
            hot = gray > med + 11
            if not np.any(hot):
                continue
            ref_x = max(0, x - 1)
            ref = out[ry, ref_x]
            for ci, is_hot in enumerate(hot):
                if is_hot:
                    row[ci] = ref
            out[ry, x:x2] = row
    return out


def _fill_from_surround(
    frame,
    repair_mask,
    *,
    band: int = 16,
    corner: str = "bottom-right",
):
    """平滑背景：仅擦除亮色水印像素。"""
    import cv2
    import numpy as np

    ys, xs = np.where(repair_mask > 0)
    if len(xs) < 1:
        return frame
    rects = [
        (
            int(xs.min()),
            int(ys.min()),
            int(xs.max() - xs.min()) + 1,
            int(ys.max() - ys.min()) + 1,
        )
    ]
    text_mask = build_douyin_text_mask(frame, rects)
    if cv2.countNonZero(text_mask) < 8:
        text_mask = repair_mask
    return _erase_text_pixels(frame, text_mask)


def _apply_mask_fill(frame, filled, mask):
    """遮罩内直接替换为填充色，不做羽化，避免和原图混出色差。"""
    import numpy as np

    out = frame.copy()
    m = mask > 0
    out[m] = filled[m]
    return out


def _finish_smooth_frame(frame, repaired, core_mask, *, feather_px: int):
    """平滑背景修复：默认硬替换；feather_px>0 时仅外缘 1 圈轻羽化。"""
    if feather_px <= 0:
        return _apply_mask_fill(frame, repaired, core_mask)

    import cv2
    import numpy as np

    edge = cv2.subtract(
        cv2.dilate(core_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))),
        cv2.erode(core_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))),
    )
    out = _apply_mask_fill(frame, repaired, core_mask)
    if cv2.countNonZero(edge) < 4:
        return out

    k = feather_px * 2 + 1
    alpha = cv2.GaussianBlur(edge.astype(np.float32), (k, k), 0) / 255.0
    alpha3 = np.stack([alpha, alpha, alpha], axis=-1)
    base = frame.astype(np.float32)
    over = repaired.astype(np.float32)
    mixed = base * (1.0 - alpha3) + over * alpha3
    out = np.where(edge[:, :, None] > 0, np.clip(mixed, 0, 255), out.astype(np.float32))
    return np.clip(out, 0, 255).astype(np.uint8)


def _inpaint_text_only(frame, mask, *, radius: int = 2):
    import cv2

    if cv2.countNonZero(mask) < 8:
        return frame
    return cv2.inpaint(frame, mask, max(1, radius), cv2.INPAINT_TELEA)


def _text_only_mask(frame, region_mask, *, bright_thresh: int = 175):
    """水印文字/图标多为亮色，仅对这些像素做轻量修复。"""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    text = ((gray >= bright_thresh) & (region_mask > 0)).astype(np.uint8) * 255
    if cv2.countNonZero(text) < 8:
        return region_mask
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    return cv2.dilate(text, k, iterations=1)


def _finish_frame(frame, inpainted, hard_mask, *, feather_px: int, core_mask):
    import numpy as np

    repaired = inpainted
    if _env_int("HONGGUO_WM_INPAINT_COLOR_MATCH", 1):
        ring_px = _env_int("HONGGUO_WM_INPAINT_RING", 10)
        repaired = _color_harmonize(repaired, frame, hard_mask, ring_px=ring_px)

    alpha = _outward_alpha(hard_mask, feather_px)
    alpha3 = np.stack([alpha, alpha, alpha], axis=-1)
    base = frame.astype(np.float32)
    over = repaired.astype(np.float32)
    out = base * (1.0 - alpha3) + over * alpha3
    return np.clip(out, 0, 255).astype(np.uint8)


def inpaint_video(
    src: Path,
    dest: Path,
    rects: list[tuple[int, int, int, int]],
    *,
    radius: int = 5,
    dilate_px: int = 5,
    strength: str = "normal",
) -> None:
    import cv2

    if not rects:
        raise RuntimeError("未指定水印区域")

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError("无法打开视频（OpenCV）")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    if fps < 1:
        fps = 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width < 2 or height < 2:
        cap.release()
        raise RuntimeError("无法读取视频分辨率")

    s_key = (strength or "normal").strip().lower()
    hard_mask = build_mask(height, width, rects, dilate_px=dilate_px)
    erode_px = _env_int(
        "HONGGUO_WM_CORE_ERODE",
        {"tight": 8, "normal": 6, "strong": 4}.get(s_key, 6),
    )
    core_mask = _core_mask(hard_mask, erode_px)
    repair_mask = core_mask
    feather_px = _env_int(
        "HONGGUO_WM_INPAINT_FEATHER",
        {"tight": 8, "normal": 10, "strong": 12}.get(s_key, 10),
    )
    core_sigma = {"tight": 0.0, "normal": 2.5, "strong": 4.0}.get(s_key, 2.5)

    tmp = dest.with_suffix(".cv.mp4")
    tmp.unlink(missing_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp), fourcc, fps, (width, height))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("无法创建临时视频（OpenCV VideoWriter）")

    frames = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        inp = _inpaint_frame(frame, repair_mask, radius=radius)
        inp = _touch_core_text(inp, core_mask, sigma=core_sigma)
        frame = _finish_frame(frame, inp, repair_mask, feather_px=feather_px, core_mask=core_mask)
        writer.write(frame)
        frames += 1

    cap.release()
    writer.release()

    if frames < 1 or not tmp.is_file() or tmp.stat().st_size < 10_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("修复后视频为空")

    logger.info(
        "OpenCV inpaint %s frames %dx%d r=%d dilate=%d feather=%d strength=%s",
        frames,
        width,
        height,
        radius,
        dilate_px,
        feather_px,
        s_key,
    )
    dest.unlink(missing_ok=True)
    tmp.replace(dest)
