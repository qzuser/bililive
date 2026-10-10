import asyncio
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional


def ffmpeg_bin() -> str:
    """ffmpeg 路径解析顺序：BLR_FFMPEG 环境变量 -> PATH -> imageio-ffmpeg 自带二进制。"""
    p = os.environ.get("BLR_FFMPEG")
    if p:
        return p
    w = shutil.which("ffmpeg")
    if w:
        return w
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _popen_kwargs() -> dict:
    # Windows 需要独立进程组，停止时才能用 CTRL_BREAK_EVENT 优雅收尾
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


class VideoRecorder:
    """单个 ffmpeg 子进程：从直播流 URL 直录为 FLV 文件。"""

    def __init__(self, url: str, output_path: Path,
                 headers: Optional[dict[str, str]] = None):
        self.url = url
        self.output_path = output_path
        self.headers = headers or {}
        self.process: Optional[asyncio.subprocess.Process] = None
        self.started_at: float = 0.0
        self._stderr_tail: list[str] = []
        self._pump: Optional[asyncio.Task] = None
        self.return_code: Optional[int] = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.return_code is None

    def _build_cmd(self) -> list[str]:
        cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "warning"]
        if self.headers:
            header_str = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())
            cmd += ["-headers", header_str]
        cmd += [
            "-rw_timeout", "15000000",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-i", self.url,
            "-c", "copy",
            "-f", "flv",
            "-y",
            str(self.output_path),
        ]
        return cmd

    async def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.process = await asyncio.create_subprocess_exec(
            *self._build_cmd(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **_popen_kwargs(),
        )
        self._pump = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-20]

    def error_hint(self) -> str:
        return " | ".join(self._stderr_tail[-3:]) if self._stderr_tail else ""

    async def wait_finished(self) -> int:
        assert self.process
        return await self.process.wait()

    def _send_stop_signal(self) -> bool:
        """Linux/macOS 用 SIGINT；Windows 用 CTRL_BREAK_EVENT（配合进程组）。"""
        try:
            if sys.platform == "win32":
                os.kill(self.process.pid, signal.CTRL_BREAK_EVENT)
            else:
                self.process.send_signal(signal.SIGINT)
            return True
        except Exception:
            return False

    async def stop(self, timeout: float = 15.0) -> int:
        """优雅停止：先发停止信号让 ffmpeg 写完文件尾，超时再 kill。"""
        if not self.process:
            return 0
        if self.return_code is None:
            if self._send_stop_signal():
                try:
                    self.return_code = await asyncio.wait_for(
                        self.process.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
            if self.return_code is None:
                try:
                    self.process.kill()
                except Exception:
                    pass
                self.return_code = await self.process.wait()
        if self._pump:
            self._pump.cancel()
        return self.return_code or 0


async def remux_flv_to_mp4(flv_path: Path, mp4_path: Path) -> bool:
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-y", "-i", str(flv_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(mp4_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    code = await proc.wait()
    if code != 0 or not mp4_path.exists():
        mp4_path.unlink(missing_ok=True)
        return False
    return True
