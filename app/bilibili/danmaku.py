"""B站直播弹幕 WebSocket 客户端（协议对照 blrec src/blrec/bili/danmaku_client.py）。"""
import asyncio
import json
import logging
import struct
import time
import zlib
from typing import Any, Awaitable, Callable, Optional

import brotli
import websockets

log = logging.getLogger("danmaku")

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_USER_AUTH = 7
OP_CONNECT_SUCCESS = 8

HEADER_FMT = ">IHHII"
HEADER_LEN = 16
HEARTBEAT_INTERVAL = 30


def _pack(op: int, body: bytes, ver: int = 1) -> bytes:
    return struct.pack(HEADER_FMT, HEADER_LEN + len(body), HEADER_LEN,
                       ver, op, 1) + body


class DanmakuClient:
    """
    on_danmaku(info) 收到弹幕（protover=3 的 info 数组）；
    on_event(cmd, payload) 收到其它事件。
    run_forever() 自动重连并轮换 host / 刷新 token；stop() 结束。
    """

    def __init__(self, room_id: int, hosts: list[dict], token: str,
                 buvid: str = "", uid: int = 0,
                 on_danmaku: Optional[Callable[[dict], Awaitable[None]]] = None,
                 on_event: Optional[Callable[[str, dict], Awaitable[None]]] = None,
                 refresh_info: Optional[Callable[[], Awaitable[None]]] = None):
        self.room_id = room_id
        self.hosts = hosts
        self.token = token
        self.buvid = buvid
        self.uid = uid
        self.on_danmaku = on_danmaku
        self.on_event = on_event
        self.refresh_info = refresh_info
        self._stop = asyncio.Event()
        self._host_index = 0
        self.connected = False
        self.online_count = 0
        self.last_message_ts = 0.0

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        delay = 2.0
        while not self._stop.is_set():
            try:
                await self._run_once()
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                log.warning("danmaku ws error room=%s: %r", self.room_id, e)
            if self._stop.is_set():
                break
            # 换下一个弹幕服务器；全部失败则刷新 token
            self._host_index += 1
            if self._host_index >= len(self.hosts):
                self._host_index = 0
                if self.refresh_info:
                    try:
                        await self.refresh_info()
                    except Exception as e:
                        log.warning("refresh danmu info failed: %r", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60)

    def _auth_packet(self) -> bytes:
        body = {
            "uid": self.uid,
            "roomid": self.room_id,  # 必须是真实房间号
            "protover": 3,
            "buvid": self.buvid,
            "platform": "web",
            "type": 2,
            "key": self.token,
        }
        return _pack(OP_USER_AUTH, json.dumps(body).encode())

    async def _run_once(self) -> None:
        if not self.hosts:
            raise RuntimeError("no danmaku host")
        host = self.hosts[self._host_index]
        hostname = host["host"]
        port = int(host.get("wss_port") or 443)
        url = f"wss://{hostname}:{port}/sub"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://live.bilibili.com",
            "Referer": f"https://live.bilibili.com/{self.room_id}",
        }
        async with websockets.connect(
                url, max_size=None, close_timeout=5,
                additional_headers=headers) as ws:
            await ws.send(self._auth_packet())
            reply = await asyncio.wait_for(ws.recv(), timeout=10)
            self._handle_auth_reply(reply)
            self.connected = True
            self.last_message_ts = time.time()
            hb_task = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    self.last_message_ts = time.time()
                    self._handle_packet(
                        raw if isinstance(raw, bytes) else bytes(raw))
                    if self._stop.is_set():
                        break
            finally:
                hb_task.cancel()
                self.connected = False

    def _handle_auth_reply(self, data: Any) -> None:
        data = data if isinstance(data, bytes) else bytes(data)
        if len(data) < HEADER_LEN:
            raise RuntimeError("bad auth reply")
        total_len, header_len, _ver, op, _seq = struct.unpack(
            HEADER_FMT, data[:HEADER_LEN])
        if op not in (OP_CONNECT_SUCCESS, OP_USER_AUTH):
            raise RuntimeError(f"unexpected auth reply op={op}")
        body = json.loads(data[header_len:total_len].decode("utf-8"))
        code = body.get("code", -1)
        if code != 0:
            raise RuntimeError(f"danmaku auth failed code={code}")

    async def _heartbeat(self, ws) -> None:
        packet = _pack(OP_HEARTBEAT, b"")
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=HEARTBEAT_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await ws.send(packet)
            except Exception:
                return

    def _handle_packet(self, data: bytes) -> None:
        if len(data) < HEADER_LEN:
            return
        total_len, header_len, ver, op, _seq = struct.unpack(
            HEADER_FMT, data[:HEADER_LEN])
        if total_len > len(data):
            return
        body = data[header_len:total_len]
        if op == OP_HEARTBEAT_REPLY:
            if len(body) >= 4:
                self.online_count = struct.unpack(">I", body[:4])[0]
            return
        if op != OP_MESSAGE or not body:
            return
        if ver == 3:
            body = brotli.decompress(body)
        elif ver == 2:
            body = zlib.decompress(body)
        elif ver != 0:
            return
        for payload in self._split_subpackets(body):
            try:
                item = json.loads(payload)
            except json.JSONDecodeError:
                continue
            self._dispatch_now(item)

    @staticmethod
    def _split_subpackets(data: bytes) -> list[bytes]:
        out: list[bytes] = []
        offset = 0
        n = len(data)
        while offset + HEADER_LEN <= n:
            plen, hlen, _ver, _op, _seq = struct.unpack_from(
                HEADER_FMT, data, offset)
            if plen <= 0 or hlen < 4 or offset + plen > n:
                break
            out.append(data[offset + hlen: offset + plen])
            offset += plen
        if not out and data:
            out.append(data)  # 无子包头的裸 JSON
        return out

    def _dispatch_now(self, item: Any) -> None:
        # 同步入口：把协程投递到事件循环
        if not isinstance(item, dict):
            return
        cmd = str(item.get("cmd", ""))
        info = item.get("info")
        if cmd.startswith("DANMU_MSG") or (not cmd and isinstance(info, list)):
            if self.on_danmaku:
                asyncio.ensure_future(self.on_danmaku(item))
        elif cmd and self.on_event:
            asyncio.ensure_future(self.on_event(cmd, item))
