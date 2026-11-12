"""
日志系统 — 控制台 + 文件双输出，支持按日期分文件。
修复 BUG-002: Windows GBK 控制台 emoji 编码错误。
"""

import logging
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


def _fix_windows_console_encoding():
    """修复 Windows 控制台编码问题，使其支持 Unicode (emoji 等)。

    在 Windows 上 PowerShell/cmd 默认使用 GBK 编码，
    导致 emoji 和其他 Unicode 字符输出时抛出 UnicodeEncodeError。
    """
    if sys.platform != "win32":
        return

    # 方法1: 尝试设置控制台为 UTF-8
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # 方法2: 设置环境变量
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 方法3: 如果是通过 subprocess 运行的，用这个
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleCP(65001)  # UTF-8
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass


def setup_logger(
    name: str = "linkedin_autoapply",
    level: str = "INFO",
    log_file: Optional[str] = None,
) -> logging.Logger:
    """配置并返回 logger 实例。

    Args:
        name: Logger 名称
        level: 日志级别 (DEBUG/INFO/WARN/ERROR)
        log_file: 日志文件路径，默认 logs/run_YYYY-MM-DD.log

    Returns:
        配置好的 Logger
    """
    _fix_windows_console_encoding()

    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 格式
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件 handler（UTF-8，无编码问题）
    if log_file is None:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(log_dir / f"run_{datetime.now().strftime('%Y-%m-%d')}.log")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # 控制台 handler — 强制 flush 每条日志
    class FlushStreamHandler(logging.StreamHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()

    try:
        console = FlushStreamHandler(sys.stdout)
    except Exception:
        console = FlushStreamHandler(sys.stderr)

    console.setLevel(logging.DEBUG)
    console.setFormatter(fmt)
    logger.addHandler(console)

    return logger
