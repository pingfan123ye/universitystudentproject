"""
全局日志 —— 直接写文件，完全绕过 Python logging 系统
不会被 uvicorn 任何配置影响
"""
import os
import threading
from datetime import datetime

_log_file = None
_lock = threading.Lock()


def _get_file():
    global _log_file
    if _log_file:
        return _log_file
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(d, exist_ok=True)
    _log_file = os.path.join(d, "app.log")
    return _log_file


def log(level: str, name: str, message: str):
    """线程安全地写日志到文件 + 终端输出"""
    fp = _get_file()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level:<7s}] {name} | {message}"
    print(line)
    with _lock:
        try:
            with open(fp, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def info(msg: str, *args, name: str = "app", **kwargs):
    """单参数兼容 logging.info(msg)，也可传 name= 改日志名"""
    log("INFO", name, str(msg))


def error(msg: str, *args, name: str = "app", **kwargs):
    log("ERROR", name, str(msg))


# 安装到 logging.getLogger("app") 也同时写文件（双保险）
def patch_logger():
    import logging
    app_logger = logging.getLogger("app")
    fp = _get_file()

    class FileHandler(logging.Handler):
        def emit(self, record):
            try:
                ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
                msg = self.format(record)
                with _lock, open(fp, "a", encoding="utf-8") as f:
                    f.write(f"{ts} [{record.levelname:<7s}] {record.name} | {msg}\n")
            except Exception:
                pass

    handler = FileHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = True

    # 同样加到根日志器
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # 降低第三方库
    for lib in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    info("app", f"日志已就绪: {fp}")
