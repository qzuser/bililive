"""Docker 容器内端到端录制验证（一次性运行，非常驻服务）。

在镜像内执行：选一个正在直播的房间 -> ffmpeg 录制 30 秒 -> 停止 ->
转封装 MP4 -> 校验文件大小 -> 弹幕 WS 收包统计 -> 生成 XML 校验。

用法（宿主机项目根目录）：
  docker build -t bilivilive-recorder:test .
  docker run --rm -v "%CD%:/work" -e BLR_DATA_DIR=/work/data_e2e \
      bilivilive-recorder:test python /work/scripts/e2e_docker_test.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.environ.get("BLR_WORK",
                                  "/work" if Path("/work/app").exists()
                                  else str(Path(__file__).resolve().parent.parent)))

from app import config, db  # noqa: E402
from app.bilibili.api import BiliLiveAPI  # noqa: E402
from app.bilibili.danmaku import DanmakuClient  # noqa: E402
from app.core.recorder import VideoRecorder, remux_ts_to_mp4  # noqa: E402
from app.core.session import RecordingSession  # noqa: E402

RECORD_SECONDS = 30


async def find_live_room(api: BiliLiveAPI) -> int:
    import httpx
    async with httpx.AsyncClient(timeout=10, headers=api._base_headers()) as c:
        resp = await c.get(
            "https://api.live.bilibili.com/room/v3/area/getRoomList",
            params={"platform": "web", "parent_area_id": 2, "cate_id": 0,
                    "area_id": 0, "sort_type": "online", "page": 1,
                    "page_size": 30})
        for item in resp.json()["data"]["list"]:
            rid = item["roomid"]
            try:
                info = await api.get_room_info(rid)
            except Exception:
                continue
            if info.live_status == 1:
                print(f"picked live room {rid} ({info.title[:20]})")
                return rid
    raise SystemExit("no living room found")


async def main() -> int:
    config.ensure_dirs()
    db.init()
    api = BiliLiveAPI()
    room_id = await find_live_room(api)

    # ---- 视频链路 ----
    cands = await api.get_stream_urls(room_id, 10000)
    flv = [c for c in cands if c.fmt == "flv"] or cands
    if not flv:
        print("FAIL: no stream candidates")
        return 1
    url = flv[0].url
    print(f"recording flv qn={flv[0].quality} host={urlparse(url).hostname}")
    out = config.RECORDINGS_DIR / f"e2e_{room_id}.ts"
    headers = {"User-Agent": config.USER_AGENT,
               "Referer": f"https://live.bilibili.com/{room_id}"}
    rec = VideoRecorder(url, out, headers)
    await rec.start()
    t0 = time.time()
    stopped = False
    try:
        await asyncio.wait_for(rec.wait_finished(), RECORD_SECONDS)
    except asyncio.TimeoutError:
        stopped = True
        await rec.stop()
    dur = time.time() - t0
    ts_size = out.stat().st_size if out.exists() else 0
    print(f"ts recorded: {ts_size} bytes in {dur:.1f}s "
          f"({'timed stop' if stopped else 'ffmpeg exited early'})")
    if ts_size < 100_000:
        print("FAIL: ts too small, stream record broken")
        print("stderr:", rec.error_hint())
        return 1

    mp4 = config.RECORDINGS_DIR / f"e2e_{room_id}.mp4"
    ok = await remux_ts_to_mp4(out, mp4)
    mp4_size = mp4.stat().st_size if ok else 0
    print(f"remux mp4: {'OK' if ok else 'FAIL'} {mp4_size} bytes")
    if not ok:
        return 1

    # ---- 弹幕链路 ----
    hosts, token = await api.get_danmu_info(room_id)
    count = {"dm": 0, "ev": 0}

    async def on_dm(item):
        count["dm"] += 1

    async def on_ev(cmd, payload):
        count["ev"] += 1

    client = DanmakuClient(room_id, hosts, token, buvid=api.buvid,
                           on_danmaku=on_dm, on_event=on_ev)
    task = asyncio.create_task(client.run_forever())
    await asyncio.sleep(15)
    client.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    print(f"danmaku received: {count['dm']} dm, {count['ev']} events")
    if count["dm"] + count["ev"] == 0:
        print("WARN: no danmaku in 15s (房间可能弹幕少)")

    # ---- 清理测试文件 ----
    out.unlink(missing_ok=True)
    mp4.unlink(missing_ok=True)

    # ---- 分段轮转（RecordingSession 全链路，需 BLR_SPLIT_TEST=1，约 2 分钟）----
    if os.environ.get("BLR_SPLIT_TEST") == "1":
        ok = await split_session_test(api, room_id)
        if not ok:
            await api.aclose()
            return 1

    await api.aclose()
    print("E2E PASS")
    return 0


async def split_session_test(api: BiliLiveAPI, room_id: int) -> bool:
    """按时间分段=1 分钟，录 ~100 秒，验证生成 2 个以上分段与独立 DB 记录。"""
    cands = await api.get_stream_urls(room_id, 10000)
    flv = [c for c in cands if c.fmt == "flv"] or cands
    if not flv:
        print("FAIL: split test no stream candidates")
        return False
    sess = RecordingSession(api, room_id, "E2E 分段测试", flv[0], 0.0,
                            record_danmaku=True, auto_convert_mp4=False,
                            split_mode="time", split_arg=1)
    await sess.start()
    deadline = time.time() + 110
    while time.time() < deadline and sess._seg_index < 2:
        await asyncio.sleep(2)
    segs = sess._seg_index
    sess.request_stop()
    await sess.finalize()
    rows = [r for r in db.list_recordings(limit=50, room_id=room_id)]
    ok_rows = [r for r in rows if r["status"] == "completed"
               and r["size"] > 50_000]
    print(f"split test: segments_rotated={segs}, completed_rows={len(ok_rows)}")
    result = segs >= 2 and len(ok_rows) >= 2
    for r in rows:  # 清理
        for key in ("video_file", "danmaku_file", "events_file"):
            rel = r.get(key) or ""
            if rel:
                (config.DATA_DIR / rel).unlink(missing_ok=True)
        db.delete_recording(r["id"])
    if not result:
        print("FAIL: split rotation broken "
              "(need rotated>=2 and completed rows with size>50KB)")
    return result


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
