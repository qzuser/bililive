"""统一日志配置：控制台 + data/logs/app.log 轮转文件。

Windows 下双击 bat 后控制台会关闭/滚动丢失，文件日志保证崩溃堆栈可查。
幂等：重复调用 setup() 不会叠加 handler。
"""
import logging
import logging.handlers
import sys
import threading

from . import config

_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def _reconfigure_streams() -> None:
    """Windows GBK 控制台下打印中文/emoji 会 UnicodeEncodeError，兜底替换。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _install_thread_hook() -> None:
    def hook(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("threading").error(
            "uncaught in thread %s: %s",
            getattr(args.thread, "name", "?"), args.exc_value,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = hook


def setup() -> None:
    _reconfigure_streams()
    root = logging.getLogger()
    if getattr(root, "_blr_configured", False):
        return
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FMT))
    root.addHandler(console)
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3,
            encoding="utf-8")
        fileh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fileh)
    except OSError as e:
        root.warning("日志文件不可用（%s），仅输出控制台", e)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    root._blr_configured = True  # type: ignore[attr-defined]
    _install_thread_hook()
