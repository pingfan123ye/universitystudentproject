"""杀掉占用 8000 端口的进程"""
import subprocess, time, socket
result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10, encoding="gbk")
for line in result.stdout.splitlines():
    if ":8000" in line and "LISTENING" in line:
        pid = line.strip().split()[-1]
        print(f"Killing PID {pid}")
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        time.sleep(2)
        break
# Verify port is free
for i in range(10):
    try:
        s = socket.socket()
        s.bind(("0.0.0.0", 8000))
        s.close()
        print(f"Port 8000 free after {i+1}s")
        break
    except OSError:
        time.sleep(1)
