import sqlite3
import threading
import time
from typing import Any, Optional

from . import config

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

SPLIT_MODES = ("none", "time", "size")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    cover TEXT NOT NULL DEFAULT '',
    quality INTEGER NOT NULL DEFAULT 10000,
    enabled INTEGER NOT NULL DEFAULT 1,
    record_danmaku INTEGER NOT NULL DEFAULT 1,
    auto_convert_mp4 INTEGER NOT NULL DEFAULT 0,
    split_mode TEXT NOT NULL DEFAULT 'none',
    split_arg INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    start_ts REAL NOT NULL,
    end_ts REAL,
    video_file TEXT NOT NULL DEFAULT '',
    danmaku_file TEXT NOT NULL DEFAULT '',
    events_file TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    danmaku_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'recording'
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_recordings_room ON recordings(room_id, start_ts DESC);
"""


def init() -> None:
    global _conn
    with _lock:
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _migrate()
        _conn.commit()


def _migrate() -> None:
    """为旧数据库补齐新增列（已存在时忽略 OperationalError）。"""
    for col, ddl in (("split_mode", "TEXT NOT NULL DEFAULT 'none'"),
                     ("split_arg", "INTEGER NOT NULL DEFAULT 0")):
        try:
            _conn.execute(f"ALTER TABLE rooms ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    # 一次性迁移：自动转 MP4 改为默认关闭，存量房间保留 FLV 原始格式
    row = _conn.execute(
        "SELECT value FROM settings WHERE key='flv_default_off_mig'"
    ).fetchone()
    if not row:
        _conn.execute(
            "UPDATE rooms SET auto_convert_mp4=0 WHERE auto_convert_mp4=1")
        _conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) "
            "VALUES('flv_default_off_mig','1')")


def close() -> None:
    global _conn
    with _lock:
        if _conn:
            _conn.close()
            _conn = None


def _execute(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def _query(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


# ---------- rooms ----------

def list_rooms() -> list[dict[str, Any]]:
    return _query("SELECT * FROM rooms ORDER BY id")


def get_room(room_id: int) -> Optional[dict[str, Any]]:
    rows = _query("SELECT * FROM rooms WHERE room_id=?", (room_id,))
    return rows[0] if rows else None


def get_room_by_pk(pk: int) -> Optional[dict[str, Any]]:
    rows = _query("SELECT * FROM rooms WHERE id=?", (pk,))
    return rows[0] if rows else None


def add_room(room_id: int, name: str = "", title: str = "", cover: str = "",
             quality: int = 10000, split_mode: str = "none",
             split_arg: int = 0) -> int:
    cur = _execute(
        "INSERT OR REPLACE INTO rooms(room_id,name,title,cover,quality,enabled,"
        "record_danmaku,auto_convert_mp4,split_mode,split_arg,created_at) "
        "VALUES(?,?,?,?,?,1,1,0,?,?,?)",
        (room_id, name, title, cover, quality, split_mode, split_arg,
         time.time()),
    )
    return int(cur.lastrowid)


def update_room(room_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    _execute(f"UPDATE rooms SET {keys} WHERE room_id=?", (*fields.values(), room_id))


def delete_room(room_id: int) -> None:
    _execute("DELETE FROM rooms WHERE room_id=?", (room_id,))


# ---------- recordings ----------

def add_recording(room_id: int, title: str, start_ts: float,
                  video_file: str) -> int:
    cur = _execute(
        "INSERT INTO recordings(room_id,title,start_ts,video_file,status) "
        "VALUES(?,?,?,?,'recording')",
        (room_id, title, start_ts, video_file),
    )
    return int(cur.lastrowid)


def finish_recording(rid: int, *, status: str, end_ts: float, size: int = 0,
                     video_file: str = "", danmaku_file: str = "",
                     events_file: str = "", danmaku_count: int = 0) -> None:
    _execute(
        "UPDATE recordings SET status=?, end_ts=?, size=?, video_file=?, "
        "danmaku_file=?, events_file=?, danmaku_count=? WHERE id=?",
        (status, end_ts, size, video_file, danmaku_file, events_file,
         danmaku_count, rid),
    )


def list_recordings(limit: int = 50, offset: int = 0,
                    room_id: Optional[int] = None) -> list[dict[str, Any]]:
    if room_id is not None:
        return _query(
            "SELECT * FROM recordings WHERE room_id=? ORDER BY id DESC "
            "LIMIT ? OFFSET ?", (room_id, limit, offset))
    return _query("SELECT * FROM recordings ORDER BY id DESC LIMIT ? OFFSET ?",
                  (limit, offset))


def count_recordings(room_id: Optional[int] = None) -> int:
    if room_id is not None:
        rows = _query("SELECT COUNT(*) c FROM recordings WHERE room_id=?", (room_id,))
    else:
        rows = _query("SELECT COUNT(*) c FROM recordings")
    return int(rows[0]["c"])


def get_recording(rid: int) -> Optional[dict[str, Any]]:
    rows = _query("SELECT * FROM recordings WHERE id=?", (rid,))
    return rows[0] if rows else None


def delete_recording(rid: int) -> None:
    _execute("DELETE FROM recordings WHERE id=?", (rid,))


# ---------- settings ----------

def get_setting(key: str, default: str = "") -> str:
    rows = _query("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str) -> None:
    _execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
