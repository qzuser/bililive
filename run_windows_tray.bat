@echo off
rem B站直播录制系统 - Windows 托盘模式（无控制台窗口，服务在后台运行）
rem 启动后请在屏幕右下角通知区找粉色"B"图标：双击打开网页，右键菜单可暂停/恢复监控、刷新直播间、退出
cd /d %~dp0

if not exist .venv (
    echo [1/3] 创建虚拟环境...
    python -m venv .venv || py -3.11 -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat

echo [2/3] 安装依赖（含 ffmpeg 备用二进制）...
pip install -r requirements.txt -q
pip show imageio-ffmpeg >nul 2>&1 || pip install imageio-ffmpeg -q

echo [3/3] 后台启动，请在右下角托盘找粉色 B 图标（服务地址 http://localhost:8000）
start "" .venv\Scripts\pythonw.exe -m app.tray
goto :eof

:err
echo 未找到 Python，请先安装 Python 3.11+ 并加入 PATH
