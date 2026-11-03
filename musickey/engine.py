"""批量任务：输入收集、流式解码、统一转码、标签嵌入与原子落盘。"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .errors import JobCancelledError, MusicKeyError
from .formats import decoder_for, supported_extensions
from .formats.base import DecodeOptions
from .logging_setup import get_logger
from .model import FileOutcome
from .tags import embed, read_metadata
from .transcode import transcode

ProgressEvent = Callable[[int, int, str], None]
StageEvent = Callable[[str], None]

logger = get_logger("musickey.engine")


@dataclass(slots=True)
class BatchOptions:
    output_dir: Path
    target: str | None = None
    embed_cover: bool = True
    force: bool = False
    recursive: bool = True
    dry_run: bool = False
    ffmpeg: str | None = None
    decode: DecodeOptions = field(default_factory=DecodeOptions)
    jobs: int = 1


def _is_supported_name(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in supported_extensions())


def collect_inputs(inputs: Iterable[Path], recursive: bool = True) -> list[Path]:
    """把文件/目录展开为受支持的候选文件，保留稳定顺序并去重。"""
    files: list[Path] = []
    seen: set[Path] = set()
    for item in inputs:
        if item.is_dir():
            pattern = item.rglob("*") if recursive else item.glob("*")
            for child in sorted(pattern):
                if child.is_file() and _is_supported_name(child.name) and child not in seen:
                    seen.add(child)
                    files.append(child)
        elif item.is_file() and item not in seen:
            seen.add(item)
            files.append(item)
    return files


def _unique_path(directory: Path, stem: str, ext: str, force: bool) -> Path | None:
    candidate = directory / f"{stem}.{ext}"
    if not candidate.exists() or force:
        return candidate
    for index in range(1, 1000):
        candidate = directory / f"{stem} ({index}).{ext}"
        if not candidate.exists():
            return candidate
    return None


def _safe_stem(name: str) -> str:
    stem = Path(name).stem or "audio"
    cleaned = "".join("_" if c in '\\/:*?"<>|' or ord(c) < 32 else c for c in stem)
    return cleaned.strip(". ") or "audio"


def _progress_safe(callback, done: int, total: int, phase: str) -> None:
    if callback:
        callback(max(0, min(done, total)), max(1, total), phase)


def process_one(
    path: Path,
    opts: BatchOptions,
    progress: ProgressEvent | None = None,
    cancel_event: threading.Event | None = None,
    on_stage: StageEvent | None = None,
) -> FileOutcome:
    """处理单个文件；任何单文件错误只反映在 outcome 中，不中断批次。"""
    outcome = FileOutcome(input=path)
    temp_path: Path | None = None
    raw_path: Path | None = None
    started = time.perf_counter()

    def stage(name: str) -> None:
        _check_cancel(cancel_event)
        if on_stage is not None:
            on_stage(name)
        logger.info("阶段开始: %s -> %s", path.name, name)

    try:
        decoder = decoder_for(path)
        opts.output_dir.mkdir(parents=True, exist_ok=True)

        # 先解码到临时输出，结果容器未知时再决定最终扩展名。
        stage("decode")
        decode_started = time.perf_counter()
        probe_suffix = "." + uuid.uuid4().hex[:8]
        temp_path = opts.output_dir / probe_suffix
        with temp_path.open("wb") as out:
            result = decoder.decode_to(
                path,
                out,
                opts.decode,
                progress=lambda done, total, phase: (
                    _check_cancel(cancel_event),
                    _progress_safe(progress, done, total, phase),
                )[1],
            )
        decoded_bytes = temp_path.stat().st_size if temp_path.exists() else 0
        logger.info(
            "解码完成: %s container=%s %.2f MB 用时=%.2fs",
            path.name,
            result.container,
            decoded_bytes / (1024 * 1024),
            time.perf_counter() - decode_started,
        )

        if not result.container or result.container == "bin":
            raise MusicKeyError("解密结果不是可识别的音频容器")

        target = opts.target
        final_ext = target or result.suggested_extension
        final = _unique_path(opts.output_dir, _safe_stem(path.name), final_ext, opts.force)
        if final is None:
            outcome.status = "skipped"
            outcome.note = "输出已存在（--force 可覆盖）"
            return outcome
        outcome.output = final
        outcome.source = result.source
        outcome.container = result.container
        if opts.dry_run:
            outcome.note = f"dry-run: 将输出 {final.name}"
            return outcome

        if target and target != result.container:
            stage("transcode")
            transcode_started = time.perf_counter()
            raw_path = opts.output_dir / f".{uuid.uuid4().hex}.raw"
            shutil.move(str(temp_path), raw_path)
            temp_path = opts.output_dir / f".{uuid.uuid4().hex}.{final_ext}"
            transcode(raw_path, temp_path, target, opts.ffmpeg)
            raw_path.unlink(missing_ok=True)
            raw_path = None
            logger.info(
                "转码完成: %s -> %s 用时=%.2fs",
                path.name,
                target,
                time.perf_counter() - transcode_started,
            )

        if opts.embed_cover:
            stage("embed")
            embed_started = time.perf_counter()
            existing_tags, existing_cover = read_metadata(temp_path, final_ext)
            merged_tags = {**existing_tags, **result.tags}
            merged_cover = result.cover or existing_cover
            embed(final_ext, temp_path, merged_tags, merged_cover)
            logger.info(
                "标签写入完成: %s 用时=%.2fs",
                path.name,
                time.perf_counter() - embed_started,
            )

        stage("publish")
        os.replace(temp_path, final)
        temp_path = None
        outcome.size = final.stat().st_size if final.exists() else 0
        outcome.note = f"{result.container} -> {final_ext}"
        logger.info(
            "文件输出完成: %s -> %s 总用时=%.2fs",
            path.name,
            final.name,
            time.perf_counter() - started,
        )
        return outcome
    except JobCancelledError:
        outcome.status = "cancelled"
        outcome.note = "用户取消了任务"
        return outcome
    except MusicKeyError as exc:
        outcome.status = "failed"
        outcome.note = str(exc)
        return outcome
    except Exception as exc:
        outcome.status = "failed"
        outcome.note = f"{type(exc).__name__}: {exc}"
        return outcome
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        if raw_path:
            raw_path.unlink(missing_ok=True)


def _check_cancel(event: threading.Event | None) -> None:
    if event and event.is_set():
        raise JobCancelledError()


def run_batch(
    inputs: Iterable[Path],
    opts: BatchOptions,
    on_event: Callable[[FileOutcome], None] | None = None,
    progress: ProgressEvent | None = None,
    cancel_event: threading.Event | None = None,
) -> list[FileOutcome]:
    files = collect_inputs(inputs, opts.recursive)
    if not files:
        return []
    results: list[FileOutcome] = []
    if opts.jobs <= 1 or len(files) == 1:
        for path in files:
            _check_cancel(cancel_event)
            outcome = process_one(path, opts, progress, cancel_event)
            results.append(outcome)
            if on_event:
                on_event(outcome)
        return results

    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=min(opts.jobs, len(files))) as executor:
        future_map = {
            executor.submit(process_one, path, opts, progress, cancel_event): path for path in files
        }
        for future in as_completed(future_map):
            outcome = future.result()
            with lock:
                results.append(outcome)
                if on_event:
                    on_event(outcome)
    return results


def summarize(results: list[FileOutcome]) -> dict[str, int]:
    return {
        "ok": sum(1 for item in results if item.status == "ok"),
        "skipped": sum(1 for item in results if item.status == "skipped"),
        "failed": sum(1 for item in results if item.status == "failed"),
        "cancelled": sum(1 for item in results if item.status == "cancelled"),
    }
