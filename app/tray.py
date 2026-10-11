"""Windows 托盘模式：后台运行录制服务，托盘图标提供快捷操作。

用法：双击 run_windows_tray.bat，或在项目根目录执行 ``pythonw -m app.tray``。

托盘菜单：
- 打开网页界面：浏览器打开管理页
- 暂停/恢复监控：暂停只停止"自动开新录"，正在录制的分段不受影响
- 刷新直播间：立即对所有监控房间做一轮开播检测（不等轮询间隔）
- 退出程序：优雅停止服务（在录会话收尾落盘）后退出

pystray / Pillow 仅在 Windows 安装（requirements.txt 带平台标记），
因此 pystray 采用延迟导入，非 Windows 平台 import 本模块不报错。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

HOST = "0.0.0.0"
PORT = int(os.environ.get("BLR_PORT", "8000"))
BASE = f"http://127.0.0.1:{PORT}"

_server = None
_server_thread = None


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _api(path: str, method: str = "POST") -> dict:
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=8) as r:
        body = r.read()
    return json.loads(body) if body else {}


def _start_server() -> None:
    global _server, _server_thread
    import uvicorn

    from .main import app
    cfg = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    _server = uvicorn.Server(cfg)
    _server_thread = threading.Thread(target=_server.run, daemon=True)
    _server_thread.start()


def _wait_ready(timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _port_open():
            return True
        time.sleep(0.2)
    return False


def _paused() -> bool:
    try:
        return bool(_api("/api/status", "GET").get("paused"))
    except Exception:
        return False


def _make_image():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 61, 61), radius=14, fill=(247, 119, 155, 255))
    try:
        font = ImageFont.load_default(size=36)
    except TypeError:  # 旧版 Pillow 无 size 参数
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), "B", font=font)
    d.text(((64 - bbox[2] + bbox[0]) / 2 - bbox[0],
            (64 - bbox[3] + bbox[1]) / 2 - bbox[1]),
           "B", fill=(255, 255, 255, 255), font=font)
    return img


def _msg(title: str, text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        print(f"{title}: {text}")


def build_menu():
    from pystray import Menu, MenuItem as Item

    def open_page(icon, _):
        webbrowser.open(BASE)

    def toggle_monitor(icon, _):
        try:
            _api("/api/monitor/resume" if _paused() else "/api/monitor/pause")
        except Exception as e:
            logging.getLogger("tray").warning("toggle monitor failed: %s", e)
        icon.update_menu()

    def refresh_rooms(icon, _):
        try:
            _api("/api/refresh")
        except Exception as e:
            logging.getLogger("tray").warning("refresh failed: %s", e)

    def quit_app(icon, _):
        icon.stop()

    return Menu(
        Item("打开网页界面", open_page, default=True),
        Item(lambda _item: "恢复监控" if _paused() else "暂停监控",
             toggle_monitor, checked=lambda _item: _paused()),
        Item("刷新直播间", refresh_rooms),
        Menu.SEPARATOR,
        Item("退出程序", quit_app),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    log = logging.getLogger("tray")
    if sys.platform != "win32":
        print("托盘模式仅支持 Windows；本平台请直接运行 "
              "python -m uvicorn app.main:app")
        return 1
    if _port_open():
        # 服务已在运行（控制台或另一托盘实例）：不重复启动，直接打开界面
        webbrowser.open(BASE)
        log.info("service already running at %s, opened page", BASE)
        return 0
    _start_server()
    if not _wait_ready():
        if _server is not None:
            _server.should_exit = True
        _msg("启动失败", f"无法监听端口 {PORT}，可能被其他程序占用。")
        return 1
    import pystray
    icon = pystray.Icon("bililive", _make_image(),
                        f"B站直播录制系统（{BASE}）", build_menu())
    log.info("tray started, service at %s", BASE)
    try:
        icon.run()  # 阻塞直到菜单"退出程序"或注销
    finally:
        if _server is not None:
            _server.should_exit = True  # 触发 lifespan 收尾：在录会话优雅停录
            if _server_thread is not None:
                _server_thread.join(timeout=60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
