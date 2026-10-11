import re
import shutil
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import config, db
from ..bilibili.api import BiliAPIError, BiliLiveAPI
from ..core import naming
from ..core.monitor import MonitorManager

router = APIRouter(prefix="/api")


def manager(request: Request) -> MonitorManager:
    return request.app.state.manager


def _norm(p: str) -> str:
    return p.replace("\\", "/") if p else ""


def _file_url(rel: str) -> str:
    """数据库存的是相对 DATA_DIR 的路径，转为 /files（RECORDINGS_DIR）相对路径。"""
    rel = _norm(rel)
    if rel.startswith("recordings/"):
        return rel[len("recordings/"):]
    return ""


def _parse_room_id(value: Any) -> int:
    s = str(value)
    m = re.search(r"live\.bilibili\.com/(\d+)", s)
    if m:
        return int(m.group(1))
    s = s.strip()
    if re.fullmatch(r"\d{1,12}", s):
        return int(s)
    raise HTTPException(400, "无法识别房间号，请输入数字或直播间链接")


def _validate_split(mode: str, arg: int | None) -> tuple[str, int]:
    if mode not in db.SPLIT_MODES:
        raise HTTPException(400, f"分段方式无效，可选 {db.SPLIT_MODES}")
    if mode == "none":
        return "none", 0
    try:
        arg = int(arg)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(400, "分段参数无效")
    if mode == "time":
        if not 1 <= arg <= 1440:
            raise HTTPException(400, "按时间分段需 1-1440 分钟")
    elif not 1 <= arg <= 51200:
        raise HTTPException(400, "按大小分段需 1-51200 MB")
    return mode, arg


def _default_split() -> tuple[str, int]:
    mode = db.get_setting("default_split_mode", "none")
    try:
        arg = int(db.get_setting("default_split_arg", "0") or 0)
    except ValueError:
        arg = 0
    if mode not in db.SPLIT_MODES:
        mode = "none"
    return mode, arg


# ---------- schemas ----------

class RoomCreate(BaseModel):
    room_id: str | int
    quality: int = 10000
    split_mode: Optional[str] = None
    split_arg: Optional[int] = None


class RoomPatch(BaseModel):
    enabled: Optional[int] = None
    quality: Optional[int] = None
    record_danmaku: Optional[int] = None
    auto_convert_mp4: Optional[int] = None
    split_mode: Optional[str] = None
    split_arg: Optional[int] = None


class SettingsPut(BaseModel):
    cookie: Optional[str] = None
    poll_interval: Optional[int] = None
    default_split_mode: Optional[str] = None
    default_split_arg: Optional[int] = None
    filename_format: Optional[str] = None


class CookieCheck(BaseModel):
    cookie: str = ""


# ---------- status / rooms ----------

@router.get("/status")
async def get_status(request: Request):
    mgr = manager(request)
    snap = mgr.snapshot()
    usage = shutil.disk_usage(config.DATA_DIR)
    return {
        "version": config.VERSION,
        "rooms_total": len(snap),
        "living": sum(1 for s in snap.values() if s["living"]),
        "recording": sum(1 for s in snap.values() if s["recording"]),
        "disk_free": usage.free,
        "disk_total": usage.total,
        "poll_interval": mgr.poll_interval,
        "cookie_set": bool(db.get_setting("cookie")),
    }


@router.get("/rooms")
async def list_rooms(request: Request):
    mgr = manager(request)
    snap = mgr.snapshot()
    rooms = db.list_rooms()
    for r in rooms:
        r["runtime"] = snap.get(r["room_id"], {
            "status": "stopped", "living": False, "recording": False,
            "error": "", "rec_seconds": None, "last_check": 0})
    return rooms


@router.post("/rooms")
async def create_room(request: Request, body: RoomCreate):
    mgr = manager(request)
    room_id = _parse_room_id(body.room_id)
    try:
        info = await mgr.api.get_room_info(room_id)
    except BiliAPIError as e:
        if e.code in (60004, 1002004, -404):
            raise HTTPException(404, f"房间 {room_id} 不存在")
        raise HTTPException(400, f"查询房间失败：{e.message}")
    except Exception as e:
        raise HTTPException(400, f"查询房间失败：{e}")
    if body.split_mode is None or body.split_arg is None:
        dm, da = _default_split()
        mode = body.split_mode if body.split_mode is not None else dm
        arg = body.split_arg if body.split_arg is not None else da
    else:
        mode, arg = body.split_mode, body.split_arg
    mode, arg = _validate_split(mode, arg)
    db.add_room(info.room_id, name=info.name, title=info.title,
                cover=info.cover, quality=body.quality,
                split_mode=mode, split_arg=arg)
    mgr.spawn(info.room_id)
    return {"ok": True, "room_id": info.room_id, "title": info.title}


@router.patch("/rooms/{room_id}")
async def patch_room(request: Request, room_id: int, body: RoomPatch):
    room = db.get_room(room_id)
    if not room:
        raise HTTPException(404, "房间不在监控列表中")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "split_mode" in fields or "split_arg" in fields:
        mode, arg = _validate_split(
            fields.get("split_mode", room.get("split_mode", "none")),
            fields.get("split_arg", room.get("split_arg", 0)))
        fields["split_mode"], fields["split_arg"] = mode, arg
    if fields:
        db.update_room(room_id, **fields)
    return {"ok": True}


@router.delete("/rooms/{room_id}")
async def delete_room(request: Request, room_id: int):
    mgr = manager(request)
    room = db.get_room(room_id)
    if not room:
        raise HTTPException(404, "房间不在监控列表中")
    await mgr.remove(room_id)
    db.delete_room(room_id)
    return {"ok": True}


@router.post("/rooms/{room_id}/record")
async def record_now(request: Request, room_id: int):
    mgr = manager(request)
    if not db.get_room(room_id):
        raise HTTPException(404, "请先添加该房间")
    try:
        await mgr.force_record(room_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/rooms/{room_id}/stop")
async def stop_record(request: Request, room_id: int):
    mgr = manager(request)
    mgr.force_stop(room_id)
    return {"ok": True}


# ---------- recordings ----------

@router.get("/recordings")
async def list_recordings(request: Request, limit: int = 30, offset: int = 0,
                          room_id: Optional[int] = None):
    limit = max(1, min(200, limit))
    items = db.list_recordings(limit, offset, room_id)
    total = db.count_recordings(room_id)
    for it in items:
        it["video_url"] = ""
        vf = _norm(it.get("video_file", ""))
        if vf and (config.DATA_DIR / vf).exists():
            it["video_url"] = _file_url(vf)
        dm = _norm(it.get("danmaku_file", ""))
        if dm and (config.DATA_DIR / dm).exists():
            it["danmaku_url"] = _file_url(dm)
        ev = _norm(it.get("events_file", ""))
        if ev and (config.DATA_DIR / ev).exists():
            it["events_url"] = _file_url(ev)
    return {"total": total, "items": items}


@router.delete("/recordings/{rid}")
async def delete_recording(request: Request, rid: int, delete_files: int = 0):
    rec = db.get_recording(rid)
    if not rec:
        raise HTTPException(404, "记录不存在")
    if rec["status"] == "recording":
        raise HTTPException(400, "该记录正在录制中，无法删除")
    if delete_files:
        for key in ("video_file", "danmaku_file", "events_file"):
            rel = _norm(rec.get(key, ""))
            if rel:
                p = config.DATA_DIR / rel
                if p.exists():
                    p.unlink()
        # 视频分段所在目录若已空则一并清理
        vf = _norm(rec.get("video_file", ""))
        if vf:
            d = (config.DATA_DIR / vf).parent
            try:
                d.rmdir()
            except OSError:
                pass
    db.delete_recording(rid)
    return {"ok": True}


# ---------- settings ----------

@router.get("/settings")
async def get_settings():
    mode, arg = _default_split()
    return {
        "cookie": db.get_setting("cookie"),
        "poll_interval": int(db.get_setting("poll_interval",
                                            str(config.POLL_INTERVAL))),
        "default_split_mode": mode,
        "default_split_arg": arg,
        "filename_format": db.get_setting("filename_format", ""),
        "filename_format_default": naming.DEFAULT_TEMPLATE,
    }


@router.put("/settings")
async def put_settings(request: Request, body: SettingsPut):
    mgr = manager(request)
    if body.cookie is not None:
        db.set_setting("cookie", body.cookie.strip())
        mgr.update_cookie(body.cookie.strip())
    if body.poll_interval is not None:
        pi = max(10, min(300, int(body.poll_interval)))
        db.set_setting("poll_interval", str(pi))
        mgr.update_poll_interval(pi)
    if body.default_split_mode is not None or body.default_split_arg is not None:
        cur_mode, cur_arg = _default_split()
        mode, arg = _validate_split(
            body.default_split_mode if body.default_split_mode is not None
            else cur_mode,
            body.default_split_arg if body.default_split_arg is not None
            else cur_arg)
        db.set_setting("default_split_mode", mode)
        db.set_setting("default_split_arg", str(arg))
    if body.filename_format is not None:
        fmt = body.filename_format.strip()
        if fmt:
            try:
                naming.validate(fmt)
            except naming.TemplateError as e:
                raise HTTPException(400, f"文件名模板无效：{e}")
        db.set_setting("filename_format", fmt)
    return {"ok": True}


@router.post("/settings/cookie_check")
async def cookie_check(body: CookieCheck):
    api = BiliLiveAPI(cookie=body.cookie)
    try:
        result = await api.check_login()
        return result or {"logged_in": False, "message": "接口无返回"}
    except Exception as e:
        return {"logged_in": False, "message": str(e)}
    finally:
        await api.aclose()
