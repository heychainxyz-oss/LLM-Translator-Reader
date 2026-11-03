"""德语智读 · 独立桌面窗口（可选）。

正常启动（浏览器模式）：``python main.py``
桌面窗口模式：``python desktop.py``，需要额外安装 ``requirements-desktop.txt``。
未安装 pywebview 时会自动回退到默认浏览器，不影响阅读。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

import main as backend


def _wait_for_server(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1.5) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.35)
    return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def run() -> None:
    parser = argparse.ArgumentParser(description="德语智读桌面客户端")
    parser.add_argument("--port", type=int, default=int(os.environ.get("READER_PORT", "8000")))
    args = parser.parse_args()

    port = args.port
    backend.PORT = port
    url = f"http://127.0.0.1:{port}"
    if not _port_free(port):
        print(f"端口 {port} 已被占用，尝试直接连接现有服务…")
        if _wait_for_server(url, timeout=3):
            _open_window(url)
            return
        print(f"端口 {port} 被占用且不是本应用，请使用 --port 指定其他端口。")
        sys.exit(1)

    config = uvicorn.Config(
        backend.app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="reader-server", daemon=True)
    thread.start()

    if not _wait_for_server(url):
        print("服务启动超时，请查看上方日志。")
        server.should_exit = True
        sys.exit(1)

    try:
        _open_window(url)
    finally:
        server.should_exit = True
        thread.join(timeout=8)
        print("桌面窗口已关闭，服务已停止。")


def _open_window(url: str) -> None:
    try:
        import webview  # type: ignore
    except Exception as exc:  # pragma: no cover - 取决于可选依赖
        print(f"未启用独立窗口（{exc}）。已回退到浏览器：{url}")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return

    print(f"正在打开桌面窗口：{url}")
    webview.create_window(
        "德语智读 · German AI Reader",
        url,
        width=1460,
        height=940,
        min_size=(1000, 640),
        background_color="#f3ecdd",
    )
    webview.start()


if __name__ == "__main__":
    run()
