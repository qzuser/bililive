"""录制文件名模板（宏命名与默认行为参考 BililiveRecorder 的 Templating 模块）。

模板用 {{ 宏 }} 占位；渲染结果按 '/' 分段清洗非法字符，允许生成子目录，
但禁止路径逃逸（.. 与绝对路径）。校验失败保存时返回错误；
运行期渲染失败回退默认模板（与录播姬的 fallback 行为一致）。

可用宏：
  roomId     房间号
  name       主播昵称
  title      直播间标题
  partIndex  分段序号（从 1 起）
  now        分段开始时间，配 date 过滤器使用，如 {{ now | date: "%Y-%m-%d-%H%M%S" }}
过滤器：
  date:"fmt"        strftime 格式化（仅 now）
  pad:N             数字左补零到 N 位
  truncate:N        截断到 N 字符
  default:"x"       值为空时使用 x
"""
import re
from datetime import datetime

DEFAULT_TEMPLATE = "{{ now | date: \"%Y-%m-%d_%H%M%S\" }}_{{ title }}"

MACROS = ("roomId", "name", "title", "partIndex", "now")

_TOKEN = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)(\s*\|\s*[^{}]+?)?\s*\}\}")
_UNSAFE_SEG = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Windows 保留设备名（CON/NUL/COM1…，含带扩展名形式），任意平台一律预防性加前缀
_WIN_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$")
_MAX_SEG_LEN = 120
_MAX_DEPTH = 5


class TemplateError(ValueError):
    pass


def uses_part_index(template: str) -> bool:
    return any(m.group(1) == "partIndex" for m in _TOKEN.finditer(template or ""))


def validate(template: str) -> str:
    """校验模板，非法时抛 TemplateError。"""
    t = (template or "").strip()
    if not t:
        raise TemplateError("模板不能为空，留空即恢复默认")
    if len(t) > 300:
        raise TemplateError("模板过长（上限 300 字符）")
    stripped = _TOKEN.sub("", t)
    if re.search(r"[{}]", stripped):
        raise TemplateError("存在无法解析的 { } 片段，仅支持 {{ 宏 }} 与 {{ 宏 | 过滤器:参数 }}")
    for m in _TOKEN.finditer(t):
        if m.group(1) not in MACROS:
            raise TemplateError(f"不支持的宏 {{{{{m.group(1)}}}}}，"
                                f"可用：{', '.join(MACROS)}")
        if m.group(2):
            _validate_filters(m.group(2).strip(), m.group(1))
    static = _TOKEN.sub("\x00", t)
    if static.count("/") >= _MAX_DEPTH:
        raise TemplateError(f"子目录层级过多（路径段上限 {_MAX_DEPTH} 级）")
    for seg in static.split("/"):
        if seg.replace("\x00", "").strip() == "..":
            raise TemplateError("模板静态部分不允许 .. 路径")
    # 渲染冒烟：确保宏取值与过滤器可用
    render(t, room_id=1, room_name="x", anchor_name="y",
           part_index=1, start_time=datetime(2026, 1, 1))
    return t


def _validate_filters(fchain: str, macro: str) -> None:
    for f in [x.strip() for x in fchain.split("|") if x.strip()]:
        name = f.split(":", 1)[0].strip()
        if name not in ("date", "pad", "truncate", "default"):
            raise TemplateError(f"不支持的过滤器 {name}，"
                                "可用：date / pad / truncate / default")
        if name == "date" and macro != "now":
            raise TemplateError("date 过滤器仅用于 now 宏")
        if name in ("pad",) and macro not in ("partIndex", "roomId"):
            raise TemplateError("pad 过滤器仅用于 partIndex / roomId 宏")
        if name in ("pad", "truncate") and ":" in f:
            val = f.split(":", 1)[1].strip()
            if not (val.isdigit() and 1 <= int(val) <= 9999):
                raise TemplateError(f"{name} 参数需为 1-9999 的数字：{f}")


def render(template: str, *, room_id: int, room_name: str, anchor_name: str,
           part_index: int, start_time: datetime | None = None) -> str:
    """渲染为相对路径（不含扩展名），各段已清洗。"""
    t0 = start_time or datetime.now()
    values = {
        "roomId": str(room_id),
        "name": anchor_name,
        "title": room_name,
        "partIndex": str(part_index),
        "now": t0.strftime("%Y-%m-%d %H:%M:%S"),
    }

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise TemplateError(f"不支持的宏 {{{{{key}}}}}")
        raw = values[key]
        for f in [x.strip() for x in (m.group(2) or "").split("|") if x.strip()]:
            name, _, arg = f.partition(":")
            arg = arg.strip().strip("\"'")
            if name == "date":
                try:
                    raw = t0.strftime(arg)
                except (ValueError, TypeError):
                    raise TemplateError(f"date 格式无效: {arg}")
            elif name == "pad":
                try:
                    raw = str(int(raw)).zfill(max(1, min(9, int(arg or 2))))
                except ValueError:
                    pass
            elif name == "truncate":
                raw = raw[:max(1, int(arg or 20))]
            elif name == "default":
                if not raw.strip():
                    raw = arg or f"room{room_id}"
        return raw

    rel = _TOKEN.sub(sub, template)
    segs = []
    for seg in rel.split("/"):
        seg = _UNSAFE_SEG.sub("_", seg)
        seg = re.sub(r"\s+", " ", seg).strip(" .")
        if seg in ("", "."):
            continue
        if seg == "..":
            raise TemplateError("渲染结果包含 ..，拒绝路径逃逸")
        if _WIN_RESERVED.match(seg):
            seg = "_" + seg
        segs.append(seg[:_MAX_SEG_LEN])
    if not segs:
        raise TemplateError("模板渲染结果为空（全部字符被清洗）")
    return "/".join(segs)
