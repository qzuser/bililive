@echo off
rem B站直播录制系统 - Windows 一键启动
rem 需要 Python 3.11+；ffmpeg 可选（未安装时自动使用 imageio-ffmpeg 自带二进制）
cd /d %~dp0

if not exist .venv (
    echo [1/3] 创建虚拟环境...
    python -m venv .venv || py -3.11 -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat

echo [2/3] 安装依赖（含 ffmpeg 备用二进制）...
pip install -r requirements.txt -q
pip show imageio-ffmpeg >nul 2>&1 || pip install imageio-ffmpeg -q

echo [3/3] 启动服务: http://localhost:8000
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
goto :eof

:err
echo 未找到 Python，请先安装 Python 3.11+ 并加入 PATH
