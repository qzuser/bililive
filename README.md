# B站直播监控录制系统

自研的 B 站直播间自动监控录制工具，参考 [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) / [blrec](https://github.com/acgnhiki/blrec) 的协议实现。

## 功能特性

- **实时监控**：轮询自定义直播间列表（房间号或链接均可，自动解析短号），检测开播/下播
- **自动录制**：开播即自动启动 FFmpeg 以 FLV 原格式直录（`-c copy` 无损），下播自动停止并**保留 FLV 文件**；「自动转 MP4」按房间可选开启（**默认关闭**），开启时下播后转封装为 MP4（faststart）并删除原 FLV
- **弹幕同步录制**：接入直播弹幕 WebSocket 协议（protover=3，brotli 解压），生成标准 XML 弹幕文件（兼容 danmaku2ass 等工具）
- **互动事件入 XML**：超级留言(SC)、礼物、上舰等事件除 JSONL 外，同时按 [biliLive-tools](https://github.com/renmu123/biliLive-tools) 的格式写入弹幕 XML 的 `<sc>` / `<gift>` / `<guard>` 节点（`<d>` 弹幕节点保持不变，老工具不受影响），XML 头部附带房间元数据 `<metadata>`
- **高画质支持**：在设置中填入 B 站登录 Cookie（SESSDATA 等）后可录制原画/4K 画质，未登录自动回退到账号可看的最高画质
- **分段录制**（参考录播姬）：可按「不分段 / 每 N 分钟切一次 / 每 N MB 切一次」配置，系统设置为新房间默认值，每个房间也可单独调整；分段轮转不打断弹幕连接，每段独立收尾成文件（可选转 MP4）并出现在录制记录中
- **可配置文件名格式**（参考录播姬的 `{{ 宏 }}` 模板）：系统设置中自定义录像/弹幕文件命名，支持房间号、主播名、标题、时间等宏与过滤器，可生成分级子目录；留空即使用默认模板
- **Web 管理界面**：添加/删除房间、状态总览、手动"立即录制/停止"、画质/弹幕/分段/文件名设置、录制记录浏览与文件下载、Cookie 登录状态检测
- **失败自愈**：流中断自动重新取址分段续录、弹幕服务器自动轮换重连、WBI 风控自动刷新密钥
- **多平台运行**：Docker 部署到飞牛 NAS（fnOS）等容器环境，也可在 Windows 本机直接双击 `run_windows.bat` 运行（自动建虚拟环境、自动探测 ffmpeg），Linux/macOS 同样支持本地运行

## 快速开始（飞牛 NAS / Docker）

```bash
# 1. 上传本项目到 NAS（如 ~/apps/bilivilive）
# 2. 编辑 docker-compose.yml，把 ./data 改为 NAS 存储卷路径，如：
#    - /vol1/1000/media/recordings:/data
# 3. 构建并启动
docker compose up -d --build
# 4. 浏览器打开 http://<NAS_IP>:8000
```

首次使用：进入「⚙ 设置」粘贴 B 站 Cookie 并「检测登录状态」，然后添加直播间即可开始值守。

### Cookie 获取方法

电脑浏览器登录 [bilibili.com](https://www.bilibili.com) → F12 开发者工具 → Network → 任选一个请求的 Request Headers → 复制完整 `Cookie:` 值（至少包含 `SESSDATA`、`bili_jct`、`DedeUserID`）。

> Cookie 只保存在本机数据目录的 SQLite 中，仅用于访问 B 站接口。请注意：使用脚本录制高画质依赖账号权益，高频异常请求可能触发风控，建议用小号。

## Windows 直接运行（不依赖 Docker）

双击（或在 cmd 中运行）项目根目录的 `run_windows.bat`：脚本会自动创建 `.venv`、安装依赖、启动服务，随后浏览器打开 `http://127.0.0.1:8000` 即可使用。

- **ffmpeg 自动解析**：优先使用 `BLR_FFMPEG` 环境变量 → PATH 中的 `ffmpeg` → 自动安装/使用 `imageio-ffmpeg` 自带的二进制（bat 脚本会在检测不到 PATH ffmpeg 时自动装 `imageio-ffmpeg`），三者皆无才报错
- **优雅停止**：录制子进程在 Windows 上使用 `CREATE_NEW_PROCESS_GROUP` 启动，停止时发送 `CTRL_BREAK_EVENT` 让 ffmpeg 正常收尾写平 moov box（Linux/容器仍用 SIGINT）

## 本地运行（开发/调试）

要求 Python 3.11+，并在 PATH 中安装 ffmpeg。

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# .venv/bin/pip install -r requirements.txt        # Linux/macOS
.venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

冒烟测试（不启动服务、不录制，仅验证协议解析与只读接口）：

```bash
.venv/Scripts/python scripts/smoke_test.py
```

端到端录制验证（Docker 镜像内自动选在播房间，录 30 秒 + 转 MP4 + 弹幕收包，一次性运行）：

```bash
docker build -t bilivilive-recorder:test .
docker run --rm -v "%CD%:/work" -e BLR_DATA_DIR=/work/data_e2e \
    bilivilive-recorder:test python /work/scripts/e2e_docker_test.py

# 追加真实分段轮转验证（每 1 分钟切一段，多耗时约 2 分钟）：
docker run --rm -v "%CD%:/work" -e BLR_DATA_DIR=/work/data_e2e -e BLR_SPLIT_TEST=1 \
    bilivilive-recorder:test python /work/scripts/e2e_docker_test.py
```

> 构建提示：若 Docker 守护进程配置的 registry 镜像源失效（如 `registry.docker-cn.com` 已停服），导致 `FROM python:3.11-slim` 拉取超时，可改用国内可用镜像源构建：
>
> ```bash
> docker build --build-arg BASE_IMAGE=docker.1ms.run/library/python:3.11-slim -t bilivilive-recorder .
> ```
>
> 或在 Docker Desktop / 飞牛 Docker 设置的 registry-mirrors 中配置当前可用的镜像加速地址。

## 输出文件

```
data/
├── recorder.db                     # 房间配置 / 录制记录 / 设置
└── recordings/room<房间号>/
    ├── 2026-09-26_201500_<标题>.flv        # 视频（FLV 原格式保留；该房间开启自动转 MP4 时为 .mp4）
    ├── 2026-09-26_201500_<标题>.xml        # 弹幕+互动事件 XML（<d>弹幕 / <sc>SC / <gift>礼物 / <guard>上舰，兼容 danmaku2ass）
    └── 2026-09-26_201500_<标题>.events.jsonl  # 礼物/SC/上舰等互动事件原始 JSON（弹幕与无价值消息除外）
```

同一次开播若因断流重新续录，会按录制启动时间产生多个分段文件，在「录制记录」页可逐段下载。若文件名模板未使用 `partIndex` 宏，第 2 个及以后的分段会自动追加 `_p02`、`_p03` 序号后缀以防覆盖。

> 分段说明：到点后旧段优雅收尾（写完文件尾、转封装），新段立即续录，弹幕 XML 按分段各自计时；分段条件检查粒度约 5 秒，实际切换点可能晚几秒。房间的分段设置修改在下一次开始录制时生效。

## 文件名模板（系统设置 → 录制文件名格式）

参考 [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) 的命名宏风格，留空则用默认模板 `{{ now | date: "%Y-%m-%d_%H%M%S" }}_{{ title }}`。

支持的宏：

| 宏 | 说明 |
|---|---|
| `{{ roomId }}` | 直播间房间号 |
| `{{ name }}` | 主播昵称 |
| `{{ title }}` | 直播标题（开播时） |
| `{{ partIndex }}` | 分段序号（从 1 开始） |
| `{{ now }}` | 本段录制开始时间 |

支持的过滤器（`宏 | 过滤器`）：

| 过滤器 | 作用对象 | 说明 |
|---|---|---|
| `date: "strftime格式"` | `now` | 时间格式化，如 `{{ now \| date: "%Y%m%d-%H%M%S" }}` |
| `pad: N` | `roomId` / `partIndex` | 左补零到 N 位，如 `{{ partIndex \| pad: 3 }}` |
| `truncate: N` | 任意 | 截断到 N 个字符 |
| `default: "x"` | 任意 | 宏为空时使用占位值 x |

示例：`{{ roomId }}/{{ name }}/{{ now | date: "%Y%m%d-%H%M" }}_{{ title | truncate: 30 }}` 会把录像按房间号分目录存放。

约束（保存时服务端校验，无效则返回 400 并提示原因）：路径以 `/` 分级、最多 5 级；静态部分不允许 `..`；自动替换 Windows/Linux 非法文件名字符（`\/:*?"<>|` 等）；运行期模板渲染失败会自动回退默认模板继续录制，不影响录像。

## 目录结构

```
app/
├── main.py              # FastAPI 入口 + 生命周期
├── config.py            # 环境变量与常量
├── db.py                # SQLite 数据层
├── bilibili/
│   ├── api.py           # WBI 签名 / 房间信息 / 流地址 / 弹幕服务器
│   └── danmaku.py       # 弹幕 WebSocket 协议客户端
├── core/
│   ├── recorder.py      # FFmpeg 子进程管理 + FLV→MP4 转封装
│   ├── naming.py        # 文件名模板渲染与校验（{{ 宏 }} | 过滤器）
│   ├── session.py       # 一次录制会话（视频+弹幕同步落盘）
│   └── monitor.py       # 每房间监控循环，开播自动录/下播自动停
├── api/routes.py        # REST API
└── web/                 # 管理界面（原生 JS 单页）
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `BLR_DATA_DIR` | `./data`（容器内 `/data`） | 数据与录像目录 |
| `BLR_POLL_INTERVAL` | `30` | 开播检测间隔（秒，10–300，界面可改） |
| `BLR_HTTP_TIMEOUT` | `10` | B 站接口超时（秒） |
| `BLR_FFMPEG` | `ffmpeg` | ffmpeg 可执行文件路径 |

## 已知限制

- 未登录时最高只能取到账号可看的免登画质（通常 250 超清）；4K/付费画质需账号权益
- 轮播（重播）状态不计为开播，不自动录制；需要请用「立即录制」手动开录
- 断流重连续录会产生分段；暂不提供分段自动合并
- 本工具仅供个人学习备份使用，请遵守 B 站用户协议与相关法律法规
