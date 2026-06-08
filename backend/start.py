"""
启动脚本：用 SO_REUSEADDR 打开端口后启动 uvicorn
解决 Windows 僵尸端口 TIME_WAIT / DEAD LISTENING 问题
"""
import asyncio
import logging
import os
import socket
import sys

# 切到 backend 目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 从统一配置读取端口
from config import BACKEND_PORT as PORT, BACKEND_HOST as HOST

# 1. 先用 SO_REUSEADDR 绑定端口（Windows 允许绕过 TIME_WAIT）
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((HOST, PORT))
    print(f"[启动] 端口 {PORT} 已绑定 (SO_REUSEADDR)")
except OSError as e:
    print(f"[启动] 绑定端口失败: {e}")
    sys.exit(1)

# 2. 杀死旧进程（已不关键，因为端口已用 SO_REUSEADDR 绑定）
try:
    result = __import__("subprocess").run(
        ["netstat", "-ano"], capture_output=True, timeout=10
    )
    for line in result.stdout.decode("gbk", errors="replace").splitlines():
        if f":{PORT}" in line and "LISTENING" in line and str(os.getpid()) not in line:
            pid = line.strip().split()[-1]
            if pid != str(os.getpid()):
                __import__("subprocess").run(["taskkill", "/F", "/PID", pid], capture_output=True)
                print(f"[启动] 已清理旧 PID={pid}")
except Exception:
    pass

# 3. uvicorn 重用这个 socket
import uvicorn

config = uvicorn.Config(
    "main:app",
    host=HOST,
    port=PORT,
    log_level="info",
)
server = uvicorn.Server(config)
# 注入预绑定 socket
server.config.sockets = [sock]

if __name__ == "__main__":
    asyncio.run(server.serve(sockets=[sock]))
