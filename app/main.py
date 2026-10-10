import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .api.routes import router as api_router
from .core.monitor import MonitorManager

WEB_DIR = Path(__file__).parent / "web"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    config.ensure_dirs()
    db.init()
    manager = MonitorManager()
    app.state.manager = manager
    await manager.start_all()
    logging.getLogger("main").info(
        "%s v%s started, data dir: %s", config.APP_NAME, config.VERSION,
        config.DATA_DIR)
    try:
        yield
    finally:
        await manager.stop_all()
        await manager.api.aclose()
        db.close()


app = FastAPI(title=config.APP_NAME, version=config.VERSION,
              lifespan=lifespan)
config.ensure_dirs()
app.include_router(api_router)
# 只暴露录像目录，避免 data 下的数据库文件（含 Cookie）被直接下载
app.mount("/files", StaticFiles(directory=str(config.RECORDINGS_DIR)),
          name="files")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(WEB_DIR / "index.html"))
