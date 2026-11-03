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
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from . import __version__
from .engine import BatchOptions, _safe_stem, _unique_path, process_one
from .errors import MusicKeyError
from .formats import supported_extensions
from .formats.base import DecodeOptions
from .model import OUTPUT_EXTENSIONS
from .resources import bundled_path
from .tags import read_metadata
from .transcode import TARGETS, resolve_ffmpeg

APP_NAME = "MusicKey"
WORK_DIR_NAME = ".musickey-work"
LYRIC_EXTENSIONS = {".lrc", ".krc", ".qrc"}


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
    result_path: Path | None = None
    container: str = ""
    size: int = 0
    tags: dict[str, str] = field(default_factory=dict)
    has_cover: bool = False
    error: str = ""
    source: str = ""
    created: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)

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
    ) -> None:
        self.workers = workers
        self.config_path = default_config_path() if config_path is None else config_path
        chosen = output_dir if output_dir is not None else default_output_dir()
        self.output_dir = chosen.expanduser()
        if not self.output_dir.is_absolute():
            self.output_dir = (Path.cwd() / self.output_dir).resolve()
        else:
            self.output_dir = self.output_dir.resolve()
        self.lyric_dir = self._load_lyric_dir() if lyric_dir is None else self._resolve_lyric_dir(lyric_dir)
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._threads: list[threading.Thread] = []
        self._started = False

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
        resolved = validate_output_dir(path)
        with self._lock:
            self.output_dir = resolved
        self._persist()
        return resolved

    def set_lyric_dir(self, path: Path | None) -> Path | None:
        resolved = self._resolve_lyric_dir(path)
        with self._lock:
            self.lyric_dir = resolved
        self._persist()
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
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"musickey-worker-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._process(job_id)
            except Exception as exc:
                self._fail(job_id, f"{type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()

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
        self._cleanup()
        return job

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    def discard(self, job_id: str) -> None:
        job = self.get(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        if job is not None:
            self._delete_work_dir(job)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._jobs)

    def _publish(self, job: Job, source: Path, ext: str | None = None) -> Path:
        job.output_dir.mkdir(parents=True, exist_ok=True)
        ext = ext or job.target or OUTPUT_EXTENSIONS.get(job.container, "bin")
        stem = _safe_stem(Path(job.name).stem)
        with self._lock:
            dest = _unique_path(job.output_dir, stem, ext, True)
            if dest is None:
                raise MusicKeyError("无法写入成品目录")
            os.replace(source, dest)
        return dest

    def _process(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            return
        if job.cancel_event.is_set():
            self._finish_cancelled(job)
            return
        job.status = "running"
        job.phase = "准备中"
        self._update_job(job)
        try:
            def callback(done: int, total: int, phase: str) -> None:
                percent = int(done / max(1, total) * 100)
                job.progress = percent
                job.phase = phase
                self._update_job(job)

            lyric_ext = Path(job.name).suffix.lower()
            if lyric_ext in LYRIC_EXTENSIONS:
                dest = self._publish(job, job.source_path, lyric_ext.lstrip("."))
                job.result_path = dest
                job.container = lyric_ext.lstrip(".")
                job.size = dest.stat().st_size if dest.exists() else 0
                job.status = "done"
                job.progress = 100
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
            outcome = process_one(job.source_path, opts, callback, job.cancel_event)
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
                dest = self._publish(job, outcome.output)
                self._copy_lyrics(job)
                job.result_path = dest
                job.size = dest.stat().st_size if dest.exists() else 0
                job.status = "done"
                job.progress = 100
                job.phase = "已写入成品目录"
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._delete_work_dir(job)
            if job.cancel_event.is_set():
                with self._lock:
                    self._jobs.pop(job.id, None)
            else:
                self._update_job(job)

    def _finish_cancelled(self, job: Job) -> None:
        job.status = "cancelled"
        job.error = "用户取消了任务"
        with self._lock:
            self._jobs.pop(job.id, None)
        self._delete_work_dir(job)

    def _fail(self, job_id: str, message: str) -> None:
        job = self.get(job_id)
        if job:
            job.status = "failed"
            job.error = message
            self._delete_work_dir(job)
            self._update_job(job)

    def _update_job(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def _delete_work_dir(self, job: Job) -> None:
        work = job.work_dir
        if work.name != job.id or work.parent.name != WORK_DIR_NAME:
            return
        shutil.rmtree(work, ignore_errors=True)
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
        self._delete_work_dir(job)
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
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.created < cutoff and job.status not in ("running", "queued"):
                    self._jobs.pop(job_id, None)
                    self._delete_work_dir(job)


_manager = JobManager(workers=2)


def _static_dir() -> Path:
    return bundled_path("webui")


def _job_payload(job: Job) -> dict:
    return {
        "id": job.id,
        "name": job.name,
        "status": job.status,
        "progress": job.progress,
        "phase": job.phase,
        "output_name": job.output_name,
        "saved_name": job.saved_name,
        "output_dir": str(job.output_dir),
        "container": job.container,
        "size": job.size,
        "tags": job.tags,
        "has_cover": job.has_cover,
        "error": job.error,
        "source": job.source,
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
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "formats": sorted(supported_extensions()),
                "targets": list(TARGETS),
                "ffmpeg": resolve_ffmpeg() is not None,
            }
        )

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
        return jsonify({"ok": True, "job": _job_payload(job)})

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


def run(
    host: str = "127.0.0.1",
    port: int = 8690,
    workers: int = 2,
    output_dir: Path | None = None,
) -> None:
    chosen = resolve_startup_output(output_dir)
    manager = JobManager(workers=workers, output_dir=chosen)
    app = create_app(manager)
    print(f"{APP_NAME} v{__version__} 已启动: http://{host}:{port}")
    print(f"成品目录: {chosen}")
    print("仅监听本机；文件不会离开你的电脑。按 Ctrl+C 退出。")
    app.run(host=host, port=port, debug=False, threaded=True)
