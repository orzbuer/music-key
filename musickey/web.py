"""本地网页服务：异步任务队列、流式进度与成品落盘。

服务默认只监听 127.0.0.1。上传文件写到成品目录下的临时文件夹，
处理完立刻以原文件名移入成品目录，并删除该任务的临时文件。
"""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from . import __version__
from .engine import BatchOptions, _safe_stem, _unique_path, process_one
from .errors import JobCancelledError, MusicKeyError
from .formats import supported_extensions
from .formats.base import DecodeOptions
from .logging_setup import default_log_path, get_logger, setup_logging
from .model import OUTPUT_EXTENSIONS
from .resources import bundled_path
from .tags import read_metadata
from .transcode import TARGETS, resolve_ffmpeg

APP_NAME = "MusicKey"
WORK_DIR_NAME = ".musickey-work"
LYRIC_EXTENSIONS = {".lrc", ".krc", ".qrc"}
QUEUE_NOTICE_SECONDS = 30.0
DEFAULT_STAGE_TIMEOUTS = {
    "prepare": 30.0,
    "decode": 90.0,
    "transcode": 600.0,
    "embed": 120.0,
    "publish": 120.0,
}

logger = get_logger("musickey.web")


def default_config_path() -> Path:
    return Path.home() / ".musickey" / "web.json"


def default_output_dir() -> Path:
    return Path.cwd() / "unlocked"


def validate_output_dir(path: Path) -> Path:
    """确认成品目录可创建且可写，并返回绝对路径。"""
    raw = str(path).strip()
    if not raw:
        raise ValueError("请填写成品目录")
    resolved = Path(raw).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    resolved = resolved.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("路径不是文件夹")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / f".musickey-write-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"目录不可写: {exc}") from exc
    return resolved


def resolve_startup_output(override: Path | None = None, config_path: Path | None = None) -> Path:
    """本次启动使用的成品目录：命令行优先，其次上次保存的路径，最后是 ./unlocked。"""
    if override is not None:
        return validate_output_dir(override)
    path = config_path or default_config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("output_dir")
            if isinstance(raw, str) and raw.strip():
                return validate_output_dir(Path(raw))
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return validate_output_dir(default_output_dir())


def open_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    target = str(folder.resolve())
    if sys.platform == "win32":
        # 请求跑在 Flask 工作线程里，os.startfile 经常无法弹出资源管理器。
        subprocess.Popen(["explorer", target])
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
        return
    subprocess.Popen(["xdg-open", target])


def pick_folder(initial: Path | None = None) -> str | None:
    """弹出本机文件夹选择框。用户取消时返回 None。"""
    if sys.platform == "win32":
        return _pick_folder_windows(initial)
    return _pick_folder_tk(initial)


def _pick_folder_windows(initial: Path | None) -> str | None:
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择成品保存目录'
$dialog.ShowNewFolderButton = $true
$initial = $env:MUSICKEY_BROWSE_INITIAL
if ($initial -and (Test-Path -LiteralPath $initial)) { $dialog.SelectedPath = $initial }
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.WindowState = 'Minimized'
$owner.Show()
$owner.Activate()
$result = $dialog.ShowDialog($owner)
$owner.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    [Console]::Out.WriteLine($dialog.SelectedPath)
}
"""
    env = os.environ.copy()
    env["MUSICKEY_BROWSE_INITIAL"] = str(initial) if initial else ""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
    )
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        detail = (proc.stderr or "").strip() or "文件夹选择框启动失败"
        raise OSError(detail)
    selected = (proc.stdout or "").strip()
    return selected or None


def _pick_folder_tk(initial: Path | None) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    selected = filedialog.askdirectory(initialdir=str(initial) if initial else None, parent=root)
    root.destroy()
    return selected or None


@dataclass(slots=True)
class Job:
    id: str
    name: str
    source_path: Path
    work_dir: Path
    output_dir: Path
    target: str | None
    embed_cover: bool
    ekey: str | None
    ekey_db: Path | None = None
    status: str = "queued"
    progress: int = 0
    phase: str = "等待中"
    stage: str = "queued"
    result_path: Path | None = None
    container: str = ""
    size: int = 0
    tags: dict[str, str] = field(default_factory=dict)
    has_cover: bool = False
    error: str = ""
    source: str = ""
    created: float = field(default_factory=time.time)
    started_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    worker: str = ""
    abandoned: bool = False
    log_bucket: int = -1
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def queue_wait(self) -> float | None:
        if self.started_at is None:
            return None
        return max(0.0, self.started_at - self.created)

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        start = self.started_at if self.started_at is not None else self.created
        return max(0.0, end - start)

    @property
    def output_name(self) -> str:
        stem = _safe_stem(Path(self.name).stem)
        ext = self.target or OUTPUT_EXTENSIONS.get(self.container, "bin")
        return f"{stem}.{ext}"

    @property
    def saved_name(self) -> str:
        if self.result_path is not None:
            return self.result_path.name
        return self.output_name


class JobManager:
    def __init__(
        self,
        workers: int = 2,
        output_dir: Path | None = None,
        config_path: Path | None = None,
        lyric_dir: Path | None = None,
        watchdog_interval: float = 2.0,
        queue_timeout: float = 15.0,
        stall_timeout: float | dict[str, float] | None = None,
        enable_watchdog: bool = True,
        log_path: Path | None = None,
    ) -> None:
        self.workers = max(1, int(workers))
        self.config_path = default_config_path() if config_path is None else config_path
        chosen = output_dir if output_dir is not None else default_output_dir()
        self.output_dir = chosen.expanduser()
        if not self.output_dir.is_absolute():
            self.output_dir = (Path.cwd() / self.output_dir).resolve()
        else:
            self.output_dir = self.output_dir.resolve()
        self.lyric_dir = self._load_lyric_dir() if lyric_dir is None else self._resolve_lyric_dir(lyric_dir)
        self.log_path = Path(log_path) if log_path is not None else default_log_path()
        self.output_error: str | None = None
        self.watchdog_interval = max(0.01, float(watchdog_interval))
        self.queue_timeout = max(0.0, float(queue_timeout))
        self.enable_watchdog = enable_watchdog
        if stall_timeout is None:
            self.stall_timeouts = dict(DEFAULT_STAGE_TIMEOUTS)
        elif isinstance(stall_timeout, dict):
            self.stall_timeouts = {**DEFAULT_STAGE_TIMEOUTS, **stall_timeout}
        else:
            value = max(0.0, float(stall_timeout))
            self.stall_timeouts = {stage: value for stage in DEFAULT_STAGE_TIMEOUTS}

        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._threads: list[threading.Thread] = []
        self._worker_state: dict[str, dict] = {}
        self._retired: set[str] = set()
        self._pending: list[str] = []
        self._queue_notice: dict[str, float] = {}
        self._worker_seq = 0
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self._start_time = time.time()

    @staticmethod
    def _resolve_lyric_dir(path: Path | None) -> Path | None:
        if path is None:
            return None
        raw = str(path).strip()
        if not raw:
            return None
        resolved = Path(raw).expanduser()
        if not resolved.is_absolute():
            resolved = Path.cwd() / resolved
        resolved = resolved.resolve()
        if not resolved.is_dir():
            raise ValueError("歌词目录不存在")
        return resolved

    def _load_lyric_dir(self) -> Path | None:
        if not self.config_path.is_file():
            return None
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            raw = data.get("lyric_dir")
            if not isinstance(raw, str) or not raw.strip():
                return None
            return self._resolve_lyric_dir(Path(raw))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def set_output_dir(self, path: Path) -> Path:
        try:
            resolved = validate_output_dir(path)
        except ValueError as exc:
            with self._lock:
                self.output_error = str(exc)
            raise
        with self._lock:
            self.output_dir = resolved
            self.output_error = None
        self._persist()
        logger.info("成品目录已更新: %s", resolved)
        return resolved

    def set_lyric_dir(self, path: Path | None) -> Path | None:
        resolved = self._resolve_lyric_dir(path)
        with self._lock:
            self.lyric_dir = resolved
        self._persist()
        logger.info("歌词目录已更新: %s", resolved or "（未设置）")
        return resolved

    def _persist(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "output_dir": str(self.output_dir),
            "lyric_dir": "" if self.lyric_dir is None else str(self.lyric_dir),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self.config_path.write_text(text, encoding="utf-8")

    def _copy_lyrics(self, job: Job) -> None:
        """按歌曲原名，把歌词目录里的同名歌词复制到成品目录。"""
        with self._lock:
            root = self.lyric_dir
            output_dir = job.output_dir
        if root is None or not root.is_dir():
            return
        stem = _safe_stem(Path(job.name).stem)
        lookup_stems = [Path(job.name).stem]
        if stem not in lookup_stems:
            lookup_stems.append(stem)
        output_dir.mkdir(parents=True, exist_ok=True)
        for ext in LYRIC_EXTENSIONS:
            source = None
            for name in lookup_stems:
                candidate = root / f"{name}{ext}"
                if candidate.is_file():
                    source = candidate
                    break
            if source is None:
                continue
            dest = output_dir / f"{stem}{ext}"
            if source.resolve() == dest.resolve():
                continue
            try:
                shutil.copyfile(source, dest)
            except OSError:
                continue

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        for _ in range(self.workers):
            self._spawn_worker()
        if self.enable_watchdog:
            self._watchdog = threading.Thread(
                target=self._watchdog_loop,
                name="musickey-watchdog",
                daemon=True,
            )
            self._watchdog.start()
        logger.info(
            "任务管理器启动: workers=%d, 成品目录=%s, 日志=%s, 看门狗=%s",
            self.workers,
            self.output_dir,
            self.log_path,
            "开" if self.enable_watchdog else "关",
        )

    def _spawn_worker(self) -> None:
        with self._lock:
            self._worker_seq += 1
            name = f"musickey-worker-{self._worker_seq}"
            self._worker_state[name] = {"busy": False, "job_id": None, "tick": time.time()}
        thread = threading.Thread(target=self._worker_loop, name=name, daemon=True)
        thread.start()
        with self._lock:
            self._threads.append(thread)
        logger.info("worker 线程已启动: %s", name)

    def _touch_worker(self, name: str, *, busy: bool, job_id: str | None) -> None:
        with self._lock:
            state = self._worker_state.setdefault(name, {})
            state["busy"] = busy
            state["job_id"] = job_id
            state["tick"] = time.time()

    def _retire_worker(self, name: str) -> None:
        if not name:
            return
        with self._lock:
            self._retired.add(name)

    def _is_retired(self, name: str) -> bool:
        with self._lock:
            return name in self._retired

    def _worker_loop(self) -> None:
        name = threading.current_thread().name
        while not self._stop.is_set():
            if self._is_retired(name):
                with self._lock:
                    self._retired.discard(name)
                logger.warning("worker %s 已被替换，退出", name)
                return
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._touch_worker(name, busy=False, job_id=None)
                continue
            if job_id is None:
                self._queue.task_done()
                return
            self._touch_worker(name, busy=True, job_id=job_id)
            self._mark_dequeued(job_id)
            try:
                self._process(job_id)
            except Exception as exc:
                logger.exception("worker %s 处理任务 %s 时未捕获异常", name, job_id)
                self._fail(job_id, f"{type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()
                self._touch_worker(name, busy=False, job_id=None)

    def shutdown(self, timeout: float = 2.0) -> None:
        """停止 worker 与看门狗（主要用于测试与正常退出）。"""
        self._stop.set()
        with self._lock:
            threads = list(self._threads)
        for _ in threads:
            self._queue.put(None)
        deadline = time.time() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        if self._watchdog is not None:
            self._watchdog.join(timeout=max(0.0, deadline - time.time()))

    def allocate(
        self,
        original_name: str,
        target: str | None,
        embed_cover: bool,
        ekey: str | None,
    ) -> Job:
        """创建唯一工作目录并登记任务，此时尚未入队。"""
        self.start()
        job_id = uuid.uuid4().hex
        with self._lock:
            output_dir = self.output_dir
        work_dir = output_dir / WORK_DIR_NAME / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(original_name).suffix
        job = Job(
            id=job_id,
            name=Path(original_name).name,
            source_path=work_dir / f"source{suffix}",
            work_dir=work_dir,
            output_dir=output_dir,
            target=target,
            embed_cover=embed_cover,
            ekey=ekey,
        )
        with self._lock:
            self._jobs[job_id] = job
        logger.info(
            "任务已创建: id=%s name=%s target=%s 成品目录=%s 工作目录=%s",
            job_id,
            job.name,
            target or "保持原样",
            output_dir,
            work_dir,
        )
        self._cleanup()
        return job

    def enqueue(self, job_id: str) -> None:
        with self._lock:
            self._pending.append(job_id)
            position = len(self._pending)
        self._queue.put(job_id)
        logger.info("任务已入队: %s（队列 %d）", job_id, position)

    def _mark_dequeued(self, job_id: str) -> None:
        with self._lock:
            try:
                self._pending.remove(job_id)
            except ValueError:
                pass

    def queue_position(self, job_id: str) -> int | None:
        with self._lock:
            try:
                return self._pending.index(job_id) + 1
            except ValueError:
                return None

    def discard(self, job_id: str) -> None:
        job = self.get(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
            try:
                self._pending.remove(job_id)
            except ValueError:
                pass
        if job is not None:
            self._delete_work_dir(job)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._jobs)

    def _publish(self, job: Job, source: Path, ext: str | None = None) -> Path:
        started = time.perf_counter()
        job.output_dir.mkdir(parents=True, exist_ok=True)
        ext = ext or job.target or OUTPUT_EXTENSIONS.get(job.container, "bin")
        stem = _safe_stem(Path(job.name).stem)
        with self._lock:
            dest = _unique_path(job.output_dir, stem, ext, True)
        if dest is None:
            raise MusicKeyError("无法写入成品目录")
        # 文件系统调用可能阻塞（掉线的移动盘/网络盘），不要持有全局锁。
        os.replace(source, dest)
        logger.info(
            "成品已落盘: %s -> %s（%.2fs）",
            source.name,
            dest,
            time.perf_counter() - started,
        )
        return dest

    def _process(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            return
        if job.abandoned:
            logger.warning("跳过已被看门狗放弃的任务: %s", job.name)
            self._delete_work_dir(job)
            return
        if job.cancel_event.is_set():
            self._finish_cancelled(job)
            return

        now = time.time()
        job.status = "running"
        job.stage = "prepare"
        job.phase = "准备中"
        job.started_at = now
        job.updated_at = now
        job.worker = threading.current_thread().name
        job.log_bucket = -1
        self._update_job(job)
        logger.info(
            "任务开始处理: %s（排队 %.2fs，worker=%s）",
            job.name,
            job.queue_wait or 0.0,
            job.worker,
        )
        try:

            def callback(done: int, total: int, phase: str) -> None:
                if job.abandoned:
                    raise JobCancelledError("任务已被看门狗放弃")
                percent = int(done / max(1, total) * 100)
                job.progress = percent
                if phase == "decrypt":
                    job.stage = "decode"
                    job.phase = "解密中"
                else:
                    job.phase = phase
                job.updated_at = time.time()
                bucket = percent // 25
                if bucket != job.log_bucket:
                    job.log_bucket = bucket
                    logger.info(
                        "解密进度: %s %d%%（输入 %.1f MB）",
                        job.name,
                        percent,
                        max(1, total) / (1024 * 1024),
                    )
                self._update_job(job)

            def on_stage(stage: str) -> None:
                if job.abandoned:
                    raise JobCancelledError("任务已被看门狗放弃")
                labels = {
                    "decode": "解密中",
                    "transcode": "转码中",
                    "embed": "写入标签",
                    "publish": "写入成品目录",
                }
                job.stage = stage
                job.phase = labels.get(stage, stage)
                job.updated_at = time.time()
                self._update_job(job)
                logger.info("任务进入阶段: %s -> %s", job.name, stage)

            lyric_ext = Path(job.name).suffix.lower()
            if lyric_ext in LYRIC_EXTENSIONS:
                on_stage("publish")
                dest = self._publish(job, job.source_path, lyric_ext.lstrip("."))
                if job.abandoned:
                    logger.warning("任务已被看门狗放弃，忽略迟到结果: %s", job.name)
                    return
                job.result_path = dest
                job.container = lyric_ext.lstrip(".")
                job.size = dest.stat().st_size if dest.exists() else 0
                job.status = "done"
                job.progress = 100
                job.stage = "done"
                job.phase = "已写入成品目录"
                return

            opts = BatchOptions(
                output_dir=job.work_dir,
                target=job.target,
                embed_cover=job.embed_cover,
                force=True,
                decode=DecodeOptions(ekey=job.ekey, ekey_db=job.ekey_db),
                jobs=1,
            )
            outcome = process_one(
                job.source_path,
                opts,
                callback,
                job.cancel_event,
                on_stage=on_stage,
            )
            if job.abandoned:
                logger.warning("任务已被看门狗放弃，忽略迟到结果: %s", job.name)
                return
            job.source = outcome.source
            job.container = outcome.container
            if outcome.status == "cancelled":
                job.status = "cancelled"
                job.error = outcome.note
            elif outcome.status != "ok" or outcome.output is None:
                job.status = "failed"
                job.error = outcome.note or "解码失败"
            else:
                final_container = outcome.output.suffix.lstrip(".") or outcome.container
                job.container = final_container
                tags, cover = read_metadata(outcome.output, final_container)
                job.tags = tags
                job.has_cover = cover is not None
                on_stage("publish")
                dest = self._publish(job, outcome.output)
                if job.abandoned:
                    logger.warning("任务已被看门狗放弃，忽略迟到结果: %s", job.name)
                    return
                self._copy_lyrics(job)
                job.result_path = dest
                job.size = dest.stat().st_size if dest.exists() else 0
                job.status = "done"
                job.progress = 100
                job.stage = "done"
                job.phase = "已写入成品目录"
        except JobCancelledError as exc:
            if not job.abandoned:
                job.status = "cancelled"
                job.error = str(exc) or "任务已取消"
        except Exception as exc:
            logger.exception("任务处理失败: %s", job.name)
            if not job.abandoned:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._delete_work_dir(job)
            if job.abandoned:
                logger.error(
                    "任务已判定失败: %s（阶段=%s，worker=%s）",
                    job.name,
                    job.stage,
                    job.worker,
                )
                self._update_job(job)
            elif job.cancel_event.is_set():
                with self._lock:
                    self._jobs.pop(job.id, None)
                self._queue_notice.pop(job.id, None)
                logger.info("任务已取消并从列表移除: %s", job.name)
            else:
                job.updated_at = time.time()
                if job.status in ("done", "failed", "cancelled"):
                    job.finished_at = job.updated_at
                self._update_job(job)
                logger.info(
                    "任务结束: %s status=%s 总耗时=%.2fs%s",
                    job.name,
                    job.status,
                    job.elapsed,
                    f" 错误={job.error}" if job.error else "",
                )

    def _finish_cancelled(self, job: Job) -> None:
        job.status = "cancelled"
        job.error = "用户取消了任务"
        job.finished_at = time.time()
        with self._lock:
            self._jobs.pop(job.id, None)
            try:
                self._pending.remove(job.id)
            except ValueError:
                pass
        self._queue_notice.pop(job.id, None)
        self._delete_work_dir(job)
        logger.info("排队中的任务已取消: %s", job.name)

    def _fail(self, job_id: str, message: str) -> None:
        job = self.get(job_id)
        if job:
            job.status = "failed"
            job.error = message
            job.stage = "failed"
            job.finished_at = time.time()
            job.updated_at = job.finished_at
            self._delete_work_dir(job)
            self._update_job(job)
            logger.error("任务失败: %s: %s", job.name, message)

    def _update_job(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def _delete_work_dir(self, job: Job) -> None:
        work = job.work_dir
        if work.name != job.id or work.parent.name != WORK_DIR_NAME:
            return
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            logger.warning("工作目录清理失败（可能仍被占用）: %s", work)
        try:
            work.parent.rmdir()
        except OSError:
            pass

    def remove(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status in ("queued", "running"):
            job.cancel_event.set()
            return True
        with self._lock:
            self._jobs.pop(job_id, None)
            try:
                self._pending.remove(job_id)
            except ValueError:
                pass
        self._queue_notice.pop(job_id, None)
        self._delete_work_dir(job)
        logger.info("任务已移除: %s", job.name)
        return True

    def clear(self) -> int:
        with self._lock:
            ids = list(self._jobs)
        count = 0
        for job_id in ids:
            job = self.get(job_id)
            if job and job.status in ("queued", "running"):
                job.cancel_event.set()
            elif job and self.remove(job_id):
                count += 1
        return count

    def _cleanup(self, ttl: int = 24 * 3600) -> None:
        cutoff = time.time() - ttl
        stale: list[Job] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.created < cutoff and job.status not in ("running", "queued"):
                    self._jobs.pop(job_id, None)
                    stale.append(job)
        for job in stale:
            self._delete_work_dir(job)

    # ------------------------------------------------------------------
    # 看门狗与可观测性
    # ------------------------------------------------------------------

    def _stage_timeout(self, stage: str) -> float:
        return float(self.stall_timeouts.get(stage, self.stall_timeouts.get("prepare", 60.0)))

    def _active_worker_threads(self) -> list[threading.Thread]:
        with self._lock:
            threads = list(self._threads)
            retired = set(self._retired)
        return [thread for thread in threads if thread.name not in retired]

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(self.watchdog_interval):
            try:
                self._watchdog_tick()
            except Exception:
                logger.exception("看门狗检查异常")

    def _watchdog_tick(self) -> None:
        now = time.time()
        active = self._active_worker_threads()
        alive = [thread for thread in active if thread.is_alive()]

        if self._started and not self._stop.is_set():
            missing = self.workers - len(alive)
            for _ in range(max(0, missing)):
                logger.error(
                    "worker 数量不足（存活 %d/%d），补起新线程",
                    len(alive),
                    self.workers,
                )
                self._spawn_worker()
            if missing > 0:
                active = self._active_worker_threads()
                alive = [thread for thread in active if thread.is_alive()]

        with self._lock:
            jobs = list(self._jobs.values())
            worker_states = {name: dict(state) for name, state in self._worker_state.items()}
            pending = list(self._pending)

        abandoned_this_tick = False
        for job in jobs:
            if job.status != "running" or job.abandoned:
                continue
            timeout = self._stage_timeout(job.stage)
            idle = now - job.updated_at
            if idle > timeout:
                self._abandon(
                    job,
                    f"「{job.stage}」阶段已 {idle:.0f}s 无进展"
                    f"（worker={job.worker or '未知'}），线程堆栈已写入日志",
                )
                abandoned_this_tick = True

        if not jobs:
            return

        # 放弃任务后 worker 会被替换，重新取样，避免用旧状态误判排队任务。
        if abandoned_this_tick:
            active = self._active_worker_threads()
            alive = [thread for thread in active if thread.is_alive()]
            with self._lock:
                worker_states = {
                    name: dict(state) for name, state in self._worker_state.items()
                }
                pending = list(self._pending)

        alive_names = {thread.name for thread in alive}
        productive = False
        for name in alive_names:
            state = worker_states.get(name) or {}
            if not state.get("busy"):
                continue
            current = next(
                (item for item in jobs if item.id == state.get("job_id")),
                None,
            )
            if (
                current is not None
                and not current.abandoned
                and current.status in ("queued", "running")
            ):
                productive = True
                break

        for job in jobs:
            if job.status != "queued" or job.abandoned:
                continue
            waited = now - job.created
            if not alive:
                self._abandon(
                    job,
                    "没有存活的 worker 线程，任务无法开始（已写入日志）",
                    running=False,
                )
            elif waited > self.queue_timeout and not productive:
                self._abandon(
                    job,
                    f"已排队 {waited:.1f}s 仍未被处理：worker 空闲或卡在已放弃的任务上"
                    f"（队列 {len(pending)}，存活 worker {len(alive)}）",
                    running=False,
                )
            elif waited >= QUEUE_NOTICE_SECONDS:
                last = self._queue_notice.get(job.id, 0.0)
                if now - last >= QUEUE_NOTICE_SECONDS:
                    self._queue_notice[job.id] = now
                    logger.warning(
                        "任务 %s 已排队 %.0fs（队列 %d，存活 worker %d）",
                        job.name,
                        waited,
                        len(pending),
                        len(alive),
                    )

    def _abandon(self, job: Job, message: str, *, running: bool = True) -> None:
        if job.abandoned:
            return
        job.abandoned = True
        job.status = "failed"
        job.error = message
        job.phase = "已判定卡死"
        job.finished_at = time.time()
        job.updated_at = job.finished_at
        job.cancel_event.set()
        with self._lock:
            self._jobs[job.id] = job
            try:
                self._pending.remove(job.id)
            except ValueError:
                pass
        self._queue_notice.pop(job.id, None)
        logger.error("看门狗放弃任务 %s: %s", job.name, message)
        if running:
            self._retire_worker(job.worker)
            self._spawn_worker()
            logger.error(
                "当前线程堆栈（任务 %s，worker %s）:\n%s",
                job.name,
                job.worker or "未知",
                _format_thread_stacks(),
            )

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            jobs = list(self._jobs.values())
            worker_states = {name: dict(state) for name, state in self._worker_state.items()}
            threads = list(self._threads)
            retired = set(self._retired)
            pending = len(self._pending)
            output_dir = self.output_dir
            output_error = self.output_error
            lyric_dir = self.lyric_dir

        active = [thread for thread in threads if thread.name not in retired]
        alive = [thread for thread in active if thread.is_alive()]
        busy = sum(1 for thread in alive if worker_states.get(thread.name, {}).get("busy"))
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
        for job in jobs:
            if job.status in counts:
                counts[job.status] += 1
        return {
            "workers": {"configured": self.workers, "alive": len(alive), "busy": busy},
            "queue": {"size": pending},
            "jobs": counts,
            "output_dir": str(output_dir),
            "output_ok": output_error is None,
            "output_error": output_error,
            "lyric_dir": "" if lyric_dir is None else str(lyric_dir),
            "log_path": str(self.log_path),
            "uptime_seconds": round(now - self._start_time, 1),
        }


def _format_thread_stacks() -> str:
    lines: list[str] = []
    frames = sys._current_frames()
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        lines.append(f"--- {thread.name} (alive={thread.is_alive()}) ---")
        if frame is not None:
            lines.extend(line.rstrip("\n") for line in traceback.format_stack(frame))
    return "\n".join(lines) if lines else "（无可用堆栈）"


_manager = JobManager(workers=2)


def _static_dir() -> Path:
    return bundled_path("webui")


def _job_payload(job: Job, queue_position: int | None = None) -> dict:
    return {
        "id": job.id,
        "name": job.name,
        "status": job.status,
        "progress": job.progress,
        "phase": job.phase,
        "stage": job.stage,
        "worker": job.worker,
        "output_name": job.output_name,
        "saved_name": job.saved_name,
        "output_dir": str(job.output_dir),
        "container": job.container,
        "size": job.size,
        "tags": job.tags,
        "has_cover": job.has_cover,
        "error": job.error,
        "source": job.source,
        "created_at": job.created,
        "started_at": job.started_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
        "queue_wait": None if job.queue_wait is None else round(job.queue_wait, 2),
        "elapsed": round(job.elapsed, 2),
        "queue_position": queue_position,
        "abandoned": job.abandoned,
    }


def create_app(manager: JobManager | None = None) -> Flask:
    global _manager
    if manager is not None:
        _manager = manager
    app = Flask(__name__, static_folder=str(_static_dir()), static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=2 * 1024 * 1024 * 1024, SEND_FILE_MAX_AGE_DEFAULT=0)

    @app.after_request
    def _disable_asset_cache(response):
        if request.path == "/" or request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index():
        html = (_static_dir() / "index.html").read_text(encoding="utf-8")
        for name in ("app.js", "styles.css"):
            stamp = int((_static_dir() / name).stat().st_mtime)
            html = html.replace(f"/static/{name}", f"/static/{name}?v={stamp}")
        response = app.response_class(html, mimetype="text/html; charset=utf-8")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/health")
    def health():
        payload = {
            "ok": True,
            "version": __version__,
            "formats": sorted(supported_extensions()),
            "targets": list(TARGETS),
            "ffmpeg": resolve_ffmpeg() is not None,
        }
        payload.update(_manager.snapshot())
        return jsonify(payload)

    @app.get("/api/settings")
    def get_settings():
        return jsonify(
            {
                "ok": True,
                "output_dir": str(_manager.output_dir),
                "lyric_dir": "" if _manager.lyric_dir is None else str(_manager.lyric_dir),
            }
        )

    @app.put("/api/settings")
    def put_settings():
        payload = request.get_json(silent=True) or {}
        if "output_dir" not in payload and "lyric_dir" not in payload:
            return jsonify({"ok": False, "error": "没有要保存的设置"}), 400
        try:
            if "output_dir" in payload:
                raw = str(payload.get("output_dir") or "").strip()
                if not raw:
                    return jsonify({"ok": False, "error": "请填写成品目录"}), 400
                _manager.set_output_dir(Path(raw))
            if "lyric_dir" in payload:
                raw = str(payload.get("lyric_dir") or "").strip()
                _manager.set_lyric_dir(Path(raw) if raw else None)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "output_dir": str(_manager.output_dir),
                "lyric_dir": "" if _manager.lyric_dir is None else str(_manager.lyric_dir),
            }
        )

    @app.post("/api/browse-folder")
    def browse_folder():
        try:
            selected = pick_folder(_manager.output_dir)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"无法打开文件夹选择框: {exc}"}), 500
        if not selected:
            return jsonify({"ok": False, "error": "已取消"}), 400
        return jsonify({"ok": True, "output_dir": selected})

    @app.post("/api/reveal")
    def reveal():
        try:
            open_folder(_manager.output_dir)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"无法打开目录: {exc}"}), 500
        return jsonify({"ok": True, "output_dir": str(_manager.output_dir)})

    @app.post("/api/jobs")
    def create_job():
        if not request.files:
            return jsonify({"ok": False, "error": "缺少文件"}), 400
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"ok": False, "error": "缺少文件"}), 400
        target = (request.form.get("format") or "").strip() or None
        if target and target not in TARGETS:
            return jsonify({"ok": False, "error": "不支持的输出格式"}), 400
        embed_cover = request.form.get("embed_cover", "1") != "0"
        ekey = (request.form.get("ekey") or "").strip() or None

        job = _manager.allocate(upload.filename, target, embed_cover, ekey)
        try:
            upload.save(job.source_path)
            db_upload = request.files.get("ekey_db")
            if db_upload and db_upload.filename:
                ekey_db = job.work_dir / "player_process_db"
                db_upload.save(ekey_db)
                job.ekey_db = ekey_db
        except Exception as exc:
            _manager.discard(job.id)
            return jsonify({"ok": False, "error": f"保存上传文件失败: {exc}"}), 400
        _manager.enqueue(job.id)
        return jsonify({"ok": True, "id": job.id}), 202

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        job = _manager.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify(
            {
                "ok": True,
                "job": _job_payload(job, _manager.queue_position(job_id)),
            }
        )

    @app.delete("/api/jobs/<job_id>")
    def cancel_job(job_id: str):
        if _manager.remove(job_id):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "任务不存在"}), 404

    @app.get("/api/download/<job_id>")
    def download(job_id: str):
        job = _manager.get(job_id)
        if not job or not job.result_path:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if not job.result_path.exists():
            return jsonify({"ok": False, "error": "结果已过期"}), 410
        return send_file(
            job.result_path,
            as_attachment=True,
            download_name=job.saved_name,
            conditional=True,
        )

    @app.get("/api/audio/<job_id>")
    def audio(job_id: str):
        job = _manager.get(job_id)
        if not job or not job.result_path or not job.result_path.exists():
            return jsonify({"ok": False, "error": "音频不存在"}), 404
        mime = {
            "mp3": "audio/mpeg",
            "m4a": "audio/mp4",
            "flac": "audio/flac",
            "ogg": "audio/ogg",
            "wav": "audio/wav",
        }.get(job.container, "application/octet-stream")
        return send_file(job.result_path, mimetype=mime, conditional=True)

    @app.get("/api/cover/<job_id>")
    def cover(job_id: str):
        job = _manager.get(job_id)
        if not job or not job.result_path or not job.result_path.exists():
            return jsonify({"ok": False, "error": "无封面"}), 404
        _, data = read_metadata(job.result_path, job.container or job.target or "mp3")
        if not data:
            return jsonify({"ok": False, "error": "无封面"}), 404
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return send_file(io.BytesIO(data), mimetype=mime)

    @app.post("/api/clear")
    def clear():
        count = _manager.clear()
        return jsonify({"ok": True, "removed": count})

    @app.post("/api/shutdown")
    def shutdown():
        def _stop():
            time.sleep(0.25)
            os._exit(0)

        threading.Thread(target=_stop, daemon=True).start()
        return jsonify({"ok": True, "message": "正在退出"})

    return app


def _say(text: str) -> None:
    """窗口模式下 stdout 可能为 None，避免 print 直接抛异常。"""
    if sys.stdout is None:
        return
    try:
        print(text)
    except Exception:
        pass


def run(
    host: str = "127.0.0.1",
    port: int = 8690,
    workers: int = 2,
    output_dir: Path | None = None,
) -> None:
    chosen = resolve_startup_output(output_dir)
    log_path = setup_logging()
    manager = JobManager(workers=workers, output_dir=chosen, log_path=log_path)
    manager.start()
    app = create_app(manager)
    logger.info(
        "%s v%s 启动: http://%s:%s，成品目录=%s，日志=%s",
        APP_NAME,
        __version__,
        host,
        port,
        chosen,
        log_path,
    )
    _say(f"{APP_NAME} v{__version__} 已启动: http://{host}:{port}")
    _say(f"成品目录: {chosen}")
    _say(f"日志文件: {log_path}")
    _say("仅监听本机；文件不会离开你的电脑。按 Ctrl+C 退出。")
    app.run(host=host, port=port, debug=False, threaded=True)
