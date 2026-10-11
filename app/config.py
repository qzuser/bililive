import os
from pathlib import Path

APP_NAME = "BiliLiveRecorder"
VERSION = "1.2.0"

DATA_DIR = Path(os.environ.get("BLR_DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "recorder.db"
RECORDINGS_DIR = DATA_DIR / "recordings"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"
LOG_LEVEL = os.environ.get("BLR_LOG_LEVEL", "info").strip().lower()

POLL_INTERVAL = int(os.environ.get("BLR_POLL_INTERVAL", "30"))
HTTP_TIMEOUT = float(os.environ.get("BLR_HTTP_TIMEOUT", "10"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 连续 N 次轮询确认下播才停止录制，避免瞬态误判
OFFLINE_CONFIRM_TIMES = 2
# 录制不足该秒数即中断时，换线路重试
MIN_RECORD_SECONDS_BEFORE_RETRY = 60
# 单场次连续重试上限
MAX_CONSECUTIVE_RETRIES = 10

QUALITY_MAP = {
    0: "自动",
    20000: "4K",
    10000: "原画",
    401: "蓝光(杜比)",
    400: "蓝光",
    250: "超清",
    150: "高清",
    75: "流畅",
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
