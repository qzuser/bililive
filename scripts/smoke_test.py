"""本地冒烟测试：导入检查 + 协议纯函数自测 + 真实 B 站只读接口验证。
不启动服务器、不录制。运行： .venv/Scripts/python scripts/smoke_test.py
"""
import asyncio
import json
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("BLR_DATA_DIR",
                      tempfile.mkdtemp(prefix="blr_smoke_"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ---------- 1. 导入检查 ----------
print("== imports ==")
import app.main  # noqa: F401,E402
import app.db as db  # noqa: E402
from app.bilibili.api import (BiliLiveAPI, build_wbi_query,  # noqa: E402
                              make_mixin_key, _host_priority)
from app.bilibili.danmaku import DanmakuClient, _pack, HEADER_LEN  # noqa: E402

_split_subpackets = DanmakuClient._split_subpackets
from app.core.session import safe_filename  # noqa: E402
check("all modules imported", True)

# ---------- 2. 纯函数自测 ----------
print("== protocol units ==")
check("wbi mixin key vector",
      make_mixin_key("7cd084941338484aae1ad9425b84077c",
                     "4932caff0ff746eab6f01bf08b70ac45")
      == "ea1db124af3c7062474693fa704f4ff8")

q = build_wbi_query("ea1db124af3c7062474693fa704f4ff8",
                    {"foo": "114", "bar": "1919", "baz": "19 19^_^"})
check("wbi query has w_rid/wts and sorts params",
      "w_rid=" in q and "wts=" in q
      and q.index("bar=") < q.index("baz=") < q.index("foo=") < q.index("wts="),
      q[:80])
check("wbi filters forbidden chars", "^" not in q.split("baz=")[1][:6])

packet = _pack(7, b'{"a":1}')
plen, hlen, ver, op, seq = struct.unpack(">IHHII", packet[:16])
check("packet header big-endian",
      plen == len(packet) and hlen == 16 and ver == 1 and op == 7 and seq == 1)

body = json.dumps({"cmd": "X"}).encode()
sub = _pack(5, body) + _pack(5, body)
parts = _split_subpackets(sub)
check("subpacket split", len(parts) == 2 and parts[0] == body)
check("subpacket raw fallback",
      _split_subpackets(b'{"cmd":"Y"}') == [b'{"cmd":"Y"}'])

check("host priority", _host_priority("https://gotcha04.bilivideo.com") == 0
      < _host_priority("https://mcdn.pull.bilivideo.com")
      < _host_priority("https://cn-bj-a-01.bilivideo.com"))

check("safe_filename", safe_filename('a/b\\c:d*e?f"g<h>i|j') ==
      "a_b_c_d_e_f_g_h_i_j" and safe_filename("  ") == "untitled")

# 模拟 protover=3 弹幕 dispatch
client = DanmakuClient(room_id=1, hosts=[], token="")
captured: dict = {}


async def _dm(item):
    captured["danmaku"] = item


async def _ev(cmd, payload):
    captured["event"] = cmd


client.on_danmaku = _dm
client.on_event = _ev


async def dispatch_test():
    client._dispatch_now({"cmd": "DANMU_MSG",
                          "info": [[0, 1, 25, 16777215, 1700000000000, 123, 0, "hash"],
                                   "hello", [42, "user"]]})
    client._dispatch_now({"cmd": "SEND_GIFT", "data": {}})
    await asyncio.sleep(0.05)


asyncio.run(dispatch_test())
check("dispatch DANMU_MSG", captured.get("danmaku", {}).get("cmd") == "DANMU_MSG")
check("dispatch SEND_GIFT", captured.get("event") == "SEND_GIFT")

# ---------- 2.5 数据层与会话落盘回归 ----------
print("== db & session units ==")
import app.db as _db  # noqa: E402
from app import config as _cfg  # noqa: E402
from app.core.session import RecordingSession  # noqa: E402

_db.init()
rid = _db.add_room(9527001, name="测试主播", title="测试标题", quality=10000,
                   split_mode="time", split_arg=30)
_db.update_room(9527001, enabled=0, record_danmaku=0)
row = _db.get_room(9527001)
check("db room roundtrip", row["enabled"] == 0 and row["record_danmaku"] == 0
      and row["title"] == "测试标题"
      and row["auto_convert_mp4"] == 0)  # 新房间默认不自动转 MP4
check("db 分段字段 roundtrip", row["split_mode"] == "time"
      and row["split_arg"] == 30)
rec_pk = _db.add_recording(9527001, "测试标题", 1000.0,
                           "recordings/room9527001/a.flv")
_db.finish_recording(rec_pk, status="completed", end_ts=2000.0, size=123,
                     video_file="recordings/room9527001/a.mp4",
                     danmaku_count=7)
rec = _db.get_recording(rec_pk)
check("finish_recording 写回 mp4 路径（回归：曾漏存 video_file）",
      rec["video_file"].endswith(".mp4") and rec["danmaku_count"] == 7)
_db.delete_recording(rec_pk)
_db.delete_room(9527001)
check("db cleanup", _db.get_room(9527001) is None)

sess = RecordingSession.__new__(RecordingSession)  # 不走 __init__ 的目录依赖
sess.started_at = 1000.0
sess.segment_started_at = 1000.0
sess.danmaku_count = 0
xml_tmp = Path(_cfg.DATA_DIR) / "t.xml"
sess._xml_fh = open(xml_tmp, "w", encoding="utf-8")


sess._events_fh = None


async def xml_test():
    await sess._on_danmaku({"cmd": "DANMU_MSG", "info": [
        [0, 1, 25, 16777215, 1005123, -2014150320, 0, "6089ca9d"],
        "测试<>&弹幕", [42, "用户名"]]})
    await sess._on_danmaku({"cmd": "DANMU_MSG", "info": ["坏数据"]})
    await sess._on_event("SUPER_CHAT_MESSAGE", {"data": {
        "uid": 7, "uname": "SC用户", "message": "你好<x>&",
        "price": 30, "time": 120, "timestamp": 1010}})
    await sess._on_event("SEND_GIFT", {"data": {
        "uid": 8, "uname": "G", "giftName": "小心心", "num": 3,
        "coin_type": "silver", "total_coin": 150, "timestamp": 1011}})
    await sess._on_event("GUARD_BUY", {"data": {
        "uid": 9, "username": "B", "gift_name": "舰长", "num": 1,
        "level": 3, "price": 198000, "timestamp": 1012}})
    await sess._on_event("SEND_GIFT", {"data": "坏数据"})  # 不应抛错


asyncio.run(xml_test())
sess._xml_fh.close()
xml_body = xml_tmp.read_text(encoding="utf-8").strip().splitlines()
line = xml_body[0] if xml_body else ""
check("XML p 字段顺序 stime,mode,size,color,date,pool,uid_hash,dmid",
      line.startswith('<d p="5.123,1,25,16777215,1005123,0,6089ca9d,-2014150320"')
      or line.startswith('<d p="5.123,1,25,16777215,1005123,0,6089ca9d'),
      line[:70])
check("XML 含 uid/user 属性且文本转义", 'uid="42"' in line
      and "user=" in line and "&lt;&gt;&amp;" in line)
check("坏数据被跳过不计数", sess.danmaku_count == 1)
sc = [l for l in xml_body if l.startswith("<sc ")]
gf = [l for l in xml_body if l.startswith("<gift ")]
gd = [l for l in xml_body if l.startswith("<guard ")]
check("XML <sc> 超级弹幕节点", len(sc) == 1 and 'ts="10.000"' in sc[0]
      and 'price="30"' in sc[0] and "你好&lt;x&gt;&amp;" in sc[0],
      sc[0][:80] if sc else "")
check("XML <gift> 礼物节点", len(gf) == 1 and 'giftname="小心心"' in gf[0]
      and 'count="3"' in gf[0] and 'total_coin="150"' in gf[0])
check("XML <guard> 上舰节点", len(gd) == 1 and 'level="3"' in gd[0]
      and 'user="B"' in gd[0])
xml_tmp.unlink(missing_ok=True)

from app.core.recorder import ffmpeg_bin  # noqa: E402
_fb = ffmpeg_bin()
check("ffmpeg 自动探测（PATH 或 imageio-ffmpeg）", Path(_fb).exists(), _fb)

# ---------- 分段判定 ----------
sp = RecordingSession.__new__(RecordingSession)
sp.split_mode, sp.split_arg = "time", 30
sp.stop_requested = False
sp._stopped_event = asyncio.Event()
sp.segment_started_at = time.time() - 31 * 60
check("分段-按时间到期触发", sp._split_due())
sp.segment_started_at = time.time()
check("分段-按时间未到期", not sp._split_due())
sp.split_mode = "none"
check("分段-none 不触发", sp._split_due() is False)
szs = RecordingSession.__new__(RecordingSession)
szs.split_mode, szs.split_arg = "size", 1
szs.flv_path = Path(_cfg.DATA_DIR) / "big.flv"
check("分段-size 模式无文件不误触发", not szs._split_due())
szs.flv_path.write_bytes(b"0" * (1024 * 1024 + 1))
check("分段-按大小到期触发", szs._split_due())
szs.flv_path.unlink()

_db.close()

# ---------- 3. 真实接口（只读） ----------
print("== live api (read-only) ==")


async def net():
    api = BiliLiveAPI()
    try:
        info = await api.get_room_info(21452505)
        check("get_room_info", info.room_id > 0,
              f"room={info.room_id} status={info.live_status} title={info.title[:20]!r}")
        await api.ensure_buvid()
        check("ensure_buvid", bool(api.buvid), api.buvid[:20])
        hosts, token = await api.get_danmu_info(info.room_id)
        check("get_danmu_info", bool(hosts) and bool(token),
              hosts[0]["host"] if hosts else "")
        if info.live_status == 1:
            cands = await api.get_stream_urls(info.room_id, 10000)
            check("get_stream_urls(living room)", len(cands) > 0,
                  f"{len(cands)} candidates, first fmt={cands[0].fmt}" if cands else "")
        else:
            check("get_stream_urls skipped(room offline)", True,
                  "换正在直播的房间可验证此项")
        login = await api.check_login()
        check("check_login(no cookie)", login["logged_in"] is False)
    except Exception as e:
        check("network tests", False, repr(e))
    finally:
        await api.aclose()


asyncio.run(net())

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
