"""ffmpeg 定位与音频转码。"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .errors import MusicKeyError
from .logging_setup import get_logger

logger = get_logger("musickey.transcode")

TARGETS = ("mp3", "flac", "m4a", "wav", "ogg")

_ENCODER_ARGS = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    "flac": ["-c:a", "flac", "-compression_level", "8"],
    "m4a": ["-c:a", "aac", "-b:a", "256k"],
    "wav": ["-c:a", "pcm_s16le"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "6"],
}

_WINDOWS_PATHS = (
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    r"C:\ProgramData\winget\packages\Gyan.FFmpeg\ffmpeg.exe",
)


def resolve_ffmpeg(explicit: str | None = None) -> str | None:
    """按显式参数、环境变量、内置资源、PATH、常见安装位置查找 ffmpeg。"""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("MUSICKEY_FFMPEG"):
        candidates.append(os.environ["MUSICKEY_FFMPEG"])
    if os.environ.get("FFMPEG_PATH"):
        candidates.append(os.environ["FFMPEG_PATH"])
    try:
        from .resources import asset_path, bundled_path

        inline_bundle = asset_path("bin/ffmpeg.exe")
        old_bundle = bundled_path("build_assets/ffmpeg.exe")
        for bundled in (inline_bundle, old_bundle):
            if bundled.exists():
                candidates.append(str(bundled))
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    candidates.extend(_WINDOWS_PATHS)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve())
    return None


def transcode(src: Path, dst: Path, target: str, ffmpeg: str | None = None, timeout: int = 3600) -> None:
    """把 src 解码为 target 容器并以原子方式写入 dst。"""
    binary = resolve_ffmpeg(ffmpeg)
    if binary is None:
        raise MusicKeyError(
            "未找到 ffmpeg：请安装并加入 PATH，或设置 --ffmpeg / MUSICKEY_FFMPEG。"
        )
    args = _ENCODER_ARGS.get(target)
    if args is None:
        raise MusicKeyError(f"不支持的转码目标: {target}")
    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "0",
        *args,
        str(dst),
    ]
    logger.info("ffmpeg 开始: %s -> %s (%s)", src.name, target, binary)
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        logger.error("ffmpeg 转码超时: %s（%ss）", src.name, timeout)
        raise MusicKeyError("ffmpeg 转码超时") from exc
    elapsed = time.perf_counter() - started
    if proc.returncode != 0 or not dst.exists():
        detail = proc.stderr.strip()[:300]
        logger.error(
            "ffmpeg 转码失败: %s rc=%s 用时=%.2fs stderr=%s",
            src.name,
            proc.returncode,
            elapsed,
            detail,
        )
        raise MusicKeyError(f"ffmpeg 转码失败: {detail or '未知错误'}")
    logger.info("ffmpeg 完成: %s -> %s 用时=%.2fs", src.name, target, elapsed)
