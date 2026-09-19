import json
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

# 部位名 -> (默认值, 是否渐变双值)。默认为暗色科幻风：深空底 + 沉稳蓝青点缀
COLOR_KEYS: Dict[str, Tuple[List[str], bool]] = {
    "背景": (["#0A0F1E", "#121D33"], True),
    "标题": (["#D9E4F5"], False),
    "副标题": (["#8CA0BE"], False),
    "区块": (["#C6D6EC"], False),
    "指令": (["#63BFE6"], False),
    "描述": (["#93A8C6"], False),
    "强调": (["#3E9BD6"], False),
    "卡片": (["#16223A"], False),
    "边框": (["#2C3D5C"], False),
    "页脚": (["#71849F"], False),
}

BACKGROUND_FILE = "background.img"
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

    def set_background(self, src: str) -> Optional[str]:
        return self._store_image(src, BACKGROUND_FILE)

    def background_path(self) -> Optional[Path]:
        path = self.data_dir / BACKGROUND_FILE
        return path if path.is_file() else None

    def clear_background(self) -> None:
        (self.data_dir / BACKGROUND_FILE).unlink(missing_ok=True)

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
        self.clear_background()
        self.clear_logo()
