"""
日志文件 —— 直接写文件，完全绕过 Python logging 系统
不会被 uvicorn dictConfig 影响
"""
import atexit
import os
from datetime import datetime

LOG_FILE = None
_initialized = False


def _get_log_file():
    global LOG_FILE
    if LOG_FILE:
        return LOG_FILE
    log_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs",
    )
    os.makedirs(log_dir, exist_ok=True)
    LOG_FILE = os.path.join(log_dir, "app.log")
    return LOG_FILE


def log_info(name: str, message: str):
    """直接追加写日志文件"""
    filepath = _get_log_file()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} [INFO   ] {name} | {message}\n")
    except Exception:
        pass


def log_error(name: str, message: str):
    filepath = _get_log_file()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} [ERROR  ] {name} | {message}\n")
    except Exception:
        pass


def install_logger():
    """安装到 logging.getLogger('app') 使其同时写文件"""
    import logging
    
    filepath = _get_log_file()
    from logging.handlers import TimedRotatingFileHandler
    
    handler = TimedRotatingFileHandler(
        filepath,
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.setLevel(logging.DEBUG)

    # 加到 app 日志器（不依赖 root 传播链）
    app_logger = logging.getLogger("app")
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = True

    # 同时加到根日志器
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # 降低第三方日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    log_info("app", f"日志文件: {filepath}")

    return handler


atexit.register(lambda: log_info("app", "后端进程退出"))
