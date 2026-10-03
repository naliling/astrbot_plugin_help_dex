import json
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

# 部位名 -> (默认值, 是否渐变双值)。
# 默认浅色亮蓝风：近白底 + 鲜艳蓝点缀，简洁不花哨。
COLOR_KEYS: Dict[str, Tuple[List[str], bool]] = {
    "背景": (["#EEF5FF", "#D9E7FF"], True),
    "标题": (["#0C1B33"], False),
    "副标题": (["#54658A"], False),
    "区块": (["#1E2F55"], False),
    "指令": (["#2563EB"], False),
    "描述": (["#5A6B85"], False),
    "强调": (["#3B82F6"], False),
    "卡片": (["#FFFFFF"], False),
    "边框": (["#C9DCF7"], False),
    "页脚": (["#8794B3"], False),
}

BACKGROUND_FILE = "background.img"


def background_file_name(bot_id: Optional[str]) -> str:
    """每个 bot 一份背景图：background-<bot>.img；全局背景是 background.img。

    bot_id 只保留字母数字下划线连字符，其余字符剔除（防路径穿越）；
    剔完为空就当全局处理。
    """
    if not bot_id:
        return BACKGROUND_FILE
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(bot_id))
    if not safe:
        return BACKGROUND_FILE
    return f"background-{safe}.img"
LOGO_FILE = "logo.img"
STYLE_FILE = "style.json"

VALID_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def parse_hex(value: str) -> Optional[Tuple[int, int, int, int]]:
    """把 #RGBHex / #RRGGBBAA 解析成 RGBA 元组，不合法返回 None。"""
    text = (value or "").strip()
    match = _HEX_RE.match(text)
    if not match:
        return None
    hex_str = match.group(1)
    r, g, b = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
    a = int(hex_str[6:8], 16) if len(hex_str) == 8 else 255
    return (r, g, b, a)


class StyleStore:
    """管理帮助图的自定义样式：颜色、标题、背景图、Logo。

    数据落在 data/plugin_data/help_dex/，插件升级不会丢失。
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.colors: Dict[str, List[str]] = {}
        self.title_override: Optional[str] = None
        self.subtitle_override: Optional[str] = None
        self._load()

    # -------------------- 持久化 --------------------
    def _style_path(self) -> Path:
        return self.data_dir / STYLE_FILE

    def _load(self) -> None:
        try:
            raw = json.loads(self._style_path().read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        colors = raw.get("colors") or {}
        if isinstance(colors, dict):
            for key in COLOR_KEYS:
                value = colors.get(key)
                if isinstance(value, list) and value:
                    self.colors[key] = [str(v) for v in value]
        title = raw.get("title")
        self.title_override = str(title) if isinstance(title, str) and title.strip() else None
        subtitle = raw.get("subtitle")
        self.subtitle_override = (
            str(subtitle) if isinstance(subtitle, str) and subtitle.strip() else None
        )

    def _save(self) -> None:
        payload = {
            "colors": self.colors,
            "title": self.title_override,
            "subtitle": self.subtitle_override,
        }
        self._style_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -------------------- 颜色 --------------------
    def get_color(self, key: str) -> Tuple[int, int, int, int]:
        values = self.colors.get(key) or COLOR_KEYS[key][0]
        parsed = parse_hex(values[0])
        return parsed if parsed else (0, 0, 0, 255)

    def get_gradient(self) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
        defaults = COLOR_KEYS["背景"][0]
        values = self.colors.get("背景") or defaults
        start = parse_hex(values[0]) or parse_hex(defaults[0])
        end = parse_hex(values[1]) if len(values) > 1 else None
        end = end or parse_hex(defaults[1]) or start
        return start[:3], end[:3]

    def set_color(self, key: str, values: List[str]) -> Optional[str]:
        """设置颜色。返回错误信息，None 表示成功。"""
        if key not in COLOR_KEYS:
            return f"没有「{key}」这个部位"
        _, gradient = COLOR_KEYS[key]
        values = [v for v in (s.strip() for s in values) if v]
        if not values:
            return "缺少颜色值"
        if not gradient and len(values) > 1:
            return f"「{key}」只接受一个颜色值"
        if gradient and len(values) > 2:
            return "「背景」最多两个颜色值（渐变起点/终点）"
        parsed_all = [parse_hex(v) for v in values]
        if any(p is None for p in parsed_all):
            return "颜色格式不对，请用 #RRGGBB 或 #RRGGBBAA（如 #7C5CFF）"
        if gradient and len(values) == 1:
            values = values * 2
        self.colors[key] = values
        self._save()
        return None

    def reset_color(self, key: Optional[str] = None) -> None:
        if key is None:
            self.colors.clear()
        else:
            self.colors.pop(key, None)
        self._save()

    def color_summary(self) -> str:
        lines = []
        for key in COLOR_KEYS:
            values = self.colors.get(key)
            if values:
                lines.append(f"{key}：{' → '.join(values)}")
            else:
                defaults = COLOR_KEYS[key][0]
                lines.append(f"{key}：{' → '.join(defaults)}（默认）")
        return "\n".join(lines)

    # -------------------- 标题 / 简介 --------------------
    def effective_title(self, config_default: str) -> str:
        return self.title_override or config_default

    def effective_subtitle(self, config_default: str) -> str:
        return self.subtitle_override or config_default

    def set_title(self, text: Optional[str]) -> None:
        cleaned = (text or "").strip()
        self.title_override = cleaned or None
        self._save()

    def set_subtitle(self, text: Optional[str]) -> None:
        cleaned = (text or "").strip()
        self.subtitle_override = cleaned or None
        self._save()

    # -------------------- 背景图 / Logo --------------------
    def _store_image(self, src: str, filename: str) -> Optional[str]:
        src_path = Path(src)
        ext = src_path.suffix.lower()
        if ext not in VALID_EXTENSIONS:
            return f"不支持的图片格式 {ext or '（无后缀）'}，请用 png/jpg/webp"
        try:
            shutil.copyfile(src_path, self.data_dir / filename)
        except Exception as exc:
            return f"保存图片失败：{exc}"
        return None

    def set_background(self, src: str, bot_id: Optional[str] = None) -> Optional[str]:
        """存背景图。bot_id 为空存全局（所有没单独设过的 bot 的兜底）。"""
        return self._store_image(src, background_file_name(bot_id))

    def background_path(self, bot_id: Optional[str] = None) -> Optional[Path]:
        """取背景图。先看这个 bot 自己的，没有就回落到全局。"""
        if bot_id:
            path = self.data_dir / background_file_name(bot_id)
            if path.is_file():
                return path
        path = self.data_dir / BACKGROUND_FILE
        return path if path.is_file() else None

    def clear_background(self, bot_id: Optional[str] = None) -> bool:
        """删背景图，返回是否真的删掉了。bot_id 为空删全局。"""
        path = self.data_dir / background_file_name(bot_id)
        if path.is_file():
            path.unlink(missing_ok=True)
            return True
        return False

    def list_bot_backgrounds(self) -> List[str]:
        """已单独设过背景图的 bot 列表。"""
        found = []
        for path in sorted(self.data_dir.glob("background-*.img")):
            found.append(path.name[len("background-"):-len(".img")])
        return found

    def has_any_background(self) -> bool:
        return (self.data_dir / BACKGROUND_FILE).is_file() or bool(
            self.list_bot_backgrounds()
        )

    def clear_all_backgrounds(self) -> None:
        (self.data_dir / BACKGROUND_FILE).unlink(missing_ok=True)
        for path in self.data_dir.glob("background-*.img"):
            path.unlink(missing_ok=True)

    def set_logo(self, src: str) -> Optional[str]:
        return self._store_image(src, LOGO_FILE)

    def logo_path(self) -> Optional[Path]:
        path = self.data_dir / LOGO_FILE
        return path if path.is_file() else None

    def clear_logo(self) -> None:
        (self.data_dir / LOGO_FILE).unlink(missing_ok=True)

    def reset_all(self) -> None:
        self.colors.clear()
        self.title_override = None
        self.subtitle_override = None
        self._save()
        self.clear_all_backgrounds()
        self.clear_logo()
