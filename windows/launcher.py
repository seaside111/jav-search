"""Windows desktop entry point for the frozen JAV Search application."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.request
import webbrowser


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from platform_paths import app_data_dir  # noqa: E402


MUTEX_NAME = "Local\\JAVSearchDesktop"
ERROR_ALREADY_EXISTS = 183


def _single_instance():
    if sys.platform != "win32":
        return None
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle or ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        raise SystemExit(0)
    return handle


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.15)
    raise RuntimeError(f"本地服务启动超时：{last_error}")


def _start_server(port: int):
    import uvicorn
    from main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info",
                            access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="jav-search-api", daemon=True)
    thread.start()
    return server, thread


def _run_browser(url: str, server, thread: threading.Thread) -> None:
    webbrowser.open(url)
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join(timeout=8)


def _run_webview(url: str, server, thread: threading.Thread) -> None:
    import webview

    window = webview.create_window("JAV Search", url, width=1280, height=820,
                                   min_size=(960, 640), confirm_close=False)
    try:
        webview.start(gui="edgechromium", private_mode=False,
                      storage_path=str(app_data_dir() / "webview"))
    finally:
        server.should_exit = True
        thread.join(timeout=8)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--browser", action="store_true")
    args, _unknown = parser.parse_known_args()
    mutex = _single_instance()
    data = app_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CONFIG_DIR", str(data))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    port = _free_port()
    os.environ["PORT"] = str(port)
    server, thread = _start_server(port)
    url = f"http://127.0.0.1:{port}"
    _wait_ready(url)
    try:
        if args.browser:
            _run_browser(url, server, thread)
        else:
            try:
                _run_webview(url, server, thread)
            except ImportError:
                _run_browser(url, server, thread)
    finally:
        if mutex and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

