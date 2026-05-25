"""红果/番茄 CENC 加密 MP4 解密与可播性检测。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

logger = logging.getLogger(__name__)

MP4DECRYPT = shutil.which("mp4decrypt") or "/opt/homebrew/bin/mp4decrypt"
PLAY_LICENSE_URL = os.getenv(
    "HONGGUO_PLAY_LICENSE_URL",
    "https://vas-lf-x.snssdk.com/video/playapi/1/play_licenses",
)


def _derive_candidate_keys(
    spade_a_b64: str, key_seed_b64: str, kid_hex: str
) -> list[tuple[str, bytes]]:
    spade = base64.b64decode(spade_a_b64) if spade_a_b64 else b""
    seed = base64.b64decode(key_seed_b64) if key_seed_b64 else b""
    kid = bytes.fromhex(kid_hex) if kid_hex else b""

    out: list[tuple[str, bytes]] = []

    def add(label: str, key: bytes) -> None:
        if len(key) == 16:
            out.append((label, key))

    add("md5_seed", hashlib.md5(seed).digest())
    add("md5_spade", hashlib.md5(spade).digest())
    add("md5_seed_spade", hashlib.md5(seed + spade).digest())
    add("md5_spade_seed", hashlib.md5(spade + seed).digest())
    add("md5_seed_b64", hashlib.md5(key_seed_b64.encode()).digest())
    add("md5_spade_b64", hashlib.md5(spade_a_b64.encode()).digest())
    add("sha256_seed", hashlib.sha256(seed).digest()[:16])
    add("seed16", seed[:16])
    add("spade16", spade[:16])
    if len(spade) >= 32:
        add("spade16b", spade[16:32])
    if len(kid) == 16:
        add("kid", kid)
        add("kid_xor_md5seed", bytes(a ^ b for a, b in zip(kid, hashlib.md5(seed).digest())))

    aes_keys = [
        ("md5seed", hashlib.md5(seed).digest()),
        ("seed16", seed[:16]),
        ("md5_b64", hashlib.md5(key_seed_b64.encode()).digest()),
    ]
    if len(kid) == 16:
        aes_keys.append(("kid", kid))
    for klabel, akey in aes_keys:
        try:
            pt = AES.new(akey, AES.MODE_ECB).decrypt(spade)
            add(f"ecb_spade_{klabel}", pt[:16])
            if len(pt) >= 32:
                add(f"ecb_spade2_{klabel}", pt[16:32])
        except (ValueError, KeyError):
            pass
        for iv_label, iv in [
            ("kid", kid[:16]),
            ("zero", b"\x00" * 16),
            ("seed", seed[:16]),
        ]:
            if len(iv) != 16:
                continue
            try:
                pt = AES.new(akey, AES.MODE_CBC, iv).decrypt(spade)
                add(f"cbc_{klabel}_{iv_label}", pt[:16])
                try:
                    add(f"cbc_unpad_{klabel}_{iv_label}", unpad(pt, 16)[:16])
                except ValueError:
                    pass
            except (ValueError, KeyError):
                pass

    add("hmac_md5", hmac.new(seed, spade, hashlib.md5).digest())
    add("hmac_sha256", hmac.new(seed, spade, hashlib.sha256).digest()[:16])

    seen: set[bytes] = set()
    deduped: list[tuple[str, bytes]] = []
    for label, key in out:
        if key not in seen:
            seen.add(key)
            deduped.append((label, key))
    return deduped


def _ffmpeg_path() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("未找到 ffmpeg")


def video_decodes(path: Path, *, min_frames: int = 8) -> bool:
    proc = subprocess.run(
        [_ffmpeg_path(), "-hide_banner", "-i", str(path), "-t", "2", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=45,
    )
    nums = [int(x) for x in re.findall(r"frame=\s*(\d+)", proc.stderr or "")]
    return bool(nums) and nums[-1] >= min_frames


def _decrypt_with_mp4decrypt(src: Path, dest: Path, key_hex: str, kid_hex: str) -> bool:
    if not Path(MP4DECRYPT).is_file():
        return False
    dest.unlink(missing_ok=True)
    for spec in (f"1:{key_hex}", f"{kid_hex}:{key_hex}", key_hex):
        proc = subprocess.run(
            [MP4DECRYPT, "--key", spec, str(src), str(dest)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 10_000:
            return True
    dest.unlink(missing_ok=True)
    return False


def _decrypt_with_ffmpeg(src: Path, dest: Path, key_hex: str) -> bool:
    dest.unlink(missing_ok=True)
    proc = subprocess.run(
        [
            _ffmpeg_path(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-decryption_key",
            key_hex,
            "-i",
            str(src),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return (
        proc.returncode == 0
        and dest.is_file()
        and dest.stat().st_size > 10_000
    )


def decrypt_mp4_with_key(src: Path, dest: Path, key: bytes, kid_hex: str) -> bool:
    key_hex = key.hex()
    if _decrypt_with_mp4decrypt(src, dest, key_hex, kid_hex):
        return video_decodes(dest)
    if _decrypt_with_ffmpeg(src, dest, key_hex):
        return video_decodes(dest)
    dest.unlink(missing_ok=True)
    return False


def _decode_license_key_blob(value: str) -> Optional[bytes]:
    raw = value.strip()
    if not raw:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        return bytes.fromhex(raw)
    try:
        decoded = base64.b64decode(raw + "==")
    except Exception:
        return None
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        return decoded[:16]
    return None


def _collect_keys_from_license_json(
    obj: Any, kid_hex: str, out: list[tuple[str, bytes]]
) -> None:
    kid_norm = kid_hex.lower().replace("-", "")
    seen = {key for _, key in out}

    def add(label: str, key: bytes) -> None:
        if len(key) == 16 and key not in seen:
            seen.add(key)
            out.append((label, key))

    if isinstance(obj, dict):
        key_blob = (
            obj.get("key")
            or obj.get("cipher")
            or obj.get("content_key")
            or obj.get("decrypt_key")
            or obj.get("key_value")
        )
        kid_field = str(
            obj.get("kid") or obj.get("key_id") or obj.get("KID") or ""
        ).lower().replace("-", "")
        if isinstance(key_blob, str):
            decoded = _decode_license_key_blob(key_blob)
            if decoded and (not kid_norm or not kid_field or kid_field == kid_norm):
                add("play_license", decoded)
        for child in obj.values():
            _collect_keys_from_license_json(child, kid_hex, out)
    elif isinstance(obj, list):
        for child in obj:
            _collect_keys_from_license_json(child, kid_hex, out)


async def fetch_play_license_keys(
    client: httpx.AsyncClient,
    *,
    authorization: str,
    video_id: str,
    file_id: str,
    kid: str,
    spade_a: str = "",
    key_seed: str = "",
    aid: str = "1967",
) -> list[tuple[str, bytes]]:
    """向字节 play_licenses 换取 CENC 内容密钥（需 App 抓包的 Authorization）。"""
    auth = authorization.strip()
    if not auth or not video_id:
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
        "Referer": "https://reading.snssdk.com/",
        "Authorization": auth,
    }
    params = {"video_id": video_id, "aid": aid}
    if file_id:
        params["file_id"] = file_id

    body = {
        "video_id": video_id,
        "file_id": file_id,
        "spade_a": spade_a,
        "key_seed": key_seed,
        "kid": kid,
    }

    keys: list[tuple[str, bytes]] = []
    for method, kwargs in (
        ("GET", {"params": params}),
        ("POST", {"params": params, "json": body}),
    ):
        try:
            if method == "GET":
                resp = await client.get(
                    PLAY_LICENSE_URL, headers=headers, **kwargs, timeout=20.0
                )
            else:
                resp = await client.post(
                    PLAY_LICENSE_URL, headers=headers, **kwargs, timeout=20.0
                )
        except httpx.HTTPError as exc:
            logger.debug("play_licenses %s 失败: %s", method, exc)
            continue

        if resp.status_code != 200:
            logger.debug(
                "play_licenses %s HTTP %s: %s",
                method,
                resp.status_code,
                resp.text[:200],
            )
            continue

        try:
            payload = resp.json()
        except json.JSONDecodeError:
            continue

        if (payload.get("ResponseMetadata") or {}).get("Error"):
            logger.debug(
                "play_licenses 错误: %s",
                (payload["ResponseMetadata"]["Error"]).get("Message"),
            )
            continue

        _collect_keys_from_license_json(payload, kid, keys)
        if keys:
            logger.info("play_licenses 返回 %d 个候选密钥", len(keys))
            return keys

    return keys


def try_decrypt_mp4(
    src: Path,
    dest: Path,
    *,
    spade_a: str,
    key_seed: str,
    kid: str,
    license_keys: Optional[list[tuple[str, bytes]]] = None,
) -> Optional[str]:
    """尝试解密；成功返回使用的密钥推导名称。"""
    for label, key in license_keys or []:
        if decrypt_mp4_with_key(src, dest, key, kid):
            logger.info("CENC 解密成功（%s）", label)
            return label
    for label, key in _derive_candidate_keys(spade_a, key_seed, kid):
        if decrypt_mp4_with_key(src, dest, key, kid):
            logger.info("CENC 解密成功（%s）", label)
            return label
    return None


def _audio_mean_volume_db(path: Path) -> Optional[float]:
    proc = subprocess.run(
        [
            _ffmpeg_path(),
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    for line in (proc.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:", 1)[1].split("dB")[0].strip())
            except ValueError:
                pass
    return None


def audio_is_audible(path: Path, *, min_db: float = -50.0) -> bool:
    vol = _audio_mean_volume_db(path)
    return vol is not None and vol > min_db


def try_extract_audio(
    src: Path,
    dest: Path,
    *,
    spade_a: str,
    key_seed: str,
    kid: str,
    seconds: float,
) -> Optional[str]:
    """从 CENC 加密 MP4 尝试仅提取可听的 AAC 音轨。"""
    dest.unlink(missing_ok=True)
    for label, key in _derive_candidate_keys(spade_a, key_seed, kid):
        key_hex = key.hex()
        for spec in (key_hex, f"1:{key_hex}", f"{kid}:{key_hex}"):
            proc = subprocess.run(
                [
                    _ffmpeg_path(),
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-decryption_key",
                    key_hex,
                    "-i",
                    str(src),
                    "-t",
                    str(seconds),
                    "-vn",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    str(dest),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0 and dest.is_file() and audio_is_audible(dest):
                logger.info("已提取剧集原声（%s）", label)
                return label
    dest.unlink(missing_ok=True)
    return None
