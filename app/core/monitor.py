"""每房间监控任务：轮询开播状态，自动启停录制会话。"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .. import config, db
from ..bilibili.api import BiliAPIError, BiliLiveAPI
from .session import RecordingSession

log = logging.getLogger("monitor")


@dataclass
class RoomRuntime:
    room_id: int
    task: Optional[asyncio.Task] = None
    session: Optional[RecordingSession] = None
    living: bool = False
    status: str = "idle"       # idle / living / recording / error
    error: str = ""
    last_check: float = 0.0
    offline_count: int = 0
    stop_requested: bool = False


class MonitorManager:
    def __init__(self) -> None:
        self.api = BiliLiveAPI(cookie=db.get_setting("cookie"))
        self.rooms: dict[int, RoomRuntime] = {}
        self.poll_interval = self._load_poll_interval()
        self._lock = asyncio.Lock()

    def _load_poll_interval(self) -> int:
        try:
            return max(10, min(300, int(db.get_setting("poll_interval",
                                                       str(config.POLL_INTERVAL)))))
        except ValueError:
            return config.POLL_INTERVAL

    # ---------- 设置 ----------

    def update_cookie(self, cookie: str) -> None:
        self.api.cookie = cookie.strip()
        self.api._mixin_key = ""  # 强制重新获取 WBI key

    def update_poll_interval(self, seconds: int) -> None:
        self.poll_interval = max(10, min(300, seconds))

    # ---------- 房间任务管理 ----------

    async def start_all(self) -> None:
        for room in db.list_rooms():
            self.spawn(room["room_id"])
        log.info("monitor started for %d rooms", len(self.rooms))

    async def stop_all(self) -> None:
        for rt in list(self.rooms.values()):
            rt.stop_requested = True
            if rt.session:
                rt.session.request_stop()
            if rt.task:
                rt.task.cancel()
        for rt in list(self.rooms.values()):
            if rt.task:
                try:
                    await rt.task
                except (asyncio.CancelledError, Exception):
                    pass
        self.rooms.clear()

    def spawn(self, room_id: int) -> None:
        if room_id in self.rooms:
            return
        rt = RoomRuntime(room_id=room_id)
        rt.task = asyncio.create_task(self._room_loop(rt))
        self.rooms[room_id] = rt

    async def remove(self, room_id: int) -> None:
        rt = self.rooms.pop(room_id, None)
        if not rt:
            return
        if rt.session:
            rt.session.request_stop()
        if rt.task:
            rt.task.cancel()

    def get_runtime(self, room_id: int) -> Optional[RoomRuntime]:
        return self.rooms.get(room_id)

    async def force_record(self, room_id: int) -> None:
        rt = self.rooms.get(room_id)
        if not rt:
            raise ValueError("该房间未在监控中")
        if rt.session:
            raise ValueError("该房间正在录制中")
        await self._start_session(rt)

    async def force_stop(self, room_id: int) -> None:
        rt = self.rooms.get(room_id)
        if rt and rt.session:
            rt.session.request_stop()
            rt.stop_requested = True

    # ---------- 监控循环 ----------

    async def _room_loop(self, rt: RoomRuntime) -> None:
        room_id = rt.room_id
        try:
            while True:
                row = db.get_room(room_id)
                if row is None:
                    break
                try:
                    await self._tick(rt, row)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    rt.status = "error"
                    rt.error = f"{type(e).__name__}: {e}"
                    log.warning("room %s tick error: %s", room_id, e)
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            pass
        finally:
            if rt.session:
                try:
                    rt.session.request_stop()
                    await rt.session.finalize()
                except Exception:
                    log.exception("room %s finalize on exit failed", room_id)
            self.rooms.pop(room_id, None)

    async def _tick(self, rt: RoomRuntime, row: dict) -> None:
        room_id = rt.room_id
        rt.last_check = time.time()
        rt.error = ""

        try:
            info = await self.api.get_room_info(room_id)
        except BiliAPIError as e:
            if e.code in (60004, 1002004, -404):  # 房间不存在
                rt.status = "error"
                rt.error = "房间不存在或已被封禁"
                return
            raise

        # 同步房间元数据
        changes = {}
        if info.title and info.title != row["title"]:
            changes["title"] = info.title
        if info.name and info.name != row["name"]:
            changes["name"] = info.name
        if info.cover and info.cover != row["cover"]:
            changes["cover"] = info.cover
        if changes:
            db.update_room(room_id, **changes)

        living = info.live_status == 1
        rt.living = living

        session = rt.session
        if session:
            # 分段轮转期间旧进程正常退出，不误判为流中断
            recorder_died = (session._recorder and not session._recorder.running
                             and not session._seg_lock.locked())
            if rt.stop_requested or session.stop_requested:
                await self._finish_session(rt, reason="手动/下播停止")
            elif not living:
                rt.offline_count += 1
                if rt.offline_count >= config.OFFLINE_CONFIRM_TIMES:
                    await self._finish_session(rt, reason="主播已下播")
            elif recorder_died:
                # 流中断但仍在直播：结束本段，下轮自动开新段
                await self._finish_session(rt, reason="流中断，重新分段")
            else:
                rt.status = "recording"
            rt.stop_requested = False
            return

        if not row["enabled"]:
            rt.status = "idle"
            return
        if not living:
            rt.status = "offline"
            return
        await self._start_session(rt, info_live_start=info.live_start_time,
                                  info_title=info.title or row["title"])

    async def _start_session(self, rt: RoomRuntime,
                             info_live_start: float = 0.0,
                             info_title: str = "") -> None:
        row = db.get_room(rt.room_id) or {}
        quality = int(row.get("quality", 10000))
        candidates = await self.api.get_stream_urls(
            rt.room_id, qn=quality if quality else 10000)
        flv_candidates = [c for c in candidates if c.fmt == "flv"] or candidates
        if not flv_candidates:
            rt.status = "error"
            rt.error = "获取直播流地址失败（未开播或 Cookie 无权限）"
            return
        candidate = flv_candidates[0]
        live_start = info_live_start
        title = info_title or row.get("title") or f"room{rt.room_id}"
        session = RecordingSession(
            api=self.api,
            room_id=rt.room_id,
            title=title,
            candidate=candidate,
            live_start_time=live_start,
            record_danmaku=bool(row.get("record_danmaku", 1)),
            auto_convert_mp4=bool(row.get("auto_convert_mp4", 1)),
            split_mode=row.get("split_mode", "none"),
            split_arg=int(row.get("split_arg", 0) or 0),
        )
        try:
            await session.start()
        except Exception as e:
            rt.status = "error"
            rt.error = f"启动录制失败: {e}"
            return
        rt.session = session
        rt.status = "recording"
        rt.offline_count = 0
        asyncio.create_task(self._watch_session(rt, session))

    async def _watch_session(self, rt: RoomRuntime,
                             session: RecordingSession) -> None:
        """监听 ffmpeg 进程退出，主动结束会话（避免等到下个轮询周期）。
        分段轮转会更换 recorder，需持续跟踪新分段进程。"""
        while True:
            rec = session._recorder
            if rec is None or rec.process is None:
                return
            await rec.wait_finished()
            if rt.session is not session:
                return
            if session._recorder is not rec:
                continue  # 正常分段轮转，继续观察新进程
            if not session.stop_requested:
                log.info("room %s ffmpeg exited", rt.room_id)
                await self._finish_session(rt, reason="录制进程退出")
            return

    async def _finish_session(self, rt: RoomRuntime, reason: str) -> None:
        session = rt.session
        if not session:
            return
        rt.session = None
        log.info("room %s stop recording: %s", rt.room_id, reason)
        try:
            await session.finalize()
        except Exception:
            log.exception("room %s finalize failed", rt.room_id)
        rt.status = "living" if rt.living else "offline"

    # ---------- 快照 ----------

    def snapshot(self) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for rid, rt in self.rooms.items():
            rec_seconds = (time.time() - rt.session.started_at) \
                if rt.session else None
            out[rid] = {
                "status": rt.status,
                "living": rt.living,
                "recording": rt.session is not None,
                "rec_seconds": rec_seconds,
                "error": rt.error,
                "last_check": rt.last_check,
            }
        return out
