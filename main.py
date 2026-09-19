import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star_handler import star_handlers_registry, StarHandlerMetadata
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .draw import render_help_image
from .style import COLOR_KEYS, StyleStore

PLUGIN_NAME = "help_dex"

SKIP_STARS = {PLUGIN_NAME, "astrbot", "astrbot-reminder"}

_URL_RE = re.compile(r"https?://\S+")
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),
)

HELP_TEXT = """📖 指令图鉴 · 指令一览

所有人可用：
/帮助图鉴 —— 生成一张指令图鉴图片（发 指令图鉴 也行）
群里只 @ 我 —— 不带任何文字，也会直接发图鉴
/图鉴预览 —— 看当前图鉴效果与自定义状态

管理员可用（改完立即生效，不用重启）：
/图鉴背景 + 图片 —— 上传背景图（也可发图片链接；发「重置」恢复默认渐变）
/图鉴logo + 图片 —— 上传 Logo（白底会自动抠掉；发「重置」移除）
/图鉴颜色 <部位> <颜色> —— 改字体/配色，如 /图鉴颜色 标题 #FF5733
  部位：背景 标题 副标题 区块 指令 描述 强调 卡片 边框 页脚
  「背景」可给两个颜色做渐变：/图鉴颜色 背景 #FFF7E6 #FFE3EE
  单独发 /图鉴颜色 可查看当前配色；「/图鉴颜色 重置」全部恢复默认
/图鉴标题 <文字> —— 改图鉴大标题（发「重置」恢复配置里的值）
/图鉴简介 <文字> —— 改标题下面那行简介
/图鉴重置 —— 颜色、标题、背景、Logo 全部恢复默认

自定义数据存在 data/plugin_data/help_dex/，插件升级不会丢。"""

NO_IMAGE_TIP = "⚠️ 没找到图片。用法：/{} + 图片（或直接发图片链接）"


def _arg_after(text: str, command: str) -> str:
    raw = (text or "").strip()
    index = raw.find(command)
    if index == -1:
        return raw.lstrip("/").strip()
    return raw[index + len(command):].strip()


def _image_components(event: AstrMessageEvent) -> List[Image]:
    try:
        return [m for m in event.get_messages() if isinstance(m, Image)]
    except Exception:
        return []


@register(
    PLUGIN_NAME,
    "娜莉灵",
    "一条指令，把机器人会的一切画成一张暗色科幻风图鉴。群里@一下就发图；背景、Logo、配色想换就换，发张图发条指令秒生效，不用碰文件不用重启",
    "0.2.0",
)
class HelpDexPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(get_astrbot_data_path()).joinpath("plugin_data", PLUGIN_NAME)
        self.style = StyleStore(data_dir)

    # -------------------- 对外指令 --------------------
    @filter.command("帮助图鉴", alias={"指令图鉴"})
    async def help_image(self, event: AstrMessageEvent):
        """生成一张收录所有指令的图鉴图片"""
        commands = self.collect_commands()
        if not commands:
            yield event.plain_result("暂时没有收集到任何指令，先确认装了别的插件再试试～")
            return
        try:
            image = render_help_image(self.config, self.style, commands)
        except Exception as exc:
            logger.error(f"[help_dex] 渲染帮助图失败: {exc}")
            yield event.plain_result("图鉴绘制失败了，请看后台日志排查。")
            return
        yield event.chain_result([Image.fromBytes(image)])

    @event_message_type(EventMessageType.ALL)
    async def at_only_help(self, event: AstrMessageEvent):
        """群里只 @ 机器人（不带任何文字）时，直接发送指令图鉴"""
        if (getattr(event, "message_str", "") or "").strip():
            return
        message_obj = getattr(event, "message_obj", None)
        self_id = str(getattr(message_obj, "self_id", "") or "")
        try:
            components = event.get_messages()
        except Exception:
            return
        at_me = False
        for comp in components:
            if isinstance(comp, At):
                qq = str(getattr(comp, "qq", "") or "")
                if qq and (not self_id or qq == self_id):
                    at_me = True
                continue
            if isinstance(comp, Plain) and not (getattr(comp, "text", "") or "").strip():
                continue
            return
        if not at_me:
            return
        commands = self.collect_commands()
        if not commands:
            return
        try:
            image = render_help_image(self.config, self.style, commands)
        except Exception as exc:
            logger.error(f"[help_dex] 渲染帮助图失败: {exc}")
            return
        yield event.chain_result([Image.fromBytes(image)])

    @filter.command("图鉴预览")
    async def preview(self, event: AstrMessageEvent):
        """预览当前图鉴样式与自定义状态"""
        commands = self.collect_commands()
        if not commands:
            yield event.plain_result("暂时没有收集到任何指令，先确认装了别的插件再试试～")
            return
        try:
            image = render_help_image(self.config, self.style, commands)
        except Exception as exc:
            logger.error(f"[help_dex] 渲染帮助图失败: {exc}")
            yield event.plain_result("图鉴绘制失败了，请看后台日志排查。")
            return
        status = self._style_status()
        yield event.chain_result([Image.fromBytes(image)])
        yield event.plain_result(status)

    @filter.command("图鉴帮助")
    async def plugin_help(self, event: AstrMessageEvent):
        """查看指令图鉴自己的用法"""
        yield event.plain_result(HELP_TEXT)

    # -------------------- 管理员指令 --------------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴背景")
    async def set_background(self, event: AstrMessageEvent):
        """上传/重置帮助图背景图（管理员）"""
        arg = _arg_after(event.message_str, "图鉴背景")
        if arg in ("重置", "复位", "删除"):
            self.style.clear_background()
            yield event.plain_result("✅ 背景图已移除，恢复默认渐变背景。")
            return
        source, error = await self._resolve_image_source(event, arg, "图鉴背景")
        if error:
            yield event.plain_result(error)
            return
        error = self.style.set_background(source)
        if error:
            yield event.plain_result(f"❌ {error}")
            return
        yield event.plain_result("✅ 背景图已更新，发 /帮助图鉴 看看效果～")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴logo")
    async def set_logo(self, event: AstrMessageEvent):
        """上传/移除帮助图 Logo（管理员）"""
        arg = _arg_after(event.message_str, "图鉴logo")
        if arg in ("重置", "复位", "删除", "移除"):
            self.style.clear_logo()
            yield event.plain_result("✅ Logo 已移除。")
            return
        source, error = await self._resolve_image_source(event, arg, "图鉴logo")
        if error:
            yield event.plain_result(error)
            return
        error = self.style.set_logo(source)
        if error:
            yield event.plain_result(f"❌ {error}")
            return
        yield event.plain_result("✅ Logo 已更新（白底会自动抠掉），发 /帮助图鉴 看看效果～")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴颜色")
    async def set_color(self, event: AstrMessageEvent):
        """自定义帮助图配色（管理员）"""
        arg = _arg_after(event.message_str, "图鉴颜色")
        if not arg:
            usage = "用法：/图鉴颜色 <部位> <颜色>，如 /图鉴颜色 标题 #FF5733\n"
            usage += "部位：背景 标题 副标题 区块 指令 描述 强调 卡片 边框 页脚\n"
            usage += "「背景」支持两个颜色做渐变；「卡片」支持 8 位带透明度（#RRGGBBAA）\n\n"
            usage += "当前配色：\n" + self.style.color_summary()
            yield event.plain_result(usage)
            return
        if arg == "重置":
            self.style.reset_color()
            yield event.plain_result("✅ 配色已全部恢复默认。")
            return
        parts = arg.split()
        key = parts[0]
        if key == "重置":
            if len(parts) > 1:
                self.style.reset_color(parts[1])
                yield event.plain_result(f"✅ 「{parts[1]}」已恢复默认。")
            else:
                self.style.reset_color()
                yield event.plain_result("✅ 配色已全部恢复默认。")
            return
        error = self.style.set_color(key, parts[1:])
        if error:
            yield event.plain_result(f"❌ {error}\n部位：背景 标题 副标题 区块 指令 描述 强调 卡片 边框 页脚")
            return
        yield event.plain_result(f"✅ 「{key}」颜色已更新，发 /帮助图鉴 看看效果～")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴标题")
    async def set_title(self, event: AstrMessageEvent):
        """自定义帮助图大标题（管理员）"""
        arg = _arg_after(event.message_str, "图鉴标题")
        if arg in ("重置", "复位"):
            self.style.set_title(None)
            yield event.plain_result("✅ 标题已恢复为插件配置里的值。")
            return
        if not arg:
            yield event.plain_result(
                f"用法：/图鉴标题 <文字>（当前：{self.style.effective_title(str(getattr(self.config, 'title_help', '') or '指令图鉴'))}）"
            )
            return
        self.style.set_title(arg)
        yield event.plain_result("✅ 标题已更新，发 /帮助图鉴 看看效果～")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴简介")
    async def set_subtitle(self, event: AstrMessageEvent):
        """自定义帮助图简介（管理员）"""
        arg = _arg_after(event.message_str, "图鉴简介")
        if arg in ("重置", "复位"):
            self.style.set_subtitle(None)
            yield event.plain_result("✅ 简介已恢复为插件配置里的值。")
            return
        if not arg:
            yield event.plain_result(
                f"用法：/图鉴简介 <文字>（当前：{self.style.effective_subtitle(str(getattr(self.config, 'title_desc', '') or '这里收录了我会的所有指令'))}）"
            )
            return
        self.style.set_subtitle(arg)
        yield event.plain_result("✅ 简介已更新，发 /帮助图鉴 看看效果～")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴重置")
    async def reset_style(self, event: AstrMessageEvent):
        """恢复图鉴全部默认样式（管理员）"""
        self.style.reset_all()
        yield event.plain_result("✅ 图鉴样式已全部恢复默认（颜色、标题、背景、Logo）。")

    # -------------------- 图片来源 --------------------
    async def _resolve_image_source(
        self, event: AstrMessageEvent, arg: str, command_name: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """返回 (图片本地路径, 错误提示)，两者互斥。"""
        for comp in _image_components(event):
            try:
                path = await comp.convert_to_file_path()
            except Exception as exc:
                logger.warning(f"[help_dex] 图片转存失败: {exc}")
                continue
            if path and os.path.isfile(path):
                return str(path), None
        url_match = _URL_RE.search(arg)
        if url_match:
            url = url_match.group(0).rstrip("，。！？!?,.")
            downloaded = await self._download_image(url)
            if downloaded:
                return downloaded, None
            return None, "❌ 图片下载失败，请检查链接（限 http/https，不超过 20MB）。"
        return None, NO_IMAGE_TIP.format(command_name)

    async def _download_image(self, url: str) -> Optional[str]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        return None
                    content_type = resp.headers.get("Content-Type", "")
                    data = await resp.read()
        except Exception as exc:
            logger.warning(f"[help_dex] 下载图片失败: {exc}")
            return None
        if len(data) > _MAX_DOWNLOAD_BYTES or not data:
            return None
        ext = ".png"
        if content_type.startswith("image/"):
            subtype = content_type.split("/", 1)[1].split(";")[0].strip().lower()
            if subtype in ("jpeg", "jpg", "png", "webp", "gif", "bmp"):
                ext = ".jpg" if subtype == "jpeg" else f".{subtype}"
        else:
            for signature, sig_ext in _IMAGE_SIGNATURES:
                if data.startswith(signature):
                    ext = sig_ext
                    break
        fd, path = tempfile.mkstemp(suffix=ext, prefix="help_dex_")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except Exception:
            os.unlink(path)
            return None
        return path

    # -------------------- 样式状态 --------------------
    def _style_status(self) -> str:
        lines = ["🎨 当前图鉴自定义状态："]
        customized = bool(self.style.colors) or self.style.background_path() or self.style.logo_path()
        lines.append(
            f"背景图：{'已设置' if self.style.background_path() else '未设置（默认渐变）'}"
        )
        lines.append(f"Logo：{'已设置' if self.style.logo_path() else '未设置'}")
        changed = [key for key in COLOR_KEYS if key in self.style.colors]
        lines.append(f"自定义颜色：{('、'.join(changed)) if changed else '无'}")
        lines.append(f"标题：{self.style.title_override or '默认'}")
        lines.append(f"简介：{self.style.subtitle_override or '默认'}")
        lines.append("全部恢复默认可用 /图鉴重置" if customized else "全部都是默认样式，用 /图鉴帮助 看怎么装扮～")
        return "\n".join(lines)

    # -------------------- 指令收集 --------------------
    def _permission_level(self, handler: StarHandlerMetadata) -> str:
        for event_filter in handler.event_filters:
            if isinstance(event_filter, PermissionTypeFilter):
                return (
                    "admin"
                    if event_filter.permission_type == PermissionType.ADMIN
                    else "member"
                )
        return "everyone"

    def _display_name_map(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        raw = getattr(self.config, "plugin_display_names", []) or []
        if not isinstance(raw, list):
            return mapping
        for item in raw:
            if not isinstance(item, str):
                continue
            text = item.strip()
            for sep in (":", "："):
                if sep in text:
                    left, right = text.split(sep, 1)
                    left, right = left.strip(), right.strip()
                    if left and right:
                        mapping[left] = right
                    break
        return mapping

    def collect_commands(self) -> Dict[str, List[dict]]:
        """收集所有已激活插件的指令，返回 {插件显示名: [{command, desc, permission}]}"""
        result: Dict[str, List[dict]] = {}
        try:
            stars = [star for star in self.context.get_all_stars() if star.activated]
        except Exception as exc:
            logger.error(f"[help_dex] 获取插件列表失败: {exc}")
            return {}

        name_map = self._display_name_map()
        show_all = bool(getattr(self.config, "show_all_cmds", False))
        show_builtin = bool(getattr(self.config, "show_builtin_cmds", True))
        blacklist = set(getattr(self.config, "plugin_blacklist", []) or [])
        seen: set = set()

        for star in stars:
            star_name = getattr(star, "name", "") or ""
            if star_name in SKIP_STARS:
                continue
            if star_name == "builtin_commands" and not show_builtin:
                continue
            module_path = getattr(star, "module_path", None)
            star_cls = getattr(star, "star_cls", None)
            if not star_name or not module_path or star_cls is None:
                continue

            display_name = (
                (getattr(star, "display_name", "") or "").strip()
                or name_map.get(star_name, "")
                or star_name
            )
            if star_name in blacklist or display_name in blacklist:
                continue

            for handler in star_handlers_registry:
                if not isinstance(handler, StarHandlerMetadata):
                    continue
                if handler.handler_module_path != module_path:
                    continue
                command_name = None
                for event_filter in handler.event_filters:
                    if isinstance(event_filter, (CommandFilter, CommandGroupFilter)):
                        command_name = (
                            event_filter.command_name
                            if isinstance(event_filter, CommandFilter)
                            else event_filter.group_name
                        )
                        break
                if not command_name:
                    continue
                permission = self._permission_level(handler)
                if permission == "admin" and not show_all:
                    continue
                desc = (handler.desc or "").strip().splitlines()[0].strip() if handler.desc else ""
                key = (display_name, command_name, desc, permission)
                if key in seen:
                    continue
                seen.add(key)
                result.setdefault(display_name, []).append(
                    {"command": command_name, "desc": desc, "permission": permission}
                )
        return result
