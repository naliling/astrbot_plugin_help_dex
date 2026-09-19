import io
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .style import StyleStore

FONT_PATH = os.path.join(os.path.dirname(__file__), "DouyinSansBold.otf")

WIDTH = 900
PADDING = 40
HEADER_GAP = 26
ACCENT_BAR_WIDTH = 8
ACCENT_TEXT_GAP = 18
LOGO_HEIGHT = 76

SECTION_MARKER_SIZE = 16
SECTION_MARKER_GAP = 12
SECTION_GAP_AFTER_HEADER = 12
SECTION_GAP = 24

PANEL_PAD_X = 16
PANEL_PAD_Y = 4
CELL_GAP = 12
CELL_PAD_X = 12
CELL_PAD_Y = 10
CELL_CMD_DESC_GAP = 6
CELL_RADIUS = 10

FOOTER_HEIGHT = 52

BG_IMAGE_OVERLAY_ALPHA = 60
LOGO_WHITE_THRESHOLD = 235
GRID_STEP = 64
GRID_COLOR = (110, 150, 200, 7)


def _read_metadata_value(field: str) -> str:
    metadata_path = Path(__file__).resolve().with_name("metadata.yaml")
    try:
        for line in metadata_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{field}:"):
                return stripped.split(":", 1)[1].split("#", 1)[0].strip().strip("\"'")
    except Exception:
        pass
    return ""


class HelpImageRenderer:
    def __init__(self, config, style: StyleStore):
        self.config = config
        self.style = style
        self.display_name = _read_metadata_value("display_name") or "指令图鉴"
        version = _read_metadata_value("version")
        self.version = version[1:] if version.lower().startswith("v") else version or "0.1.0"
        self.author = _read_metadata_value("author") or "娜莉灵"

        self.font_title = ImageFont.truetype(FONT_PATH, 40)
        self.font_subtitle = ImageFont.truetype(FONT_PATH, 20)
        self.font_section = ImageFont.truetype(FONT_PATH, 24)
        self.font_cmd = ImageFont.truetype(FONT_PATH, 21)
        self.font_desc = ImageFont.truetype(FONT_PATH, 16)
        self.font_footer = ImageFont.truetype(FONT_PATH, 13)

        self.title_text = style.effective_title(
            str(getattr(config, "title_help", "") or "").strip() or "指令图鉴"
        )
        self.subtitle_text = style.effective_subtitle(
            str(getattr(config, "title_desc", "") or "").strip() or "这里收录了我会的所有指令"
        )
        self.logo = self._load_logo()

    # -------------------- 素材 --------------------
    def _load_logo(self) -> Optional[Image.Image]:
        path = self.style.logo_path()
        if not path:
            return None
        try:
            logo = Image.open(path).convert("RGBA")
        except Exception:
            return None
        if logo.mode == "RGB" or not self._has_real_alpha(logo):
            logo = self._remove_near_white(logo)
        ow, oh = logo.size
        new_w = max(1, int(LOGO_HEIGHT * ow / oh))
        return logo.resize((new_w, LOGO_HEIGHT), Image.Resampling.LANCZOS)

    @staticmethod
    def _has_real_alpha(img: Image.Image) -> bool:
        alpha = img.getchannel("A")
        return alpha.getextrema()[0] < 250

    @staticmethod
    def _remove_near_white(img: Image.Image) -> Image.Image:
        r, g, b, a = img.split()
        threshold = LOGO_WHITE_THRESHOLD

        def bright(channel):
            return channel.point(lambda v: 255 if v >= threshold else 0)

        white_mask = ImageChops.multiply(
            ImageChops.multiply(bright(r), bright(g)), bright(b)
        )
        keep = ImageChops.subtract(a, white_mask)
        img.putalpha(keep)
        return img

    # -------------------- 排版辅助 --------------------
    @staticmethod
    def _text_size(text: str, font) -> Tuple[int, int]:
        bbox = font.getbbox(text or " ")
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    def _wrap_by_width(self, text: str, font, max_width: int) -> List[str]:
        text = (text or "").strip()
        if not text:
            return []
        lines: List[str] = []
        current = ""
        for char in text:
            if char == "\n":
                if current:
                    lines.append(current)
                current = ""
                continue
            candidate = current + char
            if self._text_size(candidate, font)[0] > max_width and current:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
        # 避免末行只剩一个字（如「大事」被劈成「大/事」）
        if len(lines) >= 2 and len(lines[-1]) == 1 and len(lines[-2]) > 1:
            lines[-1] = lines[-2][-1] + lines[-1]
            lines[-2] = lines[-2][:-1]
        return lines

    @staticmethod
    def _rounded_rect(draw, xy, radius, fill=None, outline=None, width=1):
        x1, y1, x2, y2 = xy
        if x1 >= x2 or y1 >= y2:
            return
        radius = min(radius, (x2 - x1) // 2, (y2 - y1) // 2)
        draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)

    # -------------------- 布局 --------------------
    def _header_height(self) -> int:
        title_h = self._text_size(self.title_text, self.font_title)[1]
        subtitle_h = self._text_size(self.subtitle_text, self.font_subtitle)[1]
        text_block = title_h + 10 + subtitle_h
        block = max(text_block, LOGO_HEIGHT if self.logo else 0)
        return PADDING + block + HEADER_GAP

    def _grid_columns(self) -> int:
        try:
            cols = int(getattr(self.config, "grid_columns", 3) or 3)
        except (TypeError, ValueError):
            cols = 3
        return max(1, min(4, cols))

    def _card_opacity(self) -> int:
        try:
            opacity = int(getattr(self.config, "card_opacity", 80) or 80)
        except (TypeError, ValueError):
            opacity = 80
        return max(20, min(100, opacity))

    def _layout(self, sections) -> Tuple[List[dict], int]:
        ops: List[dict] = []
        y = self._header_height()

        panel_width = WIDTH - PADDING * 2
        cols = self._grid_columns()
        cell_w = (panel_width - PANEL_PAD_X * 2 - CELL_GAP * (cols - 1)) // cols
        text_w = cell_w - CELL_PAD_X * 2
        cmd_line_h = self._text_size("指", self.font_cmd)[1] + 5
        desc_line_h = self._text_size("字", self.font_desc)[1] + 6

        for section_name, cmds in sections:
            # 区块标题行
            marker_y = y + (self._text_size(section_name, self.font_section)[1] - SECTION_MARKER_SIZE) // 2
            ops.append({"op": "section", "name": section_name, "y": y, "marker_y": marker_y})
            y += self._text_size(section_name, self.font_section)[1] + SECTION_GAP_AFTER_HEADER

            # 指令卡片流入网格：每行 cols 张，图片高度随栏数倍减
            cells = []
            for cmd, desc in cmds:
                cmd_lines = self._wrap_by_width(cmd, self.font_cmd, text_w)
                desc_lines = self._wrap_by_width(desc or "", self.font_desc, text_w)
                cmd_h = len(cmd_lines) * cmd_line_h
                desc_h = len(desc_lines) * desc_line_h if desc_lines else 0
                content_h = cmd_h + (CELL_CMD_DESC_GAP + desc_h if desc_lines else 0)
                cells.append(
                    {"cmd_lines": cmd_lines, "desc_lines": desc_lines, "content_h": content_h}
                )

            grid_rows = [cells[i : i + cols] for i in range(0, len(cells), cols)]
            row_heights = [
                max(c["content_h"] for c in row) + CELL_PAD_Y * 2 for row in grid_rows
            ]
            panel_h = (
                PANEL_PAD_Y * 2
                + sum(row_heights)
                + CELL_GAP * (len(row_heights) - 1)
            )
            ops.append(
                {
                    "op": "panel",
                    "y": y,
                    "height": panel_h,
                    "cell_w": cell_w,
                    "cols": cols,
                    "grid_rows": grid_rows,
                    "row_heights": row_heights,
                }
            )
            y += panel_h + SECTION_GAP

        return ops, y

    # -------------------- 绘制 --------------------
    def _make_background(self, height: int) -> Image.Image:
        start, end = self.style.get_gradient()
        bg_path = self.style.background_path()
        if bg_path:
            try:
                bg = Image.open(bg_path).convert("RGB")
                ow, oh = bg.size
                scale = max(WIDTH / ow, height / oh)
                new_w, new_h = int(ow * scale + 0.5), int(oh * scale + 0.5)
                bg = bg.resize((new_w, new_h), Image.Resampling.LANCZOS)
                left = (new_w - WIDTH) // 2
                top = (new_h - height) // 2
                canvas = bg.crop((left, top, left + WIDTH, top + height)).convert("RGBA")
                # 蒙版颜色跟随主题明暗：暗色主题叠深色蒙版，浅色主题叠白色蒙版
                luminance = 0.299 * start[0] + 0.587 * start[1] + 0.114 * start[2]
                overlay_color = (255, 255, 255) if luminance >= 128 else (10, 16, 28)
                overlay = Image.new(
                    "RGBA", canvas.size, overlay_color + (BG_IMAGE_OVERLAY_ALPHA,)
                )
                return Image.alpha_composite(canvas, overlay)
            except Exception:
                pass
        canvas = Image.new("RGBA", (WIDTH, height))
        draw = ImageDraw.Draw(canvas)
        for row in range(height):
            t = row / max(height - 1, 1)
            color = tuple(int(start[i] + (end[i] - start[i]) * t) for i in range(3))
            draw.line([(0, row), (WIDTH, row)], fill=color)
        # 极淡的网格纹理，加一层科技底噪
        for gx in range(0, WIDTH, GRID_STEP):
            draw.line([(gx, 0), (gx, height)], fill=GRID_COLOR)
        for gy in range(0, height, GRID_STEP):
            draw.line([(0, gy), (WIDTH, gy)], fill=GRID_COLOR)
        return canvas

    def _card_fill(self) -> Tuple[int, int, int, int]:
        r, g, b, a = self.style.get_color("卡片")
        if a >= 255:
            a = int(255 * self._card_opacity() / 100)
        return (r, g, b, a)

    def render(self, sections) -> bytes:
        ops, content_bottom = self._layout(sections)
        total_height = content_bottom + FOOTER_HEIGHT

        img = self._make_background(total_height)
        draw = ImageDraw.Draw(img)
        has_bg = self.style.background_path() is not None
        card_fill = self._card_fill()
        chip_fill = (
            card_fill[0],
            card_fill[1],
            card_fill[2],
            max(card_fill[3], 200),
        )

        title_color = self.style.get_color("标题")[:3]
        subtitle_color = self.style.get_color("副标题")[:3]
        accent = self.style.get_color("强调")[:3]
        accent_rgba = self.style.get_color("强调")

        title_h = self._text_size(self.title_text, self.font_title)[1]
        subtitle_h = self._text_size(self.subtitle_text, self.font_subtitle)[1]
        text_block = title_h + 10 + subtitle_h
        header_block = max(text_block, LOGO_HEIGHT if self.logo else 0)

        bar_top = PADDING
        bar_bottom = PADDING + header_block
        # 强调条柔光：先在独立图层上画再高斯模糊，避免刺眼硬边
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.rounded_rectangle(
            (PADDING - 5, bar_top - 5, PADDING + ACCENT_BAR_WIDTH + 5, bar_bottom + 5),
            radius=10,
            fill=(accent_rgba[0], accent_rgba[1], accent_rgba[2], 90),
        )
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(8)))
        draw = ImageDraw.Draw(img)
        self._rounded_rect(
            draw,
            (PADDING, bar_top, PADDING + ACCENT_BAR_WIDTH, bar_bottom),
            radius=ACCENT_BAR_WIDTH // 2,
            fill=accent,
        )
        text_x = PADDING + ACCENT_BAR_WIDTH + ACCENT_TEXT_GAP
        text_top = PADDING + max(0, (header_block - text_block) // 2)
        if has_bg:
            title_w = self._text_size(self.title_text, self.font_title)[0]
            subtitle_w = self._text_size(self.subtitle_text, self.font_subtitle)[0]
            backing_right = text_x + max(title_w, subtitle_w) + 14
            if self.logo:
                backing_right = min(backing_right, WIDTH - PADDING - self.logo.size[0] - 12)
            self._rounded_rect(
                draw,
                (PADDING - 6, text_top - 8, backing_right, text_top + text_block + 8),
                radius=12,
                fill=chip_fill,
            )
        draw.text((text_x, text_top), self.title_text, font=self.font_title, fill=title_color)
        draw.text(
            (text_x, text_top + title_h + 10),
            self.subtitle_text,
            font=self.font_subtitle,
            fill=subtitle_color,
        )
        if self.logo:
            logo_w = self.logo.size[0]
            logo_y = PADDING + max(0, (header_block - LOGO_HEIGHT) // 2)
            img.paste(self.logo, (WIDTH - PADDING - logo_w, logo_y), self.logo)

        # 标题区下方的发光细线：中间亮、两端渐隐
        divider_y = PADDING + header_block + HEADER_GAP // 2
        half = WIDTH / 2 - PADDING
        for x in range(PADDING, WIDTH - PADDING):
            t = abs((x - WIDTH / 2) / half)
            alpha = int(150 * (1 - t) ** 2 + 20)
            draw.point((x, divider_y), fill=(accent_rgba[0], accent_rgba[1], accent_rgba[2], alpha))

        # 区块与面板
        section_color = self.style.get_color("区块")[:3]
        border_color = self.style.get_color("边框")
        cmd_color = self.style.get_color("指令")[:3]
        desc_color = self.style.get_color("描述")[:3]

        for op in ops:
            if op["op"] == "section":
                if has_bg:
                    tw = self._text_size(op["name"], self.font_section)[0]
                    self._rounded_rect(
                        draw,
                        (
                            PADDING - 6,
                            op["y"] - 5,
                            PADDING + SECTION_MARKER_SIZE + SECTION_MARKER_GAP + tw + 10,
                            op["y"] + self._text_size(op["name"], self.font_section)[1] + 5,
                        ),
                        radius=8,
                        fill=chip_fill,
                    )
                # 菱形标记，代替圆点
                ms = SECTION_MARKER_SIZE
                mx, my = PADDING, op["marker_y"]
                draw.polygon(
                    [
                        (mx + ms // 2, my),
                        (mx + ms, my + ms // 2),
                        (mx + ms // 2, my + ms),
                        (mx, my + ms // 2),
                    ],
                    fill=accent,
                )
                draw.text(
                    (
                        PADDING + SECTION_MARKER_SIZE + SECTION_MARKER_GAP,
                        op["y"],
                    ),
                    op["name"],
                    font=self.font_section,
                    fill=section_color,
                )
                continue

            panel_top = op["y"]
            cell_w = op["cell_w"]
            cmd_line_h = self._text_size("指", self.font_cmd)[1] + 5
            desc_line_h = self._text_size("字", self.font_desc)[1] + 6
            cell_y = panel_top + PANEL_PAD_Y
            for row_index, row in enumerate(op["grid_rows"]):
                row_h = op["row_heights"][row_index]
                for col_index, cell in enumerate(row):
                    cell_x = PADDING + PANEL_PAD_X + col_index * (cell_w + CELL_GAP)
                    self._rounded_rect(
                        draw,
                        (cell_x, cell_y, cell_x + cell_w, cell_y + row_h),
                        radius=CELL_RADIUS,
                        fill=card_fill,
                        outline=border_color[:3] if border_color[3] >= 255 else border_color,
                        width=1,
                    )
                    text_x = cell_x + CELL_PAD_X
                    cmd_y = cell_y + CELL_PAD_Y
                    for i, line in enumerate(cell["cmd_lines"]):
                        draw.text(
                            (text_x, cmd_y + i * cmd_line_h),
                            line,
                            font=self.font_cmd,
                            fill=cmd_color,
                        )
                    if cell["desc_lines"]:
                        desc_y = cmd_y + len(cell["cmd_lines"]) * cmd_line_h + CELL_CMD_DESC_GAP
                        for i, line in enumerate(cell["desc_lines"]):
                            draw.text(
                                (text_x, desc_y + i * desc_line_h),
                                line,
                                font=self.font_desc,
                                fill=desc_color,
                            )
                cell_y += row_h + CELL_GAP

        # 页脚
        footer_color = self.style.get_color("页脚")[:3]
        footer_text = f"{self.display_name} v{self.version} · {self.author} 出品"
        fw = self._text_size(footer_text, self.font_footer)[0]
        fx = (WIDTH - fw) // 2
        fy = total_height - FOOTER_HEIGHT + 14
        self._rounded_rect(
            draw,
            (fx - 16, fy - 6, fx + fw + 16, fy + self._text_size(footer_text, self.font_footer)[1] + 8),
            radius=10,
            fill=chip_fill,
        )
        draw.text(
            (fx, fy),
            footer_text,
            font=self.font_footer,
            fill=footer_color,
        )

        with io.BytesIO() as output:
            img.convert("RGB").save(output, format="PNG", optimize=True)
            return output.getvalue()


def render_help_image(config, style: StyleStore, plugin_commands: Dict[str, List[dict]]) -> bytes:
    renderer = HelpImageRenderer(config, style)
    sections = _group_sections(config, plugin_commands)
    return renderer.render(sections)


def _parse_pair_list(raw, limit: int = 1) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    if not isinstance(raw, list):
        return pairs
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        for sep in (":", "："):
            if sep in text:
                left, right = text.split(sep, 1)
                left, right = left.strip(), right.strip()
                if left and right:
                    pairs.append((left, right))
                break
    return pairs


def _group_sections(config, plugin_commands: Dict[str, List[dict]]) -> List[Tuple[str, List[Tuple[str, Optional[str]]]]]:
    blacklist = set(getattr(config, "plugin_blacklist", []) or [])
    sections: List[Tuple[str, List[Tuple[str, Optional[str]]]]] = []

    single_cmds: List[Tuple[str, Optional[str]]] = []
    multi: List[Tuple[str, List[Tuple[str, Optional[str]]]]] = []
    for plugin_name, cmds in plugin_commands.items():
        if not cmds or plugin_name in blacklist:
            continue
        rows = []
        for item in cmds:
            cmd = str(item.get("command") or "").strip()
            if not cmd:
                continue
            desc = str(item.get("desc") or "").strip() or None
            rows.append((cmd, desc))
        if not rows:
            continue
        if len(rows) == 1:
            single_cmds.extend(rows)
        else:
            multi.append((plugin_name, rows))

    multi.sort(key=lambda entry: len(entry[1]), reverse=True)
    sections.extend(multi)
    if single_cmds:
        sections.append(("常用小指令", single_cmds))

    custom_pairs = _parse_pair_list(getattr(config, "custom_cmds", None))
    if custom_pairs:
        sections.append(("自定义命令", custom_pairs))
    return sections
