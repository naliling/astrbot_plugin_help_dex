import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import (
    CustomFilter,
    EventMessageType,
    event_message_type,
)
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star_handler import star_handlers_registry, StarHandlerMetadata
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .draw import render_help_image
from .page import (
    DEFAULT_PORT,
    INSTALL_HINT,
    LOOPBACK_HINT,
    PageServer,
    TunnelError,
    TunnelManager,
    build_page_payload,
    classify_base_url,
    describe_override,
    generate_access_key,
    is_cpolar_url,
    parse_group_override,
    sanitize_access_key,
)
from .style import COLOR_KEYS, StyleStore, background_file_name

PLUGIN_NAME = "help_dex"

# 这两个是框架本体与内置提醒，不该被当成「插件的指令」列出来。
# 注意：不要把自己（help_dex）加进来——那会让图鉴里一条自己的指令都没有，
# 用户连「怎么用图鉴」都没处看。
SKIP_STARS = {"astrbot", "astrbot-reminder"}

SEEN_GROUPS_FILE = "groups.json"
KNOWN_GROUPS_FILE = "known_groups.json"

# 群列表轮询：group_increase 通知在某些 OneBot 实现上根本不上报，
# 群列表是另一条独立可靠的路——机器人在的群才会出现在里面。
# 间隔不能太大：它是「进群后多久才发链接」的唯一决定因素（通知没来时）。
GROUP_POLL_INTERVAL = 8.0
GROUP_POLL_WARMUP = 2.0
GROUP_POLL_TIMEOUT = 15.0

_DETECT_LABELS = {"notice": "仅进群通知", "both": "通知 + 群列表",
                   "unknown": "仅陌生群", "off": "关掉"}

# 默认引导语：不表明是机器人，不放表情包堆砌，把链接单独一行。
DEFAULT_WELCOME_TEXT = "我到了，这页写着我都能做什么：\n{link}"

# 链接暂时不可用时的占位引导。不能让人以为它就是最终那条链接。
LINK_PENDING_TEXT = "我到了，正在准备说明书，稍后发上来。"

# 探测对外地址：太短会误判成不通，太长会把入群卡住
PROBE_TIMEOUT = 3.5
PROBE_TTL = 300.0

_URL_RE = re.compile(r"https?://\S+")
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),
)

HELP_TEXT = """📖 指令图鉴

用：/帮助图鉴 发一张图；群里只 @ 我 也发。

管理：
/图鉴背景 · /图鉴logo · /图鉴颜色 · /图鉴标题 · /图鉴简介 —— 换外观
（发图给当前 bot 设专属背景；/图鉴背景 全局 + 图 给所有 bot 设默认）
/图鉴重置 —— 外观全恢复默认
/图鉴入群 开|关 —— 开关进群引导
/图鉴静默 <秒> —— 进群后多久不回话，0 关（只压制自动回复，指令随时可用）
/图鉴规则 <文字> —— 改网页顶部规则
/图鉴链接 —— 看网页地址；加「换」换密钥
/图鉴隧道 开|关 —— 开=公网隧道（cpolar，启动时也会自动开）；令牌 <token> 配一次就好
/图鉴页面 [群号] —— 看某群网页配置
/图鉴诊断 —— 排查进群不发链接

数据在 data/plugin_data/help_dex/，升级不丢。"""

NO_IMAGE_TIP = "⚠️ 没找到图片。用法：/{} + 图片（或直接发图片链接）"


class _BotGroupJoinFilter(CustomFilter):
    """只认「机器人自己进群/被移出群」两件事。

    为什么不用 EventMessageType.ALL：框架里每个 handler 跑完都会执行
    `event.clear_result()`，而 ALL 匹配**所有**群消息——help_dex 在每条消息上
    都执行一次、清一次结果，会把排在后面的插件（比如自主社交）的结果清掉。

    精确匹配后，平时 help_dex 的 handler 根本不会被激活；只有机器人自己
    被拉进群或被踢出群时才进来。
    """

    def filter(self, event, cfg) -> bool:
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if not isinstance(raw, dict):
                return False
            notice = str(raw.get("notice_type") or "")
            getter = getattr(event, "get_self_id", None)
            self_id = str(getter() or "").strip() if callable(getter) else ""
            if not self_id or str(raw.get("user_id") or "") != self_id:
                return False
            return notice in ("group_increase", "group_decrease")
        except Exception:
            return False


class _OnlyAtBotFilter(CustomFilter):
    """只认「这条消息除了 @ 机器人什么都没有」，同样为了不占 activated_handlers。"""

    def filter(self, event, cfg) -> bool:
        try:
            if (getattr(event, "message_str", "") or "").strip():
                return False
            self_id = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
            if not self_id:
                get = getattr(event, "get_self_id", None)
                self_id = str(get() or "") if callable(get) else ""
            if not self_id:
                return False
            try:
                components = list(event.get_messages())
            except Exception:
                return False
            at_bot = False
            for comp in components:
                if isinstance(comp, At):
                    if str(getattr(comp, "qq", "") or "") == self_id:
                        at_bot = True
                    continue
                if isinstance(comp, Plain) and not (getattr(comp, "text", "") or "").strip():
                    continue
                return False
            return at_bot
        except Exception:
            return False


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
    "0.6.1",
)
class HelpDexPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(get_astrbot_data_path()).joinpath("plugin_data", PLUGIN_NAME)
        self.style = StyleStore(self.data_dir)
        self.server: Optional[PageServer] = None
        self._tunnel = TunnelManager(on_url_changed=self._on_tunnel_url_changed)
        self._quiet: Dict[str, float] = {}
        self._pending_link: set = set()
        self._seen_groups = self._load_seen_groups()
        self._fired_groups: set = set()
        self._probe_ok: Optional[bool] = None
        self._probed_at: float = 0.0
        self._recent: List[dict] = []
        loaded_groups, loaded_per_bot = self._load_known_groups()
        self._known_groups_initialized = loaded_groups is not None
        self._known_groups: set = loaded_groups or set()
        # 同机多 Bot 的关键：群列表基线要按 bot 分开记。
        # 群在 B 的列表里不代表对 A 是「已知群」——只有出现在 A 自己
        # 那份列表里、且上一轮不在，才算「A 进了群」。
        self._known_by_bot: Dict[str, set] = loaded_per_bot
        # group -> 进群的 bot key。补发/发欢迎时，谁刚进群就该由谁开口。
        self._join_via: Dict[str, str] = {}
        # 实例对象 -> bot key 的缓存（get_login_info 每个实例只问一次）
        self._inst_keys: Dict[int, str] = {}
        self._poller = None
        self._tunnel_task: Optional[asyncio.Task] = None
        self._poll_ready = False
        self._last_poll_error = ""
        self._link_down_reason = ""
        self._suppress_welcome_until = 0.0

    # -------------------- 生命周期 --------------------
    async def initialize(self) -> None:
        """服务必须挂在这里而不是 on_astrbot_loaded：后者只在启动时触发一次，
        面板「重载插件」不会重新触发，那样热重载后服务就没了。

        群列表轮询必须在最前面启动：它和网页服务是两件事。
        之前放在最后一行，page_enabled 一关（或者服务启动失败）就跟着 return，
        于是入群检测整个死掉，而表面上只像是「网页功能没开」。
        """
        self._start_group_poll()
        if not bool(getattr(self.config, "page_enabled", True)):
            logger.info("[help_dex] 欢迎页服务已在配置里关闭")
            return
        key = sanitize_access_key(getattr(self.config, "page_access_key", ""))
        if not key:
            key = generate_access_key()
            self.config["page_access_key"] = key
            self._save_config()
            logger.info("[help_dex] 已生成页面访问密钥")
        try:
            preferred = int(getattr(self.config, "page_port", DEFAULT_PORT) or DEFAULT_PORT)
        except (TypeError, ValueError):
            preferred = DEFAULT_PORT
        self.server = PageServer(self.style, key, preferred, self._page_payload)
        try:
            port = await self.server.start()
        except Exception as exc:
            logger.error(f"[help_dex] 欢迎页服务启动失败：{exc}")
            self.server = None
            return
        logger.info(f"[help_dex] 欢迎页服务已启动：http://127.0.0.1:{port}/{key}/")
        if not self._base_url():
            raw = str(getattr(self.config, "public_base_url", "") or "").strip()
            if raw:
                logger.error("[help_dex] " + LOOPBACK_HINT)
        # 隧道放后台拉：首次会自动下载 cpolar（十几 MB），网络不好要几分钟，
        # 不能卡住插件加载；轮询与指令在这期间照常可用。
        if self._tunnel_task is None or self._tunnel_task.done():
            self._tunnel_task = asyncio.create_task(self._restore_tunnel(port))

    async def _restore_tunnel(self, port: int) -> None:
        """启动时把隧道拉回来——不然每次重启都必然「对外地址不可用」。

        换到 cpolar 后这里简单多了：token 在就行——
        没 token 就什么都不做（等用户粘 token），有 token 就直接拉。
        没装 cpolar 也不让用户管：当场自动下载安装。
        免费版随机地址本来就会重置，拉起来会拿一个新地址写回配置。

        这个函数跑在后台任务里，**整段包一层**：任何一个漏网异常
        都会变成「Task exception was never retrieved」——不响、也没人管。
        """
        try:
            await self._restore_tunnel_inner(port)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[help_dex] 隧道初始化异常（不影响其他功能）：{exc}")

    async def _restore_tunnel_inner(self, port: int) -> None:
        if not bool(getattr(self.config, "tunnel_autostart", True)):
            logger.info("[help_dex] tunnel_autostart 是关的，启动时不自动拉隧道")
            return
        if not TunnelManager.has_token():
            return                      # 没 token，等用户 /图鉴隧道 令牌 <token>
        raw = str(getattr(self.config, "public_base_url", "") or "").strip()
        if self._base_url() and not is_cpolar_url(raw):
            # 用户自己配了反代/其他公网地址：不动它
            logger.info("[help_dex] 对外地址不是 cpolar 隧道，请自行确认反代指向本机端口")
            return
        if not TunnelManager.is_available():
            logger.info("[help_dex] 没找到 cpolar，正在自动下载安装…")
            try:
                await TunnelManager.ensure_binary()
            except Exception as exc:
                logger.warning(f"[help_dex] cpolar 自动安装失败：{exc}")
                return
        logger.info("[help_dex] 正在拉起 cpolar 隧道…")
        try:
            url = await self._tunnel.start(port)
        except Exception as exc:
            logger.warning(f"[help_dex] cpolar 隧道自动拉起失败：{exc}")
            return
        if url != raw:
            logger.info(f"[help_dex] cpolar 隧道地址已更新：{raw or '（无）'} → {url}")
        self.config["public_base_url"] = url
        self._save_config()
        self._probe_ok = None
        self._probed_at = 0.0

    async def terminate(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
            self._poller = None
        if self._tunnel_task is not None:
            self._tunnel_task.cancel()
            self._tunnel_task = None
        await self._tunnel.stop()
        if self.server is not None:
            await self.server.stop()
            self.server = None

    # -------------------- 内部状态 --------------------
    def _save_config(self) -> None:
        try:
            self.config.save_config()
        except Exception as exc:
            logger.warning(f"[help_dex] 保存配置失败: {exc}")

    def _load_known_groups(self) -> Tuple[Optional[set], Dict[str, set]]:
        """群列表基线。两层：per_bot 那份驱动「哪个 bot 进了哪个群」，
        并集只留给诊断看。旧文件没有 per_bot 键时返回空 dict——
        首轮只建基线，不会误触发。"""
        try:
            raw = json.loads(
                (self.data_dir / KNOWN_GROUPS_FILE).read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return None, {}
        except Exception:
            return None, {}
        known = raw.get("known") if isinstance(raw, dict) else None
        if not isinstance(known, list):
            return None, {}
        known_set = {str(item).strip() for item in known if str(item).strip()}
        per_bot_raw = raw.get("per_bot") if isinstance(raw, dict) else None
        per_bot: Dict[str, set] = {}
        if isinstance(per_bot_raw, dict):
            for key, groups in per_bot_raw.items():
                if isinstance(groups, list):
                    per_bot[str(key).strip()] = {
                        str(g).strip() for g in groups if str(g).strip()
                    }
        return known_set, per_bot

    def _save_known_groups(self) -> None:
        try:
            (self.data_dir / KNOWN_GROUPS_FILE).write_text(
                json.dumps(
                    {
                        "known": sorted(self._known_groups),
                        "per_bot": {
                            key: sorted(groups)
                            for key, groups in sorted(self._known_by_bot.items())
                        },
                        "at": time.time(),
                    },
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[help_dex] 保存群列表基线失败: {exc}")

    def _platform_instances(self) -> List:
        manager = getattr(self.context, "platform_manager", None)
        return list(getattr(manager, "platform_insts", []) or []) if manager else []

    async def _fetch_groups_per_instance(self) -> List[dict]:
        """逐个 bot 拉群列表。拉不到的实例**不进结果**——
        调用方保留它上一轮的基线，瞬时故障绝不能判成「退出了所有群」。
        一个都没拉到时返回空列表，等价于旧的 None。"""
        rows: List[dict] = []
        for index, inst in enumerate(self._platform_instances()):
            bot = getattr(inst, "bot", None)
            data: Optional[set] = None
            error = ""
            for attr in ("call_api", "call_action"):
                caller = getattr(bot, attr, None)
                if not callable(caller):
                    continue
                try:
                    resp = await asyncio.wait_for(
                        caller("get_group_list", no_cache=True),
                        timeout=GROUP_POLL_TIMEOUT,
                    )
                except TypeError:
                    # 签名不接受 no_cache，去掉重试一次
                    try:
                        resp = await asyncio.wait_for(
                            caller("get_group_list"), timeout=GROUP_POLL_TIMEOUT
                        )
                    except Exception as exc:
                        error = "{} 失败: {}".format(attr, exc)
                        continue
                except Exception as exc:
                    error = "{} 失败: {}".format(attr, exc)
                    continue
                data = self._parse_group_list(resp)
                if data is None:
                    error = "{} 返回了看不懂的东西".format(attr)
                    continue
                error = ""
                break
            if data is None:
                if error and not self._last_poll_error:
                    self._last_poll_error = error
                continue
            rows.append(
                {
                    "key": await self._bot_key_of(inst, index),
                    "groups": data,
                    "inst": inst,
                }
            )
        if not rows and not self._last_poll_error:
            self._last_poll_error = "没有可用平台实例"
        return rows

    async def _bot_key_of(self, inst, index: int) -> str:
        """bot 的稳定标识：get_login_info 的 QQ 号，重启后也认得出。
        拿不到就用 平台名+序号 兜底——基线只是新建一次，
        只多建基线、不会误触发。"""
        cached = self._inst_keys.get(id(inst))
        if cached:
            return cached
        key = ""
        bot = getattr(inst, "bot", None)
        for attr in ("call_api", "call_action"):
            caller = getattr(bot, attr, None)
            if not callable(caller):
                continue
            try:
                resp = await asyncio.wait_for(
                    caller("get_login_info"), timeout=GROUP_POLL_TIMEOUT
                )
                raw_data = resp.get("data") if isinstance(resp, dict) else None
                user = raw_data.get("user_id") if isinstance(raw_data, dict) else None
                key = str(user or "").strip()
            except Exception:
                continue
            if key:
                break
        if not key:
            key = "inst_{}_{}".format(self._platform_id(inst) or "?", index + 1)
        self._inst_keys[id(inst)] = key
        return key

    async def _find_instance_by_key(self, key: Optional[str]):
        if not key:
            return None
        for index, inst in enumerate(self._platform_instances()):
            if await self._bot_key_of(inst, index) == key:
                return inst
        return None

    async def _send_via_instance(self, inst, group_id: str, text: str) -> bool:
        """从**这个 bot 自己的连接**直接发群消息。

        框架的 context.send_message 会路由到「第一个成功的实例」：
        两个 bot 同群时你控制不了哪张嘴开口——这正是「A 进群、B 说话」
        的根源。直连 send_group_msg 才能保证欢迎语从刚进群的 bot 嘴里发出。
        auto_escape=True：文本原样发出，不碰 CQ 码解析。
        """
        try:
            gid = int(group_id)
        except (TypeError, ValueError):
            return False
        bot = getattr(inst, "bot", None)
        for attr in ("call_api", "call_action"):
            caller = getattr(bot, attr, None)
            if not callable(caller):
                continue
            try:
                await asyncio.wait_for(
                    caller(
                        "send_group_msg",
                        group_id=gid,
                        message=text,
                        auto_escape=True,
                    ),
                    timeout=15.0,
                )
                return True
            except Exception as exc:
                logger.warning(f"[help_dex] bot 直连发送失败({attr}): {exc}")
                return False
        return False

    async def _fetch_group_ids(self) -> Optional[set]:
        """所有 bot 的群并集。拉不到返回 None，别当成空列表。"""
        rows = await self._fetch_groups_per_instance()
        if not rows:
            return None
        found: set = set()
        for row in rows:
            found |= row["groups"]
        return found

    @staticmethod
    def _parse_group_list(resp) -> Optional[set]:
        """兼容几种返回形态：{'data': [...]} / [...] / None。"""
        if resp is None:
            return None
        rows = resp.get("data") if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            return None
        found = set()
        for item in rows:
            if isinstance(item, dict):
                gid = str(item.get("group_id") or "").strip()
            else:
                gid = str(item or "").strip()
            if gid:
                found.add(gid)
        return found

    @staticmethod
    def _platform_id(inst) -> str:
        """适配器的 meta 在不同版本可能是方法也可能是属性，都得认。"""
        meta = getattr(inst, "meta", None)
        if callable(meta):
            try:
                meta = meta()
            except Exception:
                return ""
        return str(getattr(meta, "id", "") or "")

    def _session_ums(self, group_id: str) -> List[str]:
        umos: List[str] = []
        for inst in self._platform_instances():
            pid = self._platform_id(inst)
            if pid:
                umos.append(
                    "{}:{}:{}".format(
                        pid, MessageType.GROUP_MESSAGE.value, group_id
                    )
                )
        return umos or [
            "aiocqhttp:{}:{}".format(MessageType.GROUP_MESSAGE.value, group_id)
        ]

    async def _send_to_group(self, group_id: str, text: str,
                             via_key: Optional[str] = None) -> bool:
        # 优先从「刚进群的 bot」自己的连接发：多 bot 同群时
        # 框架路由会让别的 bot 抢着开口。
        if via_key:
            inst = await self._find_instance_by_key(via_key)
            if inst is not None and await self._send_via_instance(inst, group_id, text):
                return True
        for umo in self._session_ums(group_id):
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
                return True
            except Exception as exc:
                logger.warning(f"[help_dex] 主动发消息失败({umo}): {exc}")
        return False

    async def _deliver_welcome(self, group_id: str,
                               via_key: Optional[str] = None) -> bool:
        """发一条引导。地址不可用时登记待补，不占用「已发」名额。

        关键：地址不可用不能 count 成“发过了”——如果那样，隧道之后恢复，
        这个群就永远收不到链接（旧实现就是这个毛病：发一句“稍后发”，
        然后再也不发）。所以只有真正发出链接才 claim 这个群。
        via_key 是谁刚进群的 bot：它发占位语、发链接，都用它自己的嘴。
        """
        link = await self._resolve_link(group_id)
        if not link:
            if group_id not in self._pending_link:
                self._pending_link.add(group_id)
                if via_key:
                    self._join_via[group_id] = via_key
                await self._send_to_group(group_id, LINK_PENDING_TEXT, via_key=via_key)
                logger.info(
                    f"[help_dex] 群 {group_id} 链接暂不可用，已登记待补发"
                )
            return False
        template = (
            str(getattr(self.config, "welcome_text", "") or "").strip()
            or DEFAULT_WELCOME_TEXT
        )
        await self._send_to_group(group_id, template.replace("{link}", link),
                                  via_key=via_key)
        self._pending_link.discard(group_id)
        self._join_via.pop(group_id, None)
        return True

    async def _flush_pending_links(self) -> None:
        """隧道就绪后，把之前没发成链接的群补上。"""
        if not self._pending_link:
            return
        template = (
            str(getattr(self.config, "welcome_text", "") or "").strip()
            or DEFAULT_WELCOME_TEXT
        )
        for group_id in sorted(self._pending_link):
            link = await self._resolve_link(group_id)
            if not link:
                continue
            via_key = self._join_via.get(group_id)
            await self._send_to_group(group_id, template.replace("{link}", link),
                                      via_key=via_key)
            self._pending_link.discard(group_id)
            self._join_via.pop(group_id, None)
            logger.info(f"[help_dex] 已给群 {group_id} 补发链接")

    async def _on_group_appeared(self, group_id: str,
                                 via_key: Optional[str] = None) -> None:
        """某个 bot 的列表里冒出新群 = 那个 bot 被拉进去了。
        via_key 是谁进的群；多 bot 同群时只有刚进的那个开口。"""
        who = f"（bot {via_key}）" if via_key else ""
        logger.info(f"[help_dex] 群 {group_id} 新出现在群列表里{who}，按入群处理")
        self._open_quiet(group_id)
        if not bool(getattr(self.config, "welcome_enabled", True)):
            return
        if not self._claim_group(group_id):
            return
        if via_key:
            self._join_via[group_id] = via_key
        await self._deliver_welcome(group_id, via_key=via_key)

    async def _apply_group_snapshot(self, rows: List[dict]) -> None:
        """应用一轮成功拉取。rows 里每个元素是某个 bot 的群列表。

        首轮成功只建基线，绝不触发——否则一启动就给每个老群发欢迎。
        本轮才首次出现的 bot（新登录/换了 key）也只建基线。
        拉取失败的 bot 不在 rows 里：保留它上一轮的基线——
        瞬时故障不能被误判成「退出了所有群」。
        """
        if not self._known_groups_initialized:
            for row in rows:
                self._known_by_bot[row["key"]] = set(row["groups"])
            summary = "、".join(
                "{} {} 个群".format(row["key"], len(row["groups"])) for row in rows
            ) or "没有实例"
            logger.info(f"[help_dex] 群列表基线：{summary}")
            self._known_groups_initialized = True
            self._known_groups = self._union_of(rows)
            self._save_known_groups()
            self._poll_ready = True
            return

        for row in rows:
            key = row["key"]
            current = set(row["groups"])
            prev = self._known_by_bot.get(key)
            if prev is None:
                logger.info(
                    f"[help_dex] bot {key} 第一轮：建群基线（{len(current)} 个群），不触发"
                )
                self._known_by_bot[key] = current
                continue
            departed = prev - current
            if departed:
                for gid in departed:
                    self._forget_group(gid)
                self._save_seen_groups()
            arrived = current - prev
            self._known_by_bot[key] = current
            for gid in sorted(arrived):
                await self._on_group_appeared(gid, via_key=key)
        fresh = {row["key"] for row in rows}
        stale = {
            gid
            for key, groups in self._known_by_bot.items()
            if key not in fresh
            for gid in groups
        }
        self._known_groups = self._union_of(rows) | stale
        self._save_known_groups()
        self._poll_ready = True

    @staticmethod
    def _union_of(rows: List[dict]) -> set:
        found: set = set()
        for row in rows:
            found |= row["groups"]
        return found

    async def _group_poll_loop(self) -> None:
        await asyncio.sleep(GROUP_POLL_WARMUP)
        while True:
            try:
                rows = await self._fetch_groups_per_instance()
                if rows:
                    await self._apply_group_snapshot(rows)
                await self._flush_pending_links()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[help_dex] 群列表轮询异常: {exc}")
            await asyncio.sleep(GROUP_POLL_INTERVAL)

    def _forget_group(self, group_id: str) -> None:
        self._seen_groups.discard(group_id)
        self._fired_groups.discard(group_id)
        self._quiet.pop(group_id, None)

    def _forget_bot_left(self, event: AstrMessageEvent) -> None:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return
        if str(raw.get("notice_type") or "") != "group_decrease":
            return
        self_id = str(event.get_self_id() or "").strip()
        if not self_id or str(raw.get("user_id") or "") != self_id:
            return
        group_id = str(raw.get("group_id") or event.get_group_id() or "").strip()
        if group_id:
            self._forget_group(group_id)
            self._save_seen_groups()
            logger.info(f"[help_dex] 机器人离开群 {group_id}，已清理入群去重状态")

    def _start_group_poll(self) -> None:
        if not bool(getattr(self.config, "group_poll", True)):
            logger.info("[help_dex] 群列表轮询已在配置里关闭")
            return
        if self._poller is not None and not self._poller.done():
            return
        self._poller = asyncio.create_task(self._group_poll_loop())

    def _load_seen_groups(self) -> set:
        """已见过的群号要落盘，否则重启后所有老群都会被当成新群再发一遍欢迎。"""
        try:
            raw = json.loads(
                (self.data_dir / SEEN_GROUPS_FILE).read_text(encoding="utf-8")
            )
        except Exception:
            return set()
        seen = raw.get("seen") if isinstance(raw, dict) else None
        if not isinstance(seen, list):
            return set()
        return {str(item).strip() for item in seen if str(item).strip()}

    def _save_seen_groups(self) -> None:
        try:
            (self.data_dir / SEEN_GROUPS_FILE).write_text(
                json.dumps({"seen": sorted(self._seen_groups)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[help_dex] 保存群记录失败: {exc}")

    def _render_image(self, commands: Optional[Dict[str, List[dict]]] = None,
                      bot_id: Optional[str] = None) -> Optional[bytes]:
        try:
            return render_help_image(
                self.config, self.style, commands or self.collect_commands(),
                bot_id=bot_id,
            )
        except Exception as exc:
            logger.error(f"[help_dex] 渲染帮助图失败: {exc}")
            return None

    def _bot_id_for(self, event: AstrMessageEvent) -> Optional[str]:
        """当前事件属于哪个 bot。取 self_id（QQ 号）当 bot 标识。"""
        getter = getattr(event, "get_self_id", None)
        value = str(getter() or "").strip() if callable(getter) else ""
        return value or None

    def _public_commands(self) -> Dict[str, List[dict]]:
        """页面对公网开放，管理员指令一律不上页。

        不看「显示管理员指令」那个配置：它管的是给谁看的图鉴图片，
        放上公网就等于把 /图鉴重置、/图鉴链接 这类入口也贴出去。
        """
        public: Dict[str, List[dict]] = {}
        for plugin, items in self.collect_commands().items():
            rows = [item for item in items if item.get("permission") != "admin"]
            if rows:
                public[plugin] = rows
        return public

    def _page_payload(self, group_id: str = "") -> dict:
        return build_page_payload(
            config=self.config,
            style=self.style,
            commands=self._public_commands(),
            group_id=group_id,
        )

    def _public_link(self, group_id: str = "") -> Optional[str]:
        if self.server is None:
            return None
        base = self._base_url()
        if not base:
            return None
        return self.server.link(base, group_id)

    def _base_url(self) -> str:
        """对外地址。回环与内网地址都当没填处理。

        因为探测是从这台机器发出的，探 127.0.0.1 必然“通”，
        但群里的人在手机上点，127.0.0.1 指的是他自己的手机；
        192.168.x.x 同理。不在探测之前拦掉，就会报出「已探测通过」这种骗人的话。
        """
        base = str(getattr(self.config, "public_base_url", "") or "").strip()
        if not base:
            return ""
        if not base.startswith(("http://", "https://")):
            base = "https://" + base
        kind = classify_base_url(base)
        if kind:
            logger.error(
                f"[help_dex] 「对外访问地址」填的是 {base}（{kind}），"
                "群里的人打不开，已当没填处理。"
            )
            return ""
        return base

    @staticmethod
    def _group_of(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_group_id", None)
        return str(getter() or "").strip() if callable(getter) else ""

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_private_chat", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                pass
        return not HelpDexPlugin._group_of(event)

    async def _probe_public_link(self) -> bool:
        """自己探一下对外地址通不通。

        探不通就退回发图鉴图片——在群里发一条点不开的死链，比发张图糟得多。
        注意探测只能验证「链路通不通」，验证不了「别人能不能打开」，
        所以回环与内网地址必须由 _base_url() 在探测之前就拦掉。

        刚改完地址或刚换密钥，用 force 立刻重探一次。
        """
        probe_url = self._public_link()
        if not probe_url:
            self._probe_ok = False
            self._probed_at = time.time()
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(probe_url, allow_redirects=True) as resp:
                    await resp.read()
                    # 能收到 HTTP 响应就说明 DNS 和链路都通，剩下的属于配置问题
                    ok = resp.status < 500
        except Exception as exc:
            logger.info(f"[help_dex] 探测对外地址不通: {exc}")
            ok = False
        self._probe_ok = ok
        self._probed_at = time.time()
        logger.info(f"[help_dex] 对外地址探测结果: {'通' if ok else '不通'}（{probe_url}）")
        return ok

    def _on_tunnel_url_changed(self, url: str) -> None:
        """守护进程重开隧道后地址会变，必须立刻回写配置。

        不回写的话：入群发的还是旧地址，而旧地址已经作废。
        临时隧道本来就每次重开都换地址，这里就是它“链接能不能用”的关键一环。
        顺带把之前没发成链接的群补上（隧道就绪 = 现在能发了）。
        """
        if not url:
            return
        changed = str(getattr(self.config, "public_base_url", "") or "").strip() != url
        if changed:
            self.config["public_base_url"] = url
            self._save_config()
            self._probe_ok = None
            self._probed_at = 0.0
            logger.info(f"[help_dex] 隧道地址已更新并写回配置：{url}")
        if self._pending_link:
            asyncio.create_task(self._flush_pending_links())

    async def _resolve_link(self, group_id: str = "", force: bool = False) -> Optional[str]:
        link = self._public_link(group_id)
        if not link:
            return None
        # 存的是 cpolar 地址、但隧道进程根本没在跑：不用等探测也知道打不开。
        # 非 cpolar 地址（用户自配反代等）不做这个判定——那种地址的生命周期
        # 不归这里管。
        base = self._base_url()
        if is_cpolar_url(base) and not self._tunnel.running():
            self._probe_ok = False
            self._probed_at = time.time()
            self._link_down_reason = (
                "隧道进程没在跑，稍等会自动拉起；也可以发 /图鉴隧道 开 看看。"
            )
            return None
        # 进程在跑但地址还没拿到（刚重开、还在握手）：此时发链接必然是死链
        if is_cpolar_url(base) and not self._tunnel.url:
            self._probe_ok = False
            self._probed_at = time.time()
            self._link_down_reason = "隧道刚重开、地址还没就绪，稍等几秒再试。"
            return None
        fresh = (
            self._probe_ok is not None and (time.time() - self._probed_at) < PROBE_TTL
        )
        if fresh and not force:
            return link if self._probe_ok else None
        if await self._probe_public_link():
            return link
        if not self._link_down_reason:
            self._link_down_reason = "这个地址现在访问不到"
        return None

    # -------------------- 入群检测 --------------------
    @staticmethod
    def _is_self_joined(raw, event: AstrMessageEvent) -> bool:
        """只认「Bot 自己进群」，群里别人被拉进来不算。

        框架没有群成员增加事件，OneBot 适配器又把 notice 原样全量转进来，
        所以判据得自己从 raw_message 里读。
        """
        if not isinstance(raw, dict):
            return False
        if str(raw.get("notice_type") or "") != "group_increase":
            return False
        getter = getattr(event, "get_self_id", None)
        self_id = str(getter() or "").strip() if callable(getter) else ""
        return bool(self_id) and str(raw.get("user_id") or "") == self_id

    def _detect_group_join(self, event: AstrMessageEvent) -> Tuple[str, bool]:
        group_id = self._group_of(event)
        if not group_id:
            return "", False
        mode = str(getattr(self.config, "welcome_detect", "notice") or "notice").lower()
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        notice = str(raw.get("notice_type") or "") if isinstance(raw, dict) else ""
        # 每条都记一条，「入群不触发」这种事光看图猜不出来
        self._recent.append({
            "at": time.strftime("%H:%M:%S"),
            "group": group_id,
            "notice": notice or "（普通消息）",
            "self": str(raw.get("user_id") or "") if isinstance(raw, dict) else "",
        })
        del self._recent[:-20]
        if mode == "off":
            return "", False
        if mode in ("notice", "both") and self._is_self_joined(raw, event):
            return group_id, True
        # 兜底：有些 OneBot 实现不会给自己上报 group_increase，
        # 这时「没见过的群号」就是唯一能救场的信号。
        if mode in ("unknown", "both") and group_id not in self._seen_groups:
            return group_id, True
        # ⚠ 这里**不能**再加「群不在已知列表就算新群」之类的兜底。
        # 已知列表在插件刚装、还没跑过第一轮轮询时是空的，那种兜底会在
        # 预热窗口内把群里**任何一条消息**都当成入群——别人说话也发欢迎语。
        # 群列表这条路已经由 _on_group_appeared 独立处理（它比对的是
        # 「列表里真的多了一个群」，只有机器人自己被拉进去才会多），
        # 不需要在这里重复判断，也不该绕过 welcome_detect。
        return "", False

    def _claim_group(self, group_id: str) -> bool:
        """本次运行里这个群已经发过欢迎就返回 False。

        _fired_groups 故意只在内存：被踢之后重新拉进群会再来一条
        group_increase，那时它已经被清空，所以能再发一次。
        """
        if group_id in self._fired_groups:
            return False
        self._fired_groups.add(group_id)
        if group_id not in self._seen_groups:
            self._seen_groups.add(group_id)
            self._save_seen_groups()
        return True

    async def _force_join(self, event: AstrMessageEvent, group_id: str):
        """手动走一遍「进群」流程，不写已触发名单。

        用来把「检测」和「执行」分开验证：执行这边坏了，一发就能看出来；
        检测那边（收没收到进群通知）靠 /图鉴诊断 判断。
        """
        if not group_id:
            yield event.plain_result(
                "用法：/图鉴入群 试 <群号>（在群里发就默认用当前群）"
            )
            return
        self._open_quiet(group_id)
        try:
            quiet = int(getattr(self.config, "welcome_quiet_seconds", 60) or 0)
        except (TypeError, ValueError):
            quiet = 60
        left = self._quiet_left(group_id)
        lines = ["🧪 手动触发一次入群流程（群 {}）。".format(group_id)]
        lines.append(
            "静默窗口：{}".format(
                "已开，还剩 {:.0f} 秒".format(left) if left > 0 else "❌ 没开"
            )
        )
        yield event.plain_result("\n".join(lines))
        link = await self._resolve_link(group_id)
        if link:
            template = (
                str(getattr(self.config, "welcome_text", "") or "").strip()
                or DEFAULT_WELCOME_TEXT
            )
            yield event.plain_result(template.replace("{link}", link))
            return
        yield event.plain_result(
            "⚠️ 对外地址不可用，这次退回发图鉴（和真实入群时行为一致）。"
        )
        image = self._render_image(bot_id=self._bot_id_for(event))
        if image:
            yield event.chain_result([Image.fromBytes(image)])
        else:
            yield event.plain_result("👋（图鉴也渲染失败了，看后台日志）")

    def _open_quiet(self, group_id: str) -> None:
        if not bool(getattr(self.config, "quiet_enabled", True)):
            return
        try:
            seconds = int(getattr(self.config, "welcome_quiet_seconds", 60) or 0)
        except (TypeError, ValueError):
            seconds = 60
        if seconds > 0:
            self._quiet[group_id] = time.time() + seconds

    def _quiet_left(self, group_id: str) -> float:
        deadline = self._quiet.get(group_id, 0.0)
        if deadline <= 0.0:
            return 0.0
        left = deadline - time.time()
        if left <= 0.0:
            self._quiet.pop(group_id, None)
            return 0.0
        return left

    @filter.custom_filter(_BotGroupJoinFilter)
    async def watch_group_join(self, event: AstrMessageEvent):
        """机器人进群：开静默并发引导；机器人离群：清去重，允许同进程重拉再触发。"""
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            notice = str(raw.get("notice_type") or "") if isinstance(raw, dict) else ""
            if notice == "group_decrease":
                self._forget_bot_left(event)
                return
            group_id, joined = self._detect_group_join(event)
        except Exception as exc:
            # 这里的异常会被框架当异常消息发到群里，必须自己吃掉
            logger.warning(f"[help_dex] 入群检测异常: {exc}")
            return
        if not joined:
            return
        # 静默和「发不发欢迎语」是两件事，不该绑在一起：
        # 关掉欢迎语但仍要防撞车的情况是存在的。
        self._open_quiet(group_id)
        if not self._claim_group(group_id):
            logger.info(
                f"[help_dex] 群 {group_id} 这次又收到入群事件，"
                "静默窗口已重开，引导不重复发。"
            )
            return
        logger.info(f"[help_dex] 群 {group_id} 触发入群引导")
        # 事件上的 self_id 就是「刚进群的 bot」。欢迎语必须从那 bot 自己嘴里发，
        # 不能让同机另一个 bot 替它开口。
        via_key = str(event.get_self_id() or "").strip() or None
        if await self._deliver_welcome(group_id, via_key=via_key):
            return
        # 链接还没就绪：已登记待补发（轮询和隧道就绪时都会自动补），
        # 同时先把图鉴图片发出去，不让人干等。
        image = self._render_image(bot_id=self._bot_id_for(event))
        if image:
            await self._announce(event, None, image)
        else:
            await self._announce(event, LINK_PENDING_TEXT)

    @staticmethod
    async def _announce(event: AstrMessageEvent, text: Optional[str] = None,
                        image: Optional[bytes] = None) -> None:
        """把入群欢迎语当**一条独立消息**发出去。

        之前是用 `yield event.plain_result(...)` 挂到事件管线上的，
        那是错的：star_request 里每个 handler 执行完都会
        `event.clear_result()`，只要后面还有别的 handler（我这个插件自己
        就还有一个 EventMessageType.ALL 的 handler），欢迎语就被清掉，
        永远到不了 RespondStage。表现是「进群那一刻不发，过一会儿才冒出来」。

        而且它本来就该是一条普通消息，不是某条指令的回复——
        没人 @ 机器人，这条消息跟当前这条事件没有语义关系。
        顺带的好处：event.send() 会置 _has_send_oper=True，
        ProcessStage 的 LLM 分支判的就是 `not event._has_send_oper`，
        所以发完这一轮天然不会走 LLM。
        """
        chain = MessageChain()
        if image is not None:
            chain.append(Image.fromBytes(image))
        elif text is not None:
            chain.append(Plain(text))
        try:
            await event.send(chain)
        except Exception as exc:
            logger.warning(f"[help_dex] 发送入群欢迎语失败: {exc}")

    @filter.on_waiting_llm_request()
    async def guard_llm(self, event: AstrMessageEvent):
        """静默窗口内只拦「自动回话」，带指令输出的不拦。

        这里是唯一真正能拦住 LLM 的地方：钩子里 raise 会被 call_event_hook
        的 except BaseException 吞掉只打日志，请求照发；只有 stop_event 能让
        它返回 True 从而中断。

        但 stop_event 会连本轮的回复一起停掉，所以分两种：
        - 事件上已经带着指令输出（get_result() 不为 None）→ 不拦，让指令发出去
        - 纯粹聊天、没有任何指令输出 → 拦，这就是「静默」压制的对象
        """
        group_id = self._group_of(event)
        if not group_id or self._quiet_left(group_id) <= 0.0:
            return
        getter = getattr(event, "get_result", None)
        result = getter() if callable(getter) else None
        if result is None:
            event.stop_event()

    # -------------------- 对外指令 --------------------
    @filter.command("帮助图鉴", alias={"指令图鉴"})
    async def help_image(self, event: AstrMessageEvent):
        """生成一张收录所有指令的图鉴图片"""
        if not self.collect_commands():
            yield event.plain_result("暂时没有收集到任何指令，先确认装了别的插件再试试～")
            return
        image = self._render_image(bot_id=self._bot_id_for(event))
        if image is None:
            yield event.plain_result("图鉴绘制失败了，请看后台日志排查。")
        else:
            yield event.chain_result([Image.fromBytes(image)])
        # 私聊时把网页地址也给他，方便转到需要的地方
        if self._is_private(event):
            link = await self._resolve_link()
            if link:
                yield event.plain_result(
                    f"网页版也在这儿（每条指令都能一键复制）：\n{link}"
                )

    @filter.custom_filter(_OnlyAtBotFilter)
    async def at_only_help(self, event: AstrMessageEvent):
        """群里只 @ 机器人（不带任何文字）时，直接发送指令图鉴。

        「是不是只 @ 了机器人」由 _OnlyAtBotFilter 在激活阶段就判完了。
        """
        if not self.collect_commands():
            return
        image = self._render_image(bot_id=self._bot_id_for(event))
        if image is None:
            return
        yield event.chain_result([Image.fromBytes(image)])

    @filter.command("图鉴预览")
    async def preview(self, event: AstrMessageEvent):
        """预览当前图鉴样式与自定义状态"""
        if not self.collect_commands():
            yield event.plain_result("暂时没有收集到任何指令，先确认装了别的插件再试试～")
            return
        image = self._render_image(bot_id=self._bot_id_for(event))
        if image is None:
            yield event.plain_result("图鉴绘制失败了，请看后台日志排查。")
            return
        yield event.chain_result([Image.fromBytes(image)])
        yield event.plain_result(self._style_status(self._bot_id_for(event)))

    @filter.command("图鉴帮助")
    async def plugin_help(self, event: AstrMessageEvent):
        """查看指令图鉴自己的用法"""
        yield event.plain_result(HELP_TEXT)

    # -------------------- 管理员指令 --------------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴背景")
    async def set_background(self, event: AstrMessageEvent):
        """上传/重置帮助图背景图（管理员）

        三种用法：
        - 直接发图/图链：只给「当前这个 bot」设背景
        - /图鉴背景 全局 + 图：给所有 bot 设默认背景
        - /图鉴背景 重置：删当前 bot 的；后面跟「全局」则删全局的
        """
        arg = _arg_after(event.message_str, "图鉴背景")
        current_bot = self._bot_id_for(event)
        if arg in ("重置", "复位", "删除"):
            removed = self.style.clear_background(current_bot)
            if removed:
                yield event.plain_result(
                    "✅ 本 bot（{}）的专属背景图已移除。".format(current_bot or "?")
                )
            else:
                yield event.plain_result(
                    "本 bot 没单独设置过背景图，没什么可重置的。\n"
                    "（想删全局默认背景：/图鉴背景 重置 全局）"
                )
            return
        if arg in ("重置 全局", "复位 全局", "删除 全局", "重置全局", "全局重置"):
            removed = self.style.clear_background(None)
            if removed:
                yield event.plain_result("✅ 全局默认背景图已移除。")
            else:
                yield event.plain_result("本来就没设过全局背景图。")
            return
        target_bot: Optional[str] = current_bot
        if arg in ("全局", "默认") or arg.startswith(("全局 ", "默认 ")):
            target_bot = None
            arg = arg[len("全局"):].strip() if arg.startswith("全局") else arg[len("默认"):].strip()
        elif arg.startswith("重置"):
            pass  # 上面的精确匹配已经处理了
        source, error = await self._resolve_image_source(event, arg, "图鉴背景")
        if error:
            yield event.plain_result(error)
            return
        error = self.style.set_background(source, bot_id=target_bot)
        if error:
            yield event.plain_result(f"❌ {error}")
            return
        if target_bot:
            yield event.plain_result(
                f"✅ 背景图已更新（只对 bot {target_bot} 生效）。\n"
                "发 /帮助图鉴 看看效果～\n"
                "想给所有 bot 用同一张：发 /图鉴背景 全局 + 图片。"
            )
        else:
            yield event.plain_result(
                "✅ 全局默认背景图已更新（所有没单独设过的 bot 都会用它）。\n"
                "发 /帮助图鉴 看看效果～"
            )

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
        yield event.plain_result(
                "✅ Logo 已更新，发 /帮助图鉴 看看效果～\n"
                "（透明底 PNG 效果最好；不透明的图会原样使用，不会自动抠）"
            )

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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴入群")
    async def toggle_welcome(self, event: AstrMessageEvent):
        """开关入群欢迎语与进群静默（管理员）"""
        arg = _arg_after(event.message_str, "图鉴入群")
        if arg.startswith("试"):
            rest = arg[len("试"):].replace("：", " ").replace(":", " ")
            gid = (rest.split() or [""])[0] or self._group_of(event)
            async for item in self._force_join(event, gid):
                yield item
            return
        if arg.startswith("允许"):
            raw = arg[len("允许"):].replace("，", " ").replace(",", " ")
            ids = [item for item in raw.split() if item.strip()]
            if not ids:
                yield event.plain_result(
                    "用法：/图鉴入群 允许 123456789（多个群号用空格或逗号隔开）\n"
                    "把这些群从「已触发过」名单里去掉，下次它们来消息时会重新发欢迎语。"
                )
                return
            cleared = [gid for gid in ids if gid in self._seen_groups]
            for gid in ids:
                self._seen_groups.discard(gid)
            if cleared:
                self._save_seen_groups()
            yield event.plain_result(
                "✅ 已清除 {} 个群的记录。".format(len(cleared))
                + ("（这些群本来就还没触发过）" if not cleared else "")
            )
            return
        if arg in ("开", "开启", "启用", "on"):
            enabled = True
        elif arg in ("关", "关闭", "禁用", "off"):
            enabled = False
        elif arg in ("静默开", "静默"):
            self.config["quiet_enabled"] = True
            self._save_config()
            yield event.plain_result("✅ 进群静默已单独打开。")
            return
        elif arg in ("静默关",):
            self.config["quiet_enabled"] = False
            self._save_config()
            yield event.plain_result("✅ 进群静默已单独关掉（欢迎语不受影响）。")
            return
        else:
            lines = [
                "入群欢迎：{}".format(
                    "开" if bool(getattr(self.config, "welcome_enabled", True)) else "关"
                ),
                "进群静默：{}".format(
                    "开" if bool(getattr(self.config, "quiet_enabled", True)) else "关"
                ),
                "",
                "开关：/图鉴入群 开｜关　　只开关静默：/图鉴入群 静默开｜静默关",
                "让某个群能重新触发：/图鉴入群 允许 <群号>",
                "立刻试一遍发不发：/图鉴入群 试 [群号]（不写已触发名单）",
            ]
            yield event.plain_result("\n".join(lines))
            return
        self.config["welcome_enabled"] = enabled
        self._save_config()
        yield event.plain_result(f"✅ 入群欢迎已{'开启' if enabled else '关闭'}。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴静默")
    async def set_quiet(self, event: AstrMessageEvent):
        """设置进群后多久不自动回话（管理员）"""
        arg = _arg_after(event.message_str, "图鉴静默")
        if not arg:
            current = getattr(self.config, "welcome_quiet_seconds", 60)
            yield event.plain_result(
                "进群静默：{}\n".format(f"{current} 秒" if current else "关")
                + "作用：进群后这段时间，别人 @ 我我不会自动回，\n"
                "避免一群机器人刚进群就抢着说话。\n"
                "**指令不受影响**：/帮助图鉴 这类指令随时照常用。\n"
                "改：/图鉴静默 <秒>（0 关掉）"
            )
            return
        try:
            seconds = int(arg)
        except ValueError:
            yield event.plain_result("请填整数秒，如 /图鉴静默 60；0 = 关掉。")
            return
        seconds = max(0, min(3600, seconds))
        self.config["welcome_quiet_seconds"] = seconds
        self._save_config()
        if seconds == 0:
            yield event.plain_result("✅ 已关掉进群静默，进群后别人 @ 我会正常回话。")
        else:
            yield event.plain_result(
                f"✅ 进群静默 {seconds} 秒。\n"
                f"这段时间只压制自动回话，指令照常能用。"
            )

    def _page_status_lines(self) -> List[str]:
        """网页功能到底是开着还是死着，一眼看得出来。"""
        if self.server is None:
            return [
                "欢迎页服务：❌ 没跑（配置里的 page_enabled 是关的？或者启动时报错了，看后台日志）"
            ]
        lines = [f"欢迎页服务：✅ 在跑，本机 127.0.0.1:{self.server.port}（只监听本机）"]
        raw = str(getattr(self.config, "public_base_url", "") or "").strip()
        kind = classify_base_url(raw if "//" in raw else "//" + raw if raw else "")
        if not raw:
            lines += [
                "对外地址：❌ 没填",
                "所以现在入群发的是图鉴图片，网页功能等于没开。",
            ]
        elif kind == "loopback":
            lines += [
                f"对外地址：❌ {raw}",
                "❗ 这种地址只有这台机器能用，群里的人点 100% 打不开。",
                "探测没用——探的是本机，探 127.0.0.1 当然“通”。",
            ]
        elif kind == "private":
            lines += [
                f"对外地址：⚠️ {raw}",
                "这是内网地址，群里的人在手机上同样打不开。",
            ]
        elif self._probe_ok is True:
            lines.append(f"对外地址：✅ {raw}（已探测通过）")
        elif self._probe_ok is False:
            lines += [
                f"对外地址：❌ {raw}（探测不通）",
                "地址能解析到，但这台机器自己访问不到。",
            ]
        else:
            lines.append(f"对外地址：{raw}（还没探过，发 /图鉴链接 试一次）")
        return lines

    def _setup_guide(self) -> str:
        base = self._base_url()
        if base and self._probe_ok:
            return ""
        if not TunnelManager.is_available():
            return (
                "\n\n发 /图鉴隧道 开 就行：没装 cpolar 会自动下载安装，"
                "缺什么都它自己补。"
            )
        if not TunnelManager.has_token():
            return (
                "\n\n修好只要两步：去 https://www.cpolar.com 注册（免费），"
                "把后台「验证」页的 authtoken 发过来：/图鉴隧道 令牌 <token>。\n"
                "（不用绑卡、不用浏览器授权、不用域名）"
            )
        return "\n\n修好只要一条指令：发 /图鉴隧道 开，它会拉起 cpolar 隧道并把地址换好。"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴链接")
    async def show_link(self, event: AstrMessageEvent):
        """发当前网页地址 / 轮换访问密钥（管理员）"""
        arg = _arg_after(event.message_str, "图鉴链接")
        if arg in ("换", "换密钥", "轮换", "换key"):
            new_key = generate_access_key()
            self.config["page_access_key"] = new_key
            self._save_config()
            if self.server is not None:
                self.server.access_key = new_key
            # 换完密钥立刻重探，否则会拿旧探测结果告诉用户「通」
            link = await self._resolve_link(force=True)
            yield event.plain_result(
                f"✅ 访问密钥已轮换，旧链接即刻失效。\n新地址：{link or '（对外地址不通，先把地址配好）'}"
            )
            return
        if self.server is None:
            yield event.plain_result(
                "⚠️ 欢迎页服务没跑起来，网页功能现在是关的。\n"
                + "\n".join(self._page_status_lines())
                + self._setup_guide()
            )
            return
        link = await self._resolve_link(force=True)
        status = "\n".join(self._page_status_lines())
        if not link:
            # 原因要分清：回环/内网是「白填了」，隧道没跑是「重启后正常现象」，
            # 其余才是「真的打不开」。含糊过去只会让人反复试。
            reason = self._link_down_reason
            if not reason:
                raw = str(getattr(self.config, "public_base_url", "") or "").strip()
                kind = classify_base_url(raw if "//" in raw else "//" + raw if raw else "")
                if kind == "loopback":
                    reason = "❗ 地址白填了（127.0.0.1 / localhost）"
                elif kind == "private":
                    reason = "❗ 地址填的是内网地址，群里的人同样打不开"
                else:
                    reason = "❌ 这个地址打不开"
            yield event.plain_result(
                reason + "\n\n" + status + self._setup_guide()
            )
            return
        yield event.plain_result(
            f"🔗 欢迎页地址：\n{link}\n\n{status}\n"
            "「/图鉴链接 换」可以轮换密钥，旧地址会立刻失效。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴隧道")
    async def tunnel(self, event: AstrMessageEvent):
        """开/关公网隧道，配 cpolar token（管理员）

        开/关/配 token 都是把欢迎页暴露到公网，只认管理员手动下令。
        """
        arg = _arg_after(event.message_str, "图鉴隧道")
        if arg in ("关", "关闭", "off", "停止", "停"):
            await self._tunnel.stop()
            self.config["public_base_url"] = ""
            self._save_config()
            self._probe_ok = None
            self._probed_at = 0.0
            yield event.plain_result(
                "✅ 隧道已关，「对外访问地址」也清空了。\n"
                "现在入群发的是图鉴图片。"
            )
            return
        if arg.startswith(("令牌", "token", "Token")):
            rest = arg[len("令牌"):].strip() if arg.startswith("令牌") else arg[5:].strip()
            for sep in ("=", "：", ":"):
                if rest.startswith(sep):
                    rest = rest[len(sep):].strip()
            rest = rest.split()[0] if rest.split() else ""
            if not rest:
                yield event.plain_result(self._token_guide_text())
                return
            try:
                TunnelManager.set_token(rest)
            except TunnelError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            yield event.plain_result("✅ token 已保存。")
            # 顺手直接把隧道拉起来——用户贴 token 的意图就是要用，
            # 不该再让他多发一条指令。服务没就绪时不算失败：启动后会自动拉起。
            if self.server is None:
                yield event.plain_result(
                    "欢迎页服务没在跑（page_enabled 关着），重新加载插件后会自动拉起隧道。"
                )
                return
            async for line in self._start_tunnel_flow(event):
                yield line
            return
        if not arg or arg not in ("开", "开启", "on", "启动", "起"):
            yield event.plain_result(self._tunnel_status_text())
            return
        if self.server is None:
            yield event.plain_result("⚠️ 欢迎页服务没跑起来，先把 page_enabled 打开。")
            return
        if not TunnelManager.has_token():
            yield event.plain_result(self._token_guide_text())
            return
        async for line in self._start_tunnel_flow(event):
            yield line

    async def _start_tunnel_flow(self, event: AstrMessageEvent):
        """拉起隧道 + 探测 + 回写配置。供「令牌」「开」两条路复用。"""
        if TunnelManager.is_available():
            yield event.plain_result("正在拉起 cpolar 隧道，最多等 45 秒…")
        else:
            yield event.plain_result(
                "首次运行：正在自动下载安装 cpolar 并拉起隧道，最多等一两分钟…"
            )
        try:
            url = await self._tunnel.start(self.server.port)
        except TunnelError as exc:
            yield event.plain_result(f"❌ 没开成：\n{exc}")
            return
        except Exception as exc:
            logger.error(f"[help_dex] 拉起隧道异常: {exc}")
            yield event.plain_result(f"❌ 没开成：{exc}")
            return
        self.config["public_base_url"] = url
        self._save_config()
        self._probe_ok = None
        self._probed_at = 0.0
        link = await self._resolve_link(force=True)
        if link:
            yield event.plain_result(
                f"✅ 地址已就绪：\n{url}\n\n{self._tunnel_status_text()}"
            )
        else:
            yield event.plain_result(
                f"⚠️ 隧道起来了，但探测不通：{url}\n"
                "过一会儿发 /图鉴链接 再试。"
            )

    @staticmethod
    def _token_guide_text() -> str:
        return (
            "📝 就差一步：把 cpolar 的 token 粘进来。\n"
            "\n"
            "1. 手机或电脑打开 https://www.cpolar.com 注册（免费，邮箱就行）\n"
            "2. 登录后找左侧「验证」页，复制那串 authtoken\n"
            "3. 回来发：/图鉴隧道 令牌 <粘上那串>\n"
            "\n"
            "以后就全自动了：启动自动拉、地址变了自动补，不用再碰。\n"
            "免费版：1Mbps 带宽，地址会周期性重置（插件会自动适配）。"
        )

    def _tunnel_status_text(self) -> str:
        token, problem = TunnelManager.token_status()
        lines = ["🚇 隧道状态（cpolar）："]
        if problem:
            lines.append("❗ " + problem)
        if not TunnelManager.is_available():
            lines.append("cpolar 还没装——不用管，发 /图鉴隧道 开 时会自动下载安装。")
        elif self._tunnel.running() and self._tunnel.url:
            lines.append(f"运行中：{self._tunnel.url}")
        else:
            lines.append("没开")
        if token:
            masked = token[:4] + "…" + token[-4:] if len(token) > 10 else "已配置"
            lines.append(f"\ntoken：{masked}（已保存）")
        else:
            lines.append("\ntoken：❌ 还没配 /图鉴隧道 令牌 <token>")
        lines.append("\n开：/图鉴隧道 开　关：/图鉴隧道 关")
        lines.append("⚠️ 开隧道等于把欢迎页开到全互联网（带密钥），链接在群里发过就能被转发。")
        lines.append("隧道只指欢迎页端口，不碰面板 6185。")
        return "\n".join(lines)

    def _framework_traps(self) -> List[str]:
        """框架侧会让入群监听彻底静默失效的三个配置。

        这三个都不是本插件能改的，只能检测出来告诉用户。
        全部来自 AstrBot 源码核实。
        """
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return ["读不到 AstrBot 全局配置，跳过检查"]
        try:
            cfg = getter()
        except Exception as exc:
            return ["读 AstrBot 全局配置失败：{}".format(exc)]
        lines: List[str] = []
        # 陷阱 A：唤醒前缀里有空字符串。群通知事件的消息链是空的，
        # 框架判断 "".startswith("") 为真后去取 messages[0] 会越界崩溃，
        # 整条 pipeline 挂掉，所有插件都不会被调用——而且只在日志里留一行。
        wake = [str(item) for item in (cfg.get("wake_prefix") or [])]
        if any(item.strip() == "" for item in wake):
            lines.append("❌ 框架配置问题：唤醒前缀里有空字符串「」。")
            lines.append("   群通知事件的消息链是空的，框架处理时会越界崩溃，")
            lines.append("   结果是所有插件都不会被调用。去面板把那个空的删掉。")
        platform = cfg.get("platform_settings") or {}
        # 陷阱 B：进群通知的 user_id 就是机器人自己，
        # 打开这个开关会命中框架里最早的那个 return，连 handler 筛选都进不去。
        if platform.get("ignore_bot_self_message"):
            lines.append("❌ 框架配置问题：开启了「忽略机器人自己发送的消息」。")
            lines.append("   进群通知里的 user_id 就是机器人自己，会被这条直接丢掉。")
        # 陷阱 C：非空白名单会把不在名单里的会话整条拦掉
        allow = [str(x).strip() for x in (platform.get("id_whitelist") or []) if str(x).strip()]
        if platform.get("enable_id_white_list") and allow:
            lines.append("⚠️ 框架会话白名单已启用（{} 项）。".format(len(allow)))
            lines.append("   新群不在名单里的事件会被框架丢掉，插件看不见。把群号加进去。")
        # 静默钩子只在 local agent 路径上被调用
        runner = (cfg.get("agent_runner") or {}).get("runner_type", "local")
        if runner != "local":
            lines.append("⚠️ agent_runner 是 {}，静默用的钩子在那条路径上不会被调用。".format(runner))
        if not lines:
            lines.append("✅ 框架配置这边没发现会拦截入群监听的问题")
        return lines

    @filter.command("图鉴轮询")
    async def poll_now(self, event: AstrMessageEvent):
        """手动拉一次群列表，看轮询到底能不能用（所有人可用）"""
        insts = self._platform_instances()
        lines = ["🔍 群列表轮询自检："]
        lines.append("平台实例：{} 个".format(len(insts)))
        lines.append("轮询开关：{}".format(
            "开" if bool(getattr(self.config, "group_poll", True)) else "❌ 关着"
        ))
        lines.append("本机服务端口：{}".format(
            self.server.port if self.server is not None else "没跑"
        ))
        lines.append("已记录基线：{} 个群".format(len(self._known_groups)))
        for inst in insts:
            bot = getattr(inst, "bot", None)
            lines.append("  实例 {}：bot={}，有 call_api={}，有 call_action={}".format(
                self._platform_id(inst) or "?",
                type(bot).__name__,
                callable(getattr(bot, "call_api", None)),
                callable(getattr(bot, "call_action", None)),
            ))
        rows = await self._fetch_groups_per_instance()
        if not rows:
            lines.append("")
            lines.append("❌ 拉不到群列表：{}".format(
                self._last_poll_error or "原因不明"
            ))
            lines.append("入群检测现在只剩通知和陌生群号两条路了。")
        else:
            groups = set()
            for row in rows:
                groups |= row["groups"]
            lines.append("")
            lines.append("✅ 拉到了，当前在 {} 个群（{} 个 bot）：{}".format(
                len(groups), len(rows), "、".join(sorted(groups)[:20]) or "（空）"
            ))
            if len(rows) > 1:
                lines.append("多 bot：各自记基线，谁进群由谁发欢迎：")
                for row in rows:
                    lines.append(
                        "  bot {}：{} 个群 {}".format(
                            row["key"], len(row["groups"]),
                            "、".join(sorted(row["groups"])[:12]) or "（无群）",
                        )
                    )
            fresh = groups - self._known_groups
            if self._known_groups:
                lines.append("基线里有 {} 个群，比对差集：{}".format(
                    len(self._known_groups),
                    "、".join(sorted(fresh)) if fresh else "无新增",
                ))
            else:
                lines.append("还没有基线，下一轮轮询会建立（不会误报新群）")
        yield event.plain_result("\n".join(lines))

    @filter.command("图鉴诊断")
    async def diagnose(self, event: AstrMessageEvent):
        """排查进群为什么不发链接（所有人可用）"""
        enabled = bool(getattr(self.config, "welcome_enabled", True))
        try:
            quiet = int(getattr(self.config, "welcome_quiet_seconds", 60) or 0)
        except (TypeError, ValueError):
            quiet = 60
        lines = ["🔧 图鉴诊断"]
        lines.append("")
        lines.append("【开关】")
        lines.append("进群引导：{}".format("开" if enabled else "❌ 关着"))
        lines.append("静默（只压制自动回话）：{}{}".format(
            "开" if bool(getattr(self.config, "quiet_enabled", True)) else "关",
            "，{} 秒".format(quiet) if quiet else "",
        ))
        lines.append("判定方式：{}".format(_DETECT_LABELS.get(
            str(getattr(self.config, "welcome_detect", "notice") or "notice").lower(),
            getattr(self.config, "welcome_detect", "notice"))))

        lines.append("")
        lines.append("【链接】")
        lines.append("隧道：{}".format(
            "在跑，地址 {}".format(self._tunnel.url or "（还没就绪）")
            if self._tunnel.running() else "❌ 没跑"
        ))
        base = self._base_url()
        lines.append("对外地址：{}".format(base or "❌ 没配（会退回发图鉴，不发死链）"))
        if self._pending_link:
            lines.append("待补发：{}（链接恢复后自动补）".format(
                "、".join(sorted(self._pending_link)[:8])))

        lines.append("")
        lines.append("【检测】")
        lines.append("群列表轮询：{}".format(
            "开，已知 {} 个群".format(len(self._known_groups))
            if (self._poller is not None and bool(getattr(self.config, "group_poll", True)))
            else "❌ 没开"
        ))
        lines.append("已触发过的群：{} 个".format(len(self._seen_groups)))
        if self._seen_groups:
            lines.append("  " + "、".join(sorted(self._seen_groups)[:12]))
            lines.append("  想让某群重新触发：/图鉴入群 允许 <群号>")
        live = {g: s for g, s in self._quiet.items() if s > 0}
        if live:
            lines.append("静默进行中：{}".format(
                "、".join("{}剩{:.0f}秒".format(g, s) for g, s in live.items())))

        lines.append("")
        if self._recent:
            lines.append("【最近 10 条群事件】")
            for row in self._recent[-10:]:
                mark = ""
                if row["notice"] == "group_increase" and row["self"]:
                    mark = "（机器人自己）"
                lines.append("  {} 群{}  {}{}".format(
                    row["at"], row["group"], row["notice"], mark))
        else:
            lines.append("【❌ 一条群事件都没收到】")
            lines.append("说明框架在插件之前就把事件丢了，插件根本没机会看见。")
            lines.append("去面板 → 配置 → 会话白名单，把群号加进去，或关掉白名单。")

        lines.append("")
        lines.append("【框架配置】")
        lines.extend("  " + item for item in self._framework_traps())
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴页面")
    async def show_group_page(self, event: AstrMessageEvent):
        """查看某个群的网页专属配置（管理员）"""
        arg = _arg_after(event.message_str, "图鉴页面")
        group_id = arg.split()[0] if arg else self._group_of(event)
        if not group_id:
            yield event.plain_result("用法：/图鉴页面 <群号>（在群里发就默认看当前群）")
            return
        override = parse_group_override(self.config, group_id)
        link = self._public_link(group_id)
        yield event.plain_result(
            f"群 {group_id} 的网页：\n{describe_override(override)}\n\n"
            f"专属地址：{link or '（还没配对外访问地址）'}\n"
            "改法：插件配置里的「按群定制页面」，一行一条，"
            "格式是 群号|标题|简介|规则1;规则2;规则3，留空的那项不覆盖。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图鉴规则")
    async def set_rules(self, event: AstrMessageEvent):
        """改网页顶部的使用规则（管理员）"""
        arg = _arg_after(event.message_str, "图鉴规则")
        if arg in ("重置", "复位", "清空"):
            self.config["page_rules"] = []
            self._save_config()
            yield event.plain_result("✅ 网页使用规则已清空。")
            return
        current = [str(line) for line in (getattr(self.config, "page_rules", []) or [])]
        if not arg:
            listing = "\n".join(f"{i}. {line}" for i, line in enumerate(current, 1))
            yield event.plain_result(
                "用法：/图鉴规则 <文字>，一行一条\n"
                f"当前规则：\n{listing or '（空）'}"
            )
            return
        rules = [line.strip() for line in arg.splitlines() if line.strip()]
        self.config["page_rules"] = rules
        self._save_config()
        yield event.plain_result(f"✅ 已写入 {len(rules)} 条规则，刷新网页就能看到。")

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
    def _style_status(self, bot_id: Optional[str] = None) -> str:
        lines = ["🎨 当前图鉴自定义状态："]
        own_bg = bool(bot_id) and (self.style.data_dir / background_file_name(bot_id)).is_file()
        global_bg = (self.style.data_dir / "background.img").is_file()
        customized = bool(self.style.colors) or self.style.has_any_background() or self.style.logo_path()
        if bot_id:
            if own_bg:
                lines.append(f"背景图：本 bot（{bot_id}）有专属背景")
            elif global_bg:
                lines.append(f"背景图：本 bot 用全局背景（发图可直接给本 bot 设专属的）")
            else:
                lines.append("背景图：未设置（默认浅色渐变）")
        else:
            lines.append(
                "背景图：{}".format("已设置全局默认" if global_bg else "未设置（默认浅色渐变）")
            )
        others = [b for b in self.style.list_bot_backgrounds() if b != bot_id]
        if others:
            lines.append("其他 bot 的专属背景：{} 个".format(len(others)))
        lines.append(f"Logo：{'已设置' if self.style.logo_path() else '未设置'}")
        changed = [key for key in COLOR_KEYS if key in self.style.colors]
        lines.append(f"自定义颜色：{('、'.join(changed)) if changed else '无'}")
        lines.append(f"标题：{self.style.title_override or '默认'}")
        lines.append(f"简介：{self.style.subtitle_override or '默认'}")
        lines.append("全部恢复默认可用 /图鉴重置" if customized else "全部都是默认样式，用 /图鉴帮助 看怎么装扮～")
        lines.append("")
        lines.extend(self._collect_diagnostic())
        return "\n".join(lines)

    def _collect_diagnostic(self) -> List[str]:
        """到底识别到了什么、漏了什么。

        「图鉴里怎么没有 XX」这类问题，光看图猜不出来，得把扫描结果摊开。
        """
        lines = ["🔍 识别结果："]
        try:
            stars = [star for star in self.context.get_all_stars() if star.activated]
        except Exception as exc:
            return lines + [f"读插件列表失败：{exc}"]

        table = self.collect_commands()
        total = sum(len(rows) for rows in table.values())
        public = {k: [r for r in v if r.get("permission") != "admin"] for k, v in table.items()}
        public = {k: v for k, v in public.items() if v}
        shown = sum(len(v) for v in public.values())
        lines.append(f"已启用插件 {len(stars)} 个，扫描到 {len(table)} 个可列的")
        lines.append(f"图鉴（图片）共 {total} 条；网页只显示非管理员指令 {shown} 条")
        blacked = sorted(
            {
                (getattr(s, "display_name", "") or getattr(s, "name", "") or "?")
                for s in stars
                if (getattr(s, "name", "") or "") in SKIP_STARS
            }
        )
        if blacked:
            lines.append("框架本体不列：" + "、".join(blacked))
        blacklisted = [
            (getattr(s, "display_name", "") or getattr(s, "name", "") or "?")
            for s in stars
            if (getattr(s, "name", "") or getattr(s, "display_name", "")) in set(
                getattr(self.config, "plugin_blacklist", []) or []
            )
        ]
        if blacklisted:
            lines.append("被黑名单挡住：" + "、".join(sorted(set(blacklisted))))
        if not table:
            lines.append("⚠️ 一条指令都没扫到，确认装了别的插件，且没全被黑名单挡了。")
        return lines

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
