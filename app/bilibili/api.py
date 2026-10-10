import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .. import config

LIVE_REFERER = "https://live.bilibili.com"


# ---------- WBI 签名（算法对照 blrec src/blrec/bili/wbi.py） ----------

def make_mixin_key(img_key: str, sub_key: str) -> str:
    mapping = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    ]
    key = (img_key + sub_key).encode()
    return bytes([key[n] for n in mapping]).decode()


# 自检向量（来自 blrec）
assert make_mixin_key("7cd084941338484aae1ad9425b84077c",
                      "4932caff0ff746eab6f01bf08b70ac45") \
    == "ea1db124af3c7062474693fa704f4ff8"


def _encode_value(value: str) -> str:
    chars = []
    for c in value:
        if c in "!'()*":
            continue
        if (c.isascii() and c.isalnum()) or c in "-_.~":
            chars.append(c)
        else:
            for b in c.encode():
                chars.append(f"%{b:02X}")
    return "".join(chars)


def build_wbi_query(key: str, params: dict[str, Any]) -> str:
    items = sorted((str(k), str(v)) for k, v in params.items())
    items.append(("wts", str(int(time.time()))))
    items.sort(key=lambda p: p[0])
    query = "&".join(f"{n}={_encode_value(v)}" for n, v in items)
    sign = hashlib.md5((query + key).encode()).hexdigest()
    return f"{query}&w_rid={sign}"


# ---------- 数据模型 ----------

@dataclass
class StreamCandidate:
    url: str
    quality: int
    fmt: str    # flv / fmp4 / ts
    codec: str  # avc / hevc


@dataclass
class RoomInfo:
    room_id: int          # 真实房间号
    short_id: int         # 短号（无则 0）
    name: str             # 主播名
    title: str
    cover: str
    live_status: int      # 0 未开播 1 直播中 2 轮播
    live_start_time: float


class BiliAPIError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"B站接口错误 code={code}: {message}")


def _host_priority(host: str) -> int:
    m = re.search(r"gotcha(\d+)", host)
    if m:
        num = m.group(1)
        order = {"04": 0, "09": 1, "08": 2, "05": 3, "07": 4}
        return order.get(num, 1000 + int(num))
    if "mcdn" in host:
        return 2000
    if re.search(r"cn-[a-z]+-[a-z]+", host):
        return 5000
    return 10000


class BiliLiveAPI:
    """B站直播开放接口封装（异步）。cookie 为完整 Cookie 字符串。"""

    def __init__(self, cookie: str = ""):
        self.cookie = cookie.strip()
        self.buvid: str = ""
        self.uid: int = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._mixin_key: str = ""
        self._mixin_key_ts: float = 0.0

    # ---------- 基础设施 ----------

    def _update_identity_from_cookie(self) -> None:
        if not self.cookie:
            return
        m = re.search(r"buvid3=([^;]+)", self.cookie)
        if m:
            self.buvid = m.group(1)
        m = re.search(r"DedeUserID=(\d+)", self.cookie)
        if m:
            self.uid = int(m.group(1))

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=config.HTTP_TIMEOUT,
                headers=self._base_headers(),
                follow_redirects=True,
            )
        return self._client

    def _base_headers(self) -> dict[str, str]:
        h = {
            "User-Agent": config.USER_AGENT,
            "Referer": LIVE_REFERER,
            "Origin": "https://live.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _plain_get(self, url: str,
                         params: Optional[dict] = None) -> dict:
        client = await self._get_client()
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _check(data: dict) -> dict:
        code = data.get("code", -1)
        if code != 0:
            raise BiliAPIError(int(code), str(data.get("message", "")))
        return data

    async def _wbi_get(self, url: str, params: dict[str, Any],
                       _retry: bool = True) -> dict:
        if not self._mixin_key or time.time() - self._mixin_key_ts > 86400:
            await self._update_wbi_key()
        query = build_wbi_query(self._mixin_key, params)
        client = await self._get_client()
        resp = await client.get(f"{url}?{query}")
        resp.raise_for_status()
        data = resp.json()
        code = data.get("code", -1)
        if code == -352 and _retry:
            # 风控：强制刷新 key 后重试一次
            self._mixin_key = ""
            return await self._wbi_get(url, params, _retry=False)
        if code != 0:
            raise BiliAPIError(int(code), str(data.get("message", "")))
        return data

    async def _update_wbi_key(self) -> None:
        data = await self._plain_get("https://api.bilibili.com/x/web-interface/nav")
        wbi_img = (data.get("data") or {}).get("wbi_img") or {}
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")
        if not img_url or not sub_url:
            raise BiliAPIError(-1, "获取 WBI 密钥失败")
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        self._mixin_key = make_mixin_key(img_key, sub_key)
        self._mixin_key_ts = time.time()

    # ---------- 房间信息（getInfoByRoom，WBI） ----------

    async def get_room_info(self, room_id: int) -> RoomInfo:
        data = await self._wbi_get(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom",
            {"room_id": room_id},
        )
        d = data["data"]
        ri = d["room_info"]
        name = (d.get("anchor_info") or {}).get("base_info", {}).get("uname", "")

        live_start = float(ri.get("live_start_time") or 0)
        if not live_start and ri.get("live_time"):
            ts = ri["live_time"]
            if ts != "0000-00-00 00:00:00":
                try:
                    from datetime import datetime
                    live_start = datetime.fromisoformat(ts).timestamp()
                except ValueError:
                    live_start = 0.0

        return RoomInfo(
            room_id=int(ri["room_id"]),
            short_id=int(ri.get("short_id") or 0),
            name=name,
            title=ri.get("title", "") or "",
            cover=ri.get("cover", "") or "",
            live_status=int(ri.get("live_status", 0)),
            live_start_time=live_start,
        )

    # ---------- 流地址（getRoomPlayInfo，WBI） ----------

    async def get_stream_urls(self, room_id: int, qn: int = 10000,
                              _fallback: bool = True) -> list[StreamCandidate]:
        params = {
            "room_id": room_id,
            "protocol": "0,1",
            "format": "0,1,2",
            "codec": "0,1",
            "qn": qn,
            "platform": "web",
            "ptype": 8,
        }
        data = await self._wbi_get(
            "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo",
            params,
        )
        playurl = (data["data"].get("playurl_info") or {}).get("playurl") or {}
        candidates: list[StreamCandidate] = []
        for stream in playurl.get("stream", []):
            for fmt_obj in stream.get("format", []):
                fmt_name = fmt_obj.get("format_name", "")
                for codec_obj in fmt_obj.get("codec", []):
                    codec_name = codec_obj.get("codec_name", "")
                    if codec_name != "avc":
                        continue
                    current_qn = int(codec_obj.get("current_qn", 0) or 0)
                    base_url = codec_obj.get("base_url", "")
                    for ui in codec_obj.get("url_info", []):
                        url = ui.get("host", "") + base_url + ui.get("extra", "")
                        if url.startswith("https://"):
                            candidates.append(StreamCandidate(
                                url=url, quality=current_qn,
                                fmt=fmt_name, codec=codec_name))
        if not candidates and _fallback and qn > 10000:
            # 4K 等无权限画质回退原画
            return await self.get_stream_urls(room_id, 10000, _fallback=False)
        fmt_order = {"flv": 0, "fmp4": 1, "ts": 2}
        candidates.sort(key=lambda c: (fmt_order.get(c.fmt, 9),
                                       -c.quality,
                                       _host_priority(c.url.split("/")[2])))
        return candidates

    # ---------- 弹幕服务器（getDanmuInfo，WBI） ----------

    async def ensure_buvid(self) -> str:
        if self.buvid:
            return self.buvid
        try:
            data = self._check(await self._plain_get(
                "https://api.bilibili.com/x/frontend/finger/spi"))
            self.buvid = data["data"]["b_3"]
        except Exception:
            self.buvid = ""
        return self.buvid

    async def get_danmu_info(self, room_id: int) -> tuple[list[dict], str]:
        """返回 (host_list, token)。"""
        self._update_identity_from_cookie()
        await self.ensure_buvid()
        data = await self._wbi_get(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
            {"id": room_id},
        )
        d = data["data"]
        hosts = d.get("host_list") or [{
            "host": "broadcastlv.chat.bilibili.com",
            "port": 2243, "wss_port": 443, "ws_port": 2244,
        }]
        return hosts, d.get("token", "")

    # ---------- 登录状态 ----------

    async def check_login(self) -> dict[str, Any]:
        try:
            data = await self._plain_get(
                "https://api.bilibili.com/x/web-interface/nav")
            d = data.get("data") or {}
            return {
                "logged_in": bool(d.get("isLogin")),
                "uname": d.get("uname", ""),
                "mid": d.get("mid", 0),
            }
        except Exception as e:
            return {"logged_in": False, "uname": "", "mid": 0,
                    "message": str(e)}
