"""音钥 MusicKey 命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from . import __version__
from .engine import BatchOptions, run_batch, summarize
from .formats import supported_summary
from .formats.base import DecodeOptions
from .formats.qmc import list_ekeys
from .transcode import TARGETS, resolve_ffmpeg
from .web import open_folder, run as run_web

BANNER = """音钥 MusicKey —— 全平台加密音乐格式转换工具
支持: 网易云 .ncm | QQ音乐 .mflac/.mgg/.qmc*/.tkm | 酷狗 .kgm/.kgma/.vpr | 酷我 .kwm
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="musickey",
        description=BANNER,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", type=Path, help="加密文件或目录（可多个）")
    parser.add_argument("-o", "--output", type=Path, default=Path("unlocked"), help="输出目录（默认 ./unlocked）")
    parser.add_argument("--format", choices=TARGETS, default=None, help="统一转码目标（默认保持原容器）")
    parser.add_argument("--embed-cover", action="store_true", default=True, help="嵌入标签与封面（默认）")
    parser.add_argument("--no-embed-cover", dest="embed_cover", action="store_false", help="不写标签/封面")
    parser.add_argument("--force", action="store_true", help="覆盖已存在输出")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="目录不递归")
    parser.add_argument("--jobs", type=int, default=1, help="并行任务数（默认 1）")
    parser.add_argument("--ekey", help="手动指定 QMC EKey")
    parser.add_argument("--ekey-db", type=Path, help="QQ 音乐安卓端 player_process_db")
    parser.add_argument("--kgm-key", type=Path, help="酷狗公钥 kugou_key.xz")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只列计划，不写文件")
    parser.add_argument("--list-formats", action="store_true", help="列出支持的格式后退出")
    parser.add_argument("--find-ffmpeg", action="store_true", help="查找 ffmpeg 后退出")
    parser.add_argument("--list-ekey-db", metavar="DB", type=Path, help="列出密钥库内容后退出")
    parser.add_argument("--find", help="与 --list-ekey-db 配合，按名字过滤")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--open", action="store_true", help="处理完成后打开输出目录")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出错误和汇总")
    parser.add_argument("--version", action="version", version=f"MusicKey {__version__}")
    return parser


def _run_batch(args: argparse.Namespace) -> int:
    if args.list_formats:
        for name, exts in supported_summary().items():
            print(f"{name}: {exts}")
        return 0
    if args.find_ffmpeg:
        path = resolve_ffmpeg(args.ffmpeg)
        print(path or "未找到 ffmpeg")
        return 0 if path else 2
    if args.list_ekey_db:
        try:
            rows = list_ekeys(args.list_ekey_db, args.find)
        except Exception as exc:
            print(f"读取密钥库失败: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            if not rows:
                print("（空）")
            for name, ekey in rows:
                print(f"{name}  ->  {ekey}")
        return 0
    if not args.inputs:
        build_parser().print_help()
        return 1
    if args.jobs < 1:
        print("--jobs 必须大于 0", file=sys.stderr)
        return 2

    decode_opts = DecodeOptions(ekey=args.ekey, ekey_db=args.ekey_db, kgm_key=args.kgm_key)
    opts = BatchOptions(
        output_dir=args.output,
        target=args.format,
        embed_cover=args.embed_cover,
        force=args.force,
        recursive=args.recursive,
        dry_run=args.dry_run,
        ffmpeg=args.ffmpeg,
        decode=decode_opts,
        jobs=args.jobs,
    )
    cancel_event = threading.Event()

    results = []

    def progress(done: int, total: int, phase: str) -> None:
        if args.quiet:
            return
        print(f"\r  {done * 100 // max(1, total):3d}%  {phase:8s}", end="", file=sys.stderr)

    def event(outcome) -> None:
        results.append(outcome)
        if args.quiet:
            if outcome.status == "failed":
                print(f"[FAIL] {outcome.input.name}: {outcome.note}", file=sys.stderr)
            return
        if not args.json:
            if outcome.status == "ok":
                print(f"\r[OK]   {outcome.input.name} -> {outcome.output}  ({outcome.note})")
            elif outcome.status == "skipped":
                print(f"\r[SKIP] {outcome.input.name}: {outcome.note}")
            elif outcome.status == "cancelled":
                print(f"\r[CANC] {outcome.input.name}: {outcome.note}")
            else:
                print(f"\r[FAIL] {outcome.input.name}: {outcome.note}", file=sys.stderr)

    run_batch(args.inputs, opts, on_event=event, progress=progress, cancel_event=cancel_event)
    counts = summarize(results)
    if args.json:
        print(
            json.dumps(
                {
                    "summary": counts,
                    "results": [
                        {
                            "input": str(item.input),
                            "output": str(item.output) if item.output else None,
                            "status": item.status,
                            "note": item.note,
                            "source": item.source,
                        }
                        for item in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if not args.quiet:
            print()
        print(
            f"完成：成功 {counts['ok']}，跳过 {counts['skipped']}，"
            f"失败 {counts['failed']}，取消 {counts['cancelled']}"
        )
    if args.open and opts.output_dir.exists():
        open_folder(opts.output_dir)
    return 0 if counts["failed"] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # 无参数默认启动本地网页版：双击即可使用，无需记住命令。
        argv = ["web", "--open"]
    if argv and argv[0] in {"web", "gui", "serve"}:
        parser = argparse.ArgumentParser(prog="musickey web", description="启动本地网页版")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8690)
        parser.add_argument("--workers", type=int, default=2)
        parser.add_argument("--output", type=Path, default=None, help="成品目录（默认 ./unlocked，或上次在网页中保存的路径）")
        parser.add_argument("--open", action="store_true", help="自动打开浏览器")
        parser.add_argument("--version", action="version", version=f"MusicKey {__version__}")
        args = parser.parse_args(argv[1:])

        import webbrowser

        if args.open:
            threading.Timer(0.8, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
        run_web(args.host, args.port, args.workers, output_dir=args.output)
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    return _run_batch(args)


if __name__ == "__main__":
    raise SystemExit(main())
