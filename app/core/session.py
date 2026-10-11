"""一次录制会话：视频（ffmpeg）+ 弹幕（WebSocket XML/JSONL）同步落盘。"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from .. import config, db
from ..bilibili.api import BiliLiveAPI, StreamCandidate
from ..bilibili.danmaku import DanmakuClient
from . import naming
from .recorder import VideoRecorder, remux_flv_to_mp4

log = logging.getLogger("session")

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
SPLIT_CHECK_INTERVAL = 5  # 秒，分段条件检查粒度


def safe_filename(name: str, limit: int = 80) -> str:
    name = _UNSAFE.sub("_", name).strip(" ._")
    return name[:limit] or "untitled"


class RecordingSession:
    def __init__(self, api: BiliLiveAPI, room_id: int, title: str,
                 candidate: StreamCandidate, live_start_time: float,
                 record_danmaku: bool, auto_convert_mp4: bool,
                 split_mode: str = "none", split_arg: int = 0,
                 anchor_name: str = ""):
        self.api = api
        self.room_id = room_id
        self.title = title
        self.anchor_name = anchor_name
        self.candidate = candidate
        self.live_start_time = live_start_time  # 服务器开播时间戳（秒）
        self.record_danmaku = record_danmaku
        self.auto_convert_mp4 = auto_convert_mp4
        self.split_mode = split_mode if split_mode in ("none", "time", "size") \
            else "none"
        self.split_arg = max(0, int(split_arg or 0))

        self.dir = config.RECORDINGS_DIR / f"room{room_id}"
        self.flv_path = self.dir / "pending.flv"
        self.xml_path = self.dir / "pending.xml"
        self.events_path = self.dir / "pending.events.jsonl"
        self.mp4_path = self.dir / "pending.mp4"

        self.started_at = time.time()       # 整场会话开始
        self.segment_started_at = self.started_at
        self.danmaku_count = 0              # 当前分段弹幕计数
        self.recording_id = 0
        self.status = "starting"  # recording / converting / completed / failed
        self.stop_requested = False

        self._recorder: VideoRecorder | None = None
        self._danmaku: DanmakuClient | None = None
        self._xml_fh = None
        self._events_fh = None
        self._stopped_event = asyncio.Event()
        self._seg_lock = asyncio.Lock()
        self._seg_index = 0
        self._any_data = False
        self._split_task: asyncio.Task | None = None

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": config.USER_AGENT,
            "Referer": f"https://live.bilibili.com/{self.room_id}",
        }
        if self.api.cookie:
            headers["Cookie"] = self.api.cookie
        return headers

    def _segment_paths(self):
        fmt = db.get_setting("filename_format", "").strip() \
            or naming.DEFAULT_TEMPLATE
        kw = dict(room_id=self.room_id, room_name=self.title,
                  anchor_name=self.anchor_name, part_index=self._seg_index,
                  start_time=datetime.fromtimestamp(time.time()))
        used = fmt
        try:
            stem = naming.render(fmt, **kw)
        except naming.TemplateError as e:
            log.warning("room %s 文件名模板无效（%s），回退默认", self.room_id, e)
            used = naming.DEFAULT_TEMPLATE
            stem = naming.render(used, **kw)
        if self._seg_index > 1 and not naming.uses_part_index(used):
            stem = f"{stem}_p{self._seg_index:02d}"
        return (self.dir / f"{stem}.flv", self.dir / f"{stem}.xml",
                self.dir / f"{stem}.events.jsonl", self.dir / f"{stem}.mp4")

    def _open_segment(self) -> None:
        self._seg_index += 1
        (self.flv_path, self.xml_path, self.events_path,
         self.mp4_path) = self._segment_paths()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flv_path.parent.mkdir(parents=True, exist_ok=True)
        self.segment_started_at = time.time()
        self.danmaku_count = 0
        self.recording_id = db.add_recording(
            self.room_id, self.title, self.segment_started_at,
            video_file=str(self.flv_path.relative_to(config.DATA_DIR)))
        if self.record_danmaku:
            self._xml_fh = open(self.xml_path, "w", encoding="utf-8")
            self._xml_fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<i>\n')
            self._xml_fh.write(
                "<metadata>\n"
                f"<platform>bilibili</platform>\n"
                f"<room_id>{self.room_id}</room_id>\n"
                f"<room_title>{xml_escape(self.title)}</room_title>\n"
                f"<video_start_time>{int(self.segment_started_at)}</video_start_time>\n"
                f"<live_start_time>{int(self.live_start_time or 0)}</live_start_time>\n"
                f"<recorder>{config.APP_NAME} {config.VERSION}</recorder>\n"
                "</metadata>\n")
            self._events_fh = open(self.events_path, "w", encoding="utf-8")

    async def _close_segment(self) -> dict:
        """停当前 ffmpeg、写完文件尾，返回该分段的收尾信息。"""
        if self._recorder:
            await self._recorder.stop()
        if self._xml_fh:
            try:
                self._xml_fh.write("</i>\n")
                self._xml_fh.close()
            except Exception:
                pass
        if self._events_fh:
            try:
                self._events_fh.close()
            except Exception:
                pass
        self._xml_fh = None
        self._events_fh = None
        return {
            "recording_id": self.recording_id,
            "flv_path": self.flv_path, "xml_path": self.xml_path,
            "events_path": self.events_path, "mp4_path": self.mp4_path,
            "danmaku_count": self.danmaku_count,
        }

    async def _finish_segment(self, seg: dict) -> int:
        """转封装并回写数据库，返回该分段最终大小。"""
        flv, mp4 = seg["flv_path"], seg["mp4_path"]
        video_file = str(flv.relative_to(config.DATA_DIR))
        final_size = flv.stat().st_size if flv.exists() else 0
        if self.auto_convert_mp4 and final_size > 0:
            ok = await remux_flv_to_mp4(flv, mp4)
            if ok:
                flv.unlink(missing_ok=True)
                video_file = str(mp4.relative_to(config.DATA_DIR))
                final_size = mp4.stat().st_size
        if final_size > 0:
            self._any_data = True
        db.finish_recording(
            seg["recording_id"],
            status="completed" if final_size > 0 else "failed",
            end_ts=time.time(), size=final_size, video_file=video_file,
            danmaku_file=str(seg["xml_path"].relative_to(config.DATA_DIR))
            if self.record_danmaku and seg["xml_path"].exists() else "",
            events_file=str(seg["events_path"].relative_to(config.DATA_DIR))
            if self.record_danmaku and seg["events_path"].exists() else "",
            danmaku_count=seg["danmaku_count"])
        return final_size

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._open_segment()
        self._recorder = VideoRecorder(
            self.candidate.url, self.flv_path, self._build_headers())
        try:
            await self._recorder.start()
        except (FileNotFoundError, OSError) as e:
            self.status = "failed"
            db.finish_recording(self.recording_id, status="failed",
                                end_ts=time.time())
            log.error("ffmpeg 启动失败（请确认已安装 ffmpeg）: %r", e)
            raise

        if self.record_danmaku:
            asyncio.create_task(self._run_danmaku())
        if self.split_mode != "none" and self.split_arg > 0:
            self._split_task = asyncio.create_task(self._split_loop())
        self.status = "recording"
        log.info("session started room=%s -> %s (split=%s/%s)",
                 self.room_id, self.flv_path, self.split_mode, self.split_arg)

    def request_stop(self) -> None:
        self.stop_requested = True

    async def wait_finished(self) -> None:
        await self._stopped_event.wait()

    # ---------- 分段 ----------

    def _split_due(self) -> bool:
        if self.split_mode == "time":
            return (time.time() - self.segment_started_at
                    >= self.split_arg * 60)
        if self.split_mode == "size":
            try:
                sz = self.flv_path.stat().st_size if self.flv_path.exists() else 0
            except OSError:
                return False
            return sz >= self.split_arg * 1024 * 1024
        return False

    async def check_split(self) -> bool:
        """达到分段条件时轮转：旧段收尾转封装，新段立即续录（弹幕 WS 不断开）。"""
        if (self.split_mode == "none" or not self.split_arg
                or self.stop_requested or self._stopped_event.is_set()):
            return False
        if not self._split_due():
            return False
        async with self._seg_lock:
            if self.stop_requested or self._stopped_event.is_set():
                return False
            log.info("room %s split segment (mode=%s arg=%s)",
                     self.room_id, self.split_mode, self.split_arg)
            seg = await self._close_segment()
            self._open_segment()
            self._recorder = VideoRecorder(
                self.candidate.url, self.flv_path, self._build_headers())
            try:
                await self._recorder.start()
            except Exception:
                await self._finish_segment(seg)
                raise
            await self._finish_segment(seg)
        return True

    async def _split_loop(self) -> None:
        while not self._stopped_event.is_set():
            try:
                await asyncio.wait_for(self._stopped_event.wait(),
                                       timeout=SPLIT_CHECK_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.check_split()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("room %s split failed", self.room_id)
                return  # 新段启动失败等异常：交给监控循环重新取流分段

    async def _run_danmaku(self) -> None:
        hosts: list[dict] = []
        token = ""
        for attempt in range(5):
            if self.stop_requested:
                return
            try:
                hosts, token = await self.api.get_danmu_info(self.room_id)
                break
            except Exception as e:
                log.warning("getDanmuInfo failed room=%s (try %d): %r",
                            self.room_id, attempt + 1, e)
                await asyncio.sleep(3 * (attempt + 1))
        else:
            log.error("getDanmuInfo permanently failed room=%s", self.room_id)
            return
        self._danmaku = DanmakuClient(
            room_id=self.room_id, hosts=hosts, token=token,
            buvid=self.api.buvid, uid=self.api.uid,
            on_danmaku=self._on_danmaku, on_event=self._on_event,
            refresh_info=self._refresh_danmu_info)
        stopper = asyncio.create_task(self._stopped_event.wait())
        runner = asyncio.create_task(self._danmaku.run_forever())
        done, pending = await asyncio.wait(
            [stopper, runner], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        self._danmaku.stop()
        runner.cancel()
        stopper.cancel()

    async def _refresh_danmu_info(self) -> None:
        hosts, token = await self.api.get_danmu_info(self.room_id)
        if self._danmaku:
            self._danmaku.hosts = hosts
            self._danmaku.token = token

    # ---------- 弹幕回调 ----------

    async def _on_danmaku(self, item: dict) -> None:
        """protover=3 的 DANMU_MSG：info[0]=参数数组, info[1]=文本, info[2]=[uid, uname]。"""
        if not self._xml_fh:
            return
        info = item.get("info")
        if not isinstance(info, list) or len(info) < 3:
            return
        meta, text, user = info[0], info[1], info[2]
        try:
            mode = int(meta[1])
            size = int(meta[2])
            color = int(meta[3])
            date_ms = int(meta[4])
            dmid = str(meta[5])
            pool = int(meta[6]) if len(meta) > 6 else 0
            uid_hash = str(meta[7]) if len(meta) > 7 else ""
            uid = int(user[0]) if isinstance(user, list) else 0
            uname = str(user[1]) if isinstance(user, list) and len(user) > 1 else ""
        except (ValueError, TypeError, IndexError):
            return
        stime = max(0.0, date_ms / 1000.0 - self.segment_started_at)
        p = (f"{stime:.3f},{mode},{size},{color},{date_ms},"
             f"{pool},{uid_hash},{dmid}")
        line = (f'<d p={quoteattr(p)} uid={quoteattr(str(uid))} '
                f'user={quoteattr(uname)}>{xml_escape(str(text))}</d>\n')
        self._xml_fh.write(line)
        self._xml_fh.flush()
        self.danmaku_count += 1

    def _seg_ts(self, data: dict) -> float:
        try:
            return max(0.0, float(data.get("timestamp") or 0)
                       - self.segment_started_at)
        except (TypeError, ValueError):
            return 0.0

    def _event_to_xml(self, cmd: str, payload: dict) -> None:
        """SC/礼物/上舰写入 <sc>/<gift>/<guard> 节点（结构参考 biliLive-tools，
        danmaku2ass 等工具只解析 <d>，不影响字幕转换）。"""
        if not self._xml_fh:
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        try:
            if cmd == "SUPER_CHAT_MESSAGE":
                line = (f'<sc ts="{self._seg_ts(data):.3f}"'
                        f' uid={quoteattr(str(data.get("uid", "")))}'
                        f' user={quoteattr(str(data.get("uname", "")))}'
                        f' price="{int(data.get("price", 0) or 0)}"'
                        f' time="{int(data.get("time", 0) or 0)}"'
                        f'>{xml_escape(str(data.get("message", "")))}</sc>\n')
            elif cmd == "SEND_GIFT":
                line = (f'<gift ts="{self._seg_ts(data):.3f}"'
                        f' uid={quoteattr(str(data.get("uid", "")))}'
                        f' user={quoteattr(str(data.get("uname", "")))}'
                        f' giftname={quoteattr(str(data.get("giftName", "")))}'
                        f' count="{int(data.get("num", 0) or 0)}"'
                        f' coin_type={quoteattr(str(data.get("coin_type", "")))}'
                        f' total_coin="{int(data.get("total_coin", 0) or 0)}"/>\n')
            elif cmd == "GUARD_BUY":
                line = (f'<guard ts="{self._seg_ts(data):.3f}"'
                        f' uid={quoteattr(str(data.get("uid", "")))}'
                        f' user={quoteattr(str(data.get("username", "")))}'
                        f' level="{int(data.get("level", 0) or 0)}"'
                        f' giftname={quoteattr(str(data.get("gift_name", "")))}'
                        f' count="{int(data.get("num", 0) or 0)}"'
                        f' price="{int(data.get("price", 0) or 0)}"/>\n')
            else:
                return
        except (TypeError, ValueError):
            return
        self._xml_fh.write(line)
        self._xml_fh.flush()

    async def _on_event(self, cmd: str, payload: dict) -> None:
        if cmd == "DANMU_MSG" or cmd in (
                "WATCH_ROOM_CHANGE_COUNT_MATCH", "room_admin_entrance",
                "ROOM_CHANGE", "ROOM_REFRESH", "INTERACT_WORD"):
            return
        if self._events_fh:
            data = payload.get("data", payload)
            self._events_fh.write(json.dumps(
                {"ts": time.time(), "cmd": cmd, "data": data},
                ensure_ascii=False) + "\n")
            self._events_fh.flush()
        self._event_to_xml(cmd, payload)
        if cmd == "PREPARING" and not self.stop_requested:
            log.info("room %s PREPARING from danmaku server, stopping session",
                     self.room_id)
            self.request_stop()

    # ---------- 收尾 ----------

    async def finalize(self) -> str:
        """停止一切：收尾当前最后一个分段，更新会话状态。"""
        if self._split_task:
            self._split_task.cancel()
            self._split_task = None
        if self._danmaku:
            self._danmaku.stop()
        async with self._seg_lock:
            self.status = "converting"
            seg = await self._close_segment()
            await self._finish_segment(seg)
        self.status = "completed" if self._any_data else "failed"
        self._stopped_event.set()
        log.info("session finished room=%s status=%s segments=%d",
                 self.room_id, self.status, self._seg_index)
        return self.status
