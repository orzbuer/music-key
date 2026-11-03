"""统一日志配置：文件轮转、可选的调试级别。

日志默认写入用户目录，避免成品目录被用户改动或清理后找不到。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "musickey"
DEFAULT_LOG_DIR = Path.home() / ".musickey" / "logs"
LOG_FILE_NAME = "web.log"

_configured_path: Path | None = None


def default_log_path() -> Path:
    override = os.environ.get("MUSICKEY_LOG_DIR")
    base = Path(override).expanduser() if override else DEFAULT_LOG_DIR
    return base / LOG_FILE_NAME


def setup_logging(log_path: Path | None = None) -> Path:
    """配置 ``musickey`` 日志树，返回实际使用的日志文件路径。"""
    global _configured_path
    path = Path(log_path).expanduser() if log_path is not None else default_log_path()
    if _configured_path == path:
        return path

    level = logging.DEBUG if os.environ.get("MUSICKEY_DEBUG") else logging.INFO
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
            )
        )
        logger.addHandler(file_handler)
    except OSError:
        # 日志目录不可写时不要影响主流程，退回控制台输出。
        pass

    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(console)

    _configured_path = path
    return path


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)
