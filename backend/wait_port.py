"""等待端口释放"""
import socket, time
for i in range(120):
    try:
        s = socket.socket()
        s.bind(('0.0.0.0', 8000))
        s.close()
        print(f"Port 8000 free after {i+1}s")
        break
    except OSError:
        time.sleep(1)
else:
    print("Timeout waiting for port")
