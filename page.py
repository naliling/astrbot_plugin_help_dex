"""入群欢迎页：插件自建的只读小 HTTP 服务 + 浅色卡片页面。

为什么不挂到 Dashboard 上：`context.register_web_api` 出来的路由只会落在
`/api/plug/<插件名>/...` 和 `/api/v1/plugins/extensions/<插件名>/...` 两处，
两套挂载点都被鉴权中间件挡着（后者用 require_plugin_scope 依赖），
群里的人点开只会拿到 401。框架也没提供起无鉴权服务的 API。
所以这里自己起一个：只绑 127.0.0.1、只读、URL 路径本身就是访问密钥。

对外只暴露一个 GET /<密钥>/。路径不匹配、密钥不对、方法不对，
一律回同一张 404 页面，不区分——不给人试探的反馈。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import secrets
import shutil
import socket
from html import escape
from ipaddress import ip_address
from pathlib import Path
from string import Template
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from aiohttp import web

from .draw import HelpImageRenderer, _group_sections, _read_metadata_value
from .style import StyleStore

# 不用 6185/6186/6187 这种连号：撞车概率高，看着也像默认端口。
# 真撞了就按下面的偏移往后找，落在哪个都不会是连号。
DEFAULT_PORT = 41783
PORT_CANDIDATE_OFFSETS = (0, 7, 21, 63, 111, 259)

BIND_HOST = "127.0.0.1"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

LOGO_HEIGHT = 128
LOGO_MAX_BYTES = 512 * 1024

NOT_FOUND_HTML = (
    "<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
    '<meta name=viewport content="width=device-width,initial-scale=1">'
    "<title>404</title></head><body></body></html>"
)

# 对外不泄露任何框架标识，也不告诉对端这是不是一个页面。
RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


LOOPBACK_HINT = (
    "你填的是 127.0.0.1 / localhost，这种地址只有跑 AstrBot 的这台机器能用。"
    "群里的人在手机上点，127.0.0.1 指的是他自己的手机，必然打不开。"
)


def classify_base_url(base: str) -> str:
    """对外地址分三类：''（公网，能用）/ 'loopback'（本机回环）/ 'private'（内网）。

    为什么要专门分出来：探测是从服务器自己发出的，探 127.0.0.1 当然通，
    于是会报「已探测通过」——探是探到了，但没有验证别人能不能打开。
    填回环地址时群里的人 100% 打不开，必须在探测之前就拦住。
    """
    text = str(base or "").strip()
    if not text:
        return ""
    candidate = text if "//" in text else "//" + text
    try:
        host = (urlsplit(candidate).hostname or "").strip().strip("[]").lower()
    except ValueError:
        return "loopback"
    if host in ("localhost", "0.0.0.0", "::", ""):
        return "loopback"
    if host == "::1":
        return "loopback"
    try:
        address = ip_address(host)
    except ValueError:
        return ""
    if address.is_loopback:
        return "loopback"
    if address.is_private or address.is_link_local or address.is_unspecified:
        return "private"
    return ""


def generate_access_key() -> str:
    return secrets.token_hex(16)


def sanitize_access_key(raw: object) -> Optional[str]:
    """把配置里的密钥收拾成能安全塞进 URL 路径的形式，不合格返回 None。"""
    text = str(raw or "").strip()
    return text if KEY_PATTERN.match(text) else None


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((BIND_HOST, port))
        except OSError:
            return False
    return True


def pick_port(preferred: int) -> int:
    for offset in PORT_CANDIDATE_OFFSETS:
        candidate = preferred + offset
        if 1024 < candidate < 65536 and port_is_free(candidate):
            return candidate
    raise OSError("找不到可用端口（已试过 %d 起的若干候选）" % preferred)


def _esc(value: object) -> str:
    """页面里所有动态文本的唯一出口。

    指令描述来自各插件的 docstring，规则文本是管理员手填的，
    都可能带着尖括号，必须在这里全转义。
    """
    return escape(str(value or ""), quote=True)


def logo_data_uri(style: StyleStore) -> Optional[str]:
    """Logo 缩到 128px 高再内嵌，避免一张 2MB 原图把整个页面拖慢。"""
    path = style.logo_path()
    if not path:
        return None
    try:
        from PIL import Image

        image = Image.open(path).convert("RGBA")
        if not HelpImageRenderer._has_real_alpha(image):
            image = HelpImageRenderer._remove_near_white(image)
        width, height = image.size
        scaled = image.resize(
            (max(1, int(LOGO_HEIGHT * width / height)), LOGO_HEIGHT),
            Image.Resampling.LANCZOS,
        )
        buffer = io.BytesIO()
        scaled.save(buffer, format="PNG", optimize=True)
        payload = buffer.getvalue()
    except Exception:
        return None
    if len(payload) > LOGO_MAX_BYTES:
        return None
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{
  margin:0;color:var(--ink);background:var(--bg);
  background-image:linear-gradient(180deg,#F7F9FC 0%,#E9EFF7 100%);
  font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Hiragino Sans GB",
    "Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  line-height:1.6;-webkit-font-smoothing:antialiased;
  min-height:100vh;min-height:100svh;
}
a{color:inherit}
.wrap{max-width:1140px;margin:0 auto;padding:clamp(18px,4vw,44px) clamp(16px,4vw,40px)}

/* 滚动进度条：只动 transform，不触发重排 */
#bar{position:fixed;left:0;top:0;height:2px;width:100%;z-index:30;
  background:linear-gradient(90deg,var(--accent),#6FB7F0);
  transform:scaleX(0);transform-origin:0 50%;will-change:transform}

/* 顶部身份区 */
.hero{display:flex;gap:18px;align-items:center;background:var(--card);
  border:1px solid var(--line);border-radius:22px;
  padding:clamp(18px,3vw,30px);
  box-shadow:0 1px 2px rgba(22,32,46,.04),0 14px 34px -22px rgba(22,32,46,.28)}
.hero .mark{flex:0 0 auto;height:clamp(44px,6vw,58px);width:auto;max-width:44vw;
  object-fit:contain;filter:drop-shadow(0 2px 6px rgba(22,32,46,.10))}
.hero .txt{min-width:0}
.hero h1{margin:0;font-size:clamp(21px,2.6vw,30px);font-weight:800;letter-spacing:-.02em;
  overflow-wrap:anywhere}
.hero p{margin:6px 0 0;color:var(--ink-2);font-size:clamp(13px,1.4vw,15px);
  overflow-wrap:anywhere}
.hero .meta{margin-top:12px;display:flex;flex-wrap:wrap;gap:8px}
.tag{border:1px solid var(--line);border-radius:999px;padding:3px 11px;
  font-size:12px;color:var(--ink-2);background:#FBFCFE}

/* 使用规则：顶部最醒目的一张卡 */
.rules{margin-top:16px;background:var(--card);border:1px solid var(--line);
  border-left:3px solid var(--accent);border-radius:16px;
  padding:clamp(16px,2.4vw,24px);
  box-shadow:0 1px 2px rgba(22,32,46,.04),0 10px 26px -20px rgba(22,32,46,.24)}
.rules h2{margin:0 0 10px;font-size:15px;font-weight:700;display:flex;
  align-items:center;gap:8px}
.rules h2::before{content:"";width:7px;height:7px;background:var(--accent);
  transform:rotate(45deg);border-radius:1px}
.rules ol{margin:0;padding-left:20px;color:var(--ink-2);font-size:14px}
.rules li+li{margin-top:6px}
.rules li::marker{color:var(--accent);font-weight:700}
@media (min-width:720px){
  .rules ol{columns:2;column-gap:36px}
  .rules li+li{margin-top:8px}
}

/* 粘性分区导航：底色跟页面底色一致，再加一条分隔线，
   不然宽屏下像一条没画完的色块 */
#nav{position:sticky;top:0;z-index:20;margin:18px -4px 0;padding:10px 4px;
  display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;
  background:rgba(236,242,249,.94);border-bottom:1px solid var(--line)}
#nav::-webkit-scrollbar{display:none}
#nav a{flex:0 0 auto;text-decoration:none;font-size:13px;color:var(--ink-2);
  border:1px solid var(--line);border-radius:999px;padding:6px 14px;
  background:var(--card);transition:color .2s var(--ease),border-color .2s var(--ease)}
#nav a.on{color:#fff;background:var(--accent);border-color:var(--accent)}

/* 指令分区 */
section{margin-top:34px;scroll-margin-top:76px}
.sec-h{display:flex;align-items:center;gap:10px;margin:0 0 14px;
  font-size:clamp(15px,1.8vw,18px);font-weight:700}
.sec-h::before{content:"";width:8px;height:8px;background:var(--accent);
  transform:rotate(45deg);border-radius:1px;flex:0 0 auto}
.sec-h .n{margin-left:auto;font-size:12px;font-weight:500;color:var(--ink-3)}
.grid{display:grid;gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(clamp(224px,18vw,300px),1fr))}
.cmd{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:14px 16px;min-width:0;position:relative;
  box-shadow:0 1px 2px rgba(22,32,46,.03)}
.cmd .k{color:var(--accent);font-weight:700;font-size:15px;overflow-wrap:anywhere;
  padding-right:54px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.cmd .d{margin-top:5px;color:var(--ink-2);font-size:13.5px;overflow-wrap:anywhere}

/* 一键复制。默认就看得见——之前写成 opacity:0 靠 hover，
   结果电脑端打开的人根本不知道有这功能。桌面降为半透明，移上去变实。 */
.copy{position:absolute;top:9px;right:9px;border:1px solid var(--line);background:#FBFCFE;
  color:var(--ink-2);font:inherit;font-size:11.5px;line-height:1;padding:5px 9px;
  border-radius:8px;cursor:pointer;
  transition:opacity .18s var(--ease),color .18s var(--ease),border-color .18s var(--ease)}
.copy:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.copy.ok{color:#0E8A5F;border-color:#BFE6D3;background:#F3FBF7}
@media (hover:hover){
  .copy{opacity:.42}
  .cmd:hover .copy{opacity:1}
}

.foot{margin:40px 0 8px;text-align:center;color:var(--ink-3);font-size:12.5px}

/* 入场动效：只碰 transform 和 opacity，安卓上才不会掉帧 */
.js .reveal{opacity:0;transform:translateY(18px);
  transition:opacity .5s var(--ease),transform .5s var(--ease)}
.js .reveal.in{opacity:1;transform:none}
@media (hover:hover) and (pointer:fine){
  .cmd{transition:transform .22s var(--ease),box-shadow .22s var(--ease),
    border-color .22s var(--ease)}
  .cmd:hover{transform:translateY(-2px);border-color:#CFE0F2;
    box-shadow:0 2px 4px rgba(22,32,46,.05),0 16px 30px -20px rgba(22,32,46,.30)}
}
@media (prefers-reduced-motion:reduce){
  html{scroll-behavior:auto}
  .js .reveal{opacity:1;transform:none;transition:none}
  *{animation-duration:.001ms!important;transition-duration:.001ms!important}
}
"""

# 只做本地滚动动画：不发任何网络请求，也没有 $ 字面量（Template 转义要求）
SCRIPT = """
(function(){
  var bar=document.getElementById('bar');
  var root=document.documentElement;
  var pending=false;
  function paint(){
    var max=root.scrollHeight-root.clientHeight;
    bar.style.transform='scaleX('+(max>0?Math.min(1,root.scrollTop/max):0).toFixed(4)+')';
    pending=false;
  }
  function onScroll(){if(!pending){pending=true;requestAnimationFrame(paint)}}
  addEventListener('scroll',onScroll,{passive:true});
  addEventListener('resize',onScroll,{passive:true});
  paint();

  var items=[].slice.call(document.querySelectorAll('.reveal'));
  if('IntersectionObserver' in window){
    var seen=new IntersectionObserver(function(rows){
      rows.forEach(function(row){
        if(row.isIntersecting){row.target.classList.add('in');seen.unobserve(row.target)}
      });
    },{rootMargin:'0px 0px -6% 0px',threshold:.06});
    items.forEach(function(el){seen.observe(el)});
  }else{
    items.forEach(function(el){el.classList.add('in')});
  }

  var links=[].slice.call(document.querySelectorAll('#nav a'));
  var byId={};
  links.forEach(function(a){byId[a.getAttribute('href').slice(1)]=a});
  var secs=[].slice.call(document.querySelectorAll('section[id]'));
  if('IntersectionObserver' in window&&secs.length){
    var mark=new IntersectionObserver(function(rows){
      rows.forEach(function(row){
        var a=byId[row.target.id];
        if(a&&row.isIntersecting){
          links.forEach(function(x){x.classList.remove('on')});
          a.classList.add('on');
        }
      });
    },{rootMargin:'-76px 0px -70% 0px'});
    secs.forEach(function(s){mark.observe(s)});
  }

  // 一键复制指令。http 下 clipboard API 不可用，退回 execCommand。
  var copies=[].slice.call(document.querySelectorAll('.copy'));
  copies.forEach(function(btn){
    btn.addEventListener('click',function(){
      var text=btn.getAttribute('data-cmd')||'';
      if(!text){return}
      var revert=function(){
        btn.classList.add('ok');
        btn.textContent='已复制';
        setTimeout(function(){
          btn.classList.remove('ok');
          btn.textContent='复制';
        },1200);
      };
      var fallback=function(){
        var area=document.createElement('textarea');
        area.value=text;
        area.setAttribute('readonly','');
        area.style.cssText='position:fixed;top:-1000px;opacity:0';
        document.body.appendChild(area);
        area.select();
        area.setSelectionRange(0,text.length);
        var ok=false;
        try{ok=document.execCommand('copy')}catch(e){}
        document.body.removeChild(area);
        if(ok){revert()}
      };
      if(navigator.clipboard&&window.isSecureContext){
        navigator.clipboard.writeText(text).then(revert,fallback);
      }else{
        fallback();
      }
    });
  });
})();
"""

PAGE = Template(
    """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light">
<meta name="referrer" content="no-referrer">
<title>${title}</title>
<script>document.documentElement.className='js'</script>
<style>:root{--bg:#E9EFF7;--card:#fff;--ink:#16202E;--ink-2:#55637A;--ink-3:#8A97AC;
--line:#E4EAF2;--accent:#2F80D8;--ease:cubic-bezier(.22,.68,.36,1)}
${css}</style>
</head><body>
<div id="bar"></div>
<div class="wrap">
  <header class="hero">
    ${logo}
    <div class="txt">
      <h1>${title}</h1>
      <p>${subtitle}</p>
      <div class="meta">${meta}</div>
    </div>
  </header>
  ${rules}
  <nav id="nav">${nav}</nav>
  ${body}
  <p class="foot">${footer}</p>
</div>
<script>${script}</script>
</body></html>"""
)


def _rules_block(rules: List[str]) -> str:
    items = [line.strip() for line in rules if str(line or "").strip()]
    if not items:
        return ""
    lines = "".join(f"<li>{_esc(line)}</li>" for line in items)
    return (
        '<section class="rules reveal" style="--i:1"><h2>使用规则</h2>'
        f"<ol>{lines}</ol></section>"
    )


def _nav_links(sections) -> str:
    links = "".join(
        f'<a href="#s{index}">{_esc(name)}</a>' for index, (name, _) in enumerate(sections)
    )
    return links


def _section_blocks(sections) -> str:
    chunks = []
    for index, (name, commands) in enumerate(sections):
        cards = "".join(
            '<div class="cmd"><div class="k">/{}</div>{}'
            '<button class="copy" type="button" data-cmd="{}">复制</button></div>'.format(
                _esc(command),
                f'<div class="d">{_esc(desc)}</div>' if desc else "",
                _esc("/" + str(command)),
            )
            for command, desc in commands
        )
        chunks.append(
            f'<section id="s{index}" class="reveal" style="--i:{min(index + 2, 6)}">'
            f'<h2 class="sec-h">{_esc(name)}<span class="n">{len(commands)} 条</span></h2>'
            f'<div class="grid">{cards}</div></section>'
        )
    return "".join(chunks)


def render_page_html(
    *,
    title: str,
    subtitle: str,
    logo_uri: Optional[str],
    rules: List[str],
    sections,
    footer: str,
) -> str:
    total = sum(len(commands) for _, commands in sections)
    logo = f'<img class="mark" src="{_esc(logo_uri)}" alt="">' if logo_uri else ""
    meta = "".join(
        f'<span class="tag">{_esc(text)}</span>'
        for text in (f"{len(sections)} 个板块", f"{total} 条指令")
    )
    return PAGE.safe_substitute(
        title=_esc(title),
        subtitle=_esc(subtitle),
        logo=logo,
        meta=meta,
        rules=_rules_block(rules),
        nav=_nav_links(sections),
        body=_section_blocks(sections) or '<p class="foot">暂时没有可公开的指令。</p>',
        footer=_esc(footer),
        css=CSS,
        script=SCRIPT,
    )


GROUP_SEPARATOR = "|"
RULE_SEPARATOR = ";"


def parse_group_override(config, group_id: str) -> dict:
    """按群覆盖标题 / 简介 / 使用规则。

    配置每行一条：群号|标题|简介|规则1;规则2;规则3
    字段用 | 分，规则用 ; 分；留空就是那一项不覆盖。
    返回的 dict 里只含实际被覆盖的键。
    """
    want = str(group_id or "").strip()
    if not want:
        return {}
    for line in getattr(config, "group_page_overrides", []) or []:
        parts = str(line or "").split(GROUP_SEPARATOR)
        if len(parts) < 2 or parts[0].strip() != want:
            continue
        override: dict = {}
        if parts[1].strip():
            override["title"] = parts[1].strip()
        if len(parts) > 2 and parts[2].strip():
            override["subtitle"] = parts[2].strip()
        if len(parts) > 3:
            rules = [r.strip() for r in parts[3].split(RULE_SEPARATOR) if r.strip()]
            if rules:
                override["rules"] = rules
        return override
    return {}


def describe_override(override: dict) -> str:
    if not override:
        return "（没配，走全局的标题/简介/规则）"
    lines = []
    if "title" in override:
        lines.append(f"标题：{override['title']}")
    if "subtitle" in override:
        lines.append(f"简介：{override['subtitle']}")
    for index, rule in enumerate(override.get("rules") or [], 1):
        lines.append(f"规则 {index}：{rule}")
    return "\n".join(lines)


def build_page_payload(*, config, style: StyleStore, commands, group_id: str = "") -> dict:
    """给 PageServer 的页面组装入口。

    commands 必须已经是剔掉管理员指令的公开集合（页面对公网开放，
    管理类指令一律不上页）。背景图不内嵌：一张 2MB 的图会让整页变重，
    网页只用干净的浅色卡片。
    """
    override = parse_group_override(config, group_id)
    return {
        "title": override.get("title") or style.effective_title(
            str(getattr(config, "title_help", "") or "").strip() or "指令图鉴"
        ),
        "subtitle": override.get("subtitle") or style.effective_subtitle(
            str(getattr(config, "title_desc", "") or "").strip()
            or "这里收录了我会的所有指令"
        ),
        "logo_uri": logo_data_uri(style),
        "rules": override.get("rules")
        or [str(line) for line in (getattr(config, "page_rules", []) or [])],
        "sections": _group_sections(config, commands),
        "footer": page_footer(config),
    }


async def _strip_server_header(request: web.Request, response: web.StreamResponse) -> None:
    """aiohttp 是在发送阶段才拼上 Server: Python/x.y aiohttp/z，
    那等于对外报版本，构造 Response 时 pop 掉没用，只能在发送前删。"""
    if "Server" in response.headers:
        del response.headers["Server"]


class PageServer:
    """只读欢迎页服务。启动挂在 initialize()，关闭挂在 terminate()，
    这样插件热重载后不会留下一个指向已卸载模块的孤儿服务。"""

    def __init__(
        self,
        style: StyleStore,
        access_key: str,
        preferred_port: int,
        page_builder: Callable[..., dict],
    ) -> None:
        self.style = style
        self.access_key = access_key
        self.preferred_port = preferred_port
        self.page_builder = page_builder
        self.port: Optional[int] = None
        self._runner: Optional[web.AppRunner] = None

    def base_url(self, public_base: str) -> str:
        return "{}/{}".format(str(public_base or "").rstrip("/"), self.access_key)

    def link(self, public_base: str, group_id: str = "") -> str:
        """群号走 ?g= 查询参数。猜错或没传都走全局版，不泄露信息差异。"""
        url = self.base_url(public_base) + "/"
        gid = str(group_id or "").strip()
        return "{}?g={}".format(url, quote(gid, safe="")) if gid else url

    async def start(self) -> int:
        self.port = pick_port(self.preferred_port)
        app = web.Application(middlewares=[self._gate])
        app.on_response_prepare.append(_strip_server_header)
        app.router.add_get("/{key}/", self._serve)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, BIND_HOST, self.port).start()
        return self.port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self.port = None

    @web.middleware
    async def _gate(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except (web.HTTPNotFound, web.HTTPMethodNotAllowed):
            return self._not_found()
        except web.HTTPException as exc:
            return self._not_found() if exc.status >= 400 else exc

    def _not_found(self) -> web.Response:
        return web.Response(
            body=NOT_FOUND_HTML.encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
            status=404,
            headers=RESPONSE_HEADERS,
        )

    async def _serve(self, request: web.Request) -> web.Response:
        if not secrets.compare_digest(request.match_info.get("key", ""), self.access_key):
            return self._not_found()
        try:
            # g 只是用来查按群覆盖表的。猜错就走全局版，不泄露任何信息差异。
            group_id = str(request.query.get("g", "") or "").strip()
            payload = self.page_builder(group_id)
            html = render_page_html(**payload)
        except Exception:
            return self._not_found()
        return web.Response(
            body=html.encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
            headers=RESPONSE_HEADERS,
        )


class TunnelError(RuntimeError):
    """隧道没开起来，或者环境里根本没有 cloudflared。"""


INSTALL_HINT = (
    "容器里没装 cloudflared。装一个（arm64）：\n"
    "  cd /tmp && curl -L -o cf.deb "
    "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    "cloudflared-linux-arm64.deb && dpkg -i cf.deb\n"
    "装完再发一次同样的指令。"
)

CLOUDFLARE_DIR = Path.home() / ".cloudflared"
CONFIG_YML = CLOUDFLARE_DIR / "config.yml"
CERT_PEM = CLOUDFLARE_DIR / "cert.pem"
TUNNEL_NAME = "helpdex"

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HOSTNAME_RE = re.compile(r"hostname:\s*([A-Za-z0-9.-]+)")
_CREDENTIALS_RE = re.compile(r"credentials-file:\s*(\S+)")
_TUNNEL_FIELD_RE = re.compile(r"^tunnel:\s*(\S+)", re.M)


class TunnelManager:
    """把欢迎页开到公网。

    两种模式：
      quick —— cloudflared 临时隧道，零配置，但地址每次重启都变。
      named —— 具名隧道，要自己的域名。地址永久固定，容器重启后自动恢复。

    临时隧道还有个实际问题：它是一个独立进程，机器休眠、网络抖动、
    插件热重载都可能把它带走。所以这里带一个守护循环，死了自动拉起。
    """

    QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    AUTH_URL_RE = re.compile(r"https://dash\.cloudflare\.com/\S+")
    BOOT_TIMEOUT = 30.0
    WATCH_INTERVAL = 20.0

    def __init__(self, on_url_changed: Optional[Callable[[str], None]] = None) -> None:
        self.mode = ""
        self.port = 0
        self.proc = None
        self.url = None
        self.on_url_changed = on_url_changed
        self._ready = None
        self._reader = None
        self._login_proc = None
        self._watcher = None
        self._watching = None
        self._booted_at = 0.0

    # ---------- 环境 ----------

    @staticmethod
    def is_available() -> bool:
        return shutil.which("cloudflared") is not None

    @staticmethod
    def named_status() -> Tuple[Optional[str], str]:
        """读具名隧道配置并校验它是不是真的。返回 (域名, 问题描述)。

        只查「凭据文件在不在」是不够的：手滑写进去的路径可能碰巧还指向
        一个真实文件。真正能判死的是**内容**——cloudflared 签发的凭据里
        必带 TunnelID，且必须和 config.yml 里的 tunnel 字段一致
        （官方文档：凭据文件名就是 <TUNNEL-UUID>.json，字段含 TunnelID /
        TunnelSecret / AccountTag）。
        """
        try:
            text = CONFIG_YML.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, ""
        except Exception as exc:
            return None, "{} 读不了：{}".format(CONFIG_YML, exc)

        tunnel_field = _TUNNEL_FIELD_RE.search(text)
        if not tunnel_field or not _UUID_RE.match(tunnel_field.group(1)):
            return None, (
                "{} 里的 tunnel 字段缺失或不是合法隧道 UUID——"
                "这份配置无效（多半是手滑写进去的），删掉它即可。".format(CONFIG_YML)
            )
        wanted_id = tunnel_field.group(1)

        found = _HOSTNAME_RE.search(text)
        if not found:
            return None, "{} 里没有 hostname——这份配置无效。".format(CONFIG_YML)
        hostname = found.group(1).strip()

        creds_field = _CREDENTIALS_RE.search(text)
        if not creds_field:
            return None, (
                "{} 里没有 credentials-file——这份配置无效。".format(CONFIG_YML)
            )
        creds_path = Path(creds_field.group(1))
        if not creds_path.is_file():
            return None, (
                "凭据文件 {} 不存在——这份配置无效，删掉 {} 即可。".format(
                    creds_path, CONFIG_YML
                )
            )
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return None, "凭据文件 {} 不是合法 JSON——这份配置无效（{}）。".format(
                creds_path, exc
            )
        if not isinstance(creds, dict):
            return None, "凭据文件 {} 的内容不是对象——这份配置无效。".format(creds_path)
        real_id = str(creds.get("TunnelID") or "").strip()
        if not real_id:
            return None, (
                "凭据文件 {} 里没有 TunnelID 字段，不是 cloudflared 签发的凭据——"
                "这份配置无效，删掉 {} 即可。".format(creds_path, CONFIG_YML)
            )
        if real_id != wanted_id:
            return None, (
                "凭据文件的 TunnelID（{}）和配置里写的 tunnel（{}）对不上——"
                "这份配置无效，删掉 {} 即可。".format(real_id, wanted_id, CONFIG_YML)
            )
        return hostname, ""

    @staticmethod
    def named_hostname() -> Optional[str]:
        """具名隧道配好的域名。配了就优先用它——地址固定，重启不丢。"""
        return TunnelManager.named_status()[0]

    @staticmethod
    def has_cert() -> bool:
        return CERT_PEM.is_file()

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def login_pending(self) -> bool:
        return self._login_proc is not None and self._login_proc.returncode is None

    # ---------- 进程 ----------

    async def _spawn(self, args: List[str]) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            "cloudflared",
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._ready = asyncio.Event()
        self._reader = asyncio.create_task(self._drain())
        # 进程刚建，给它一个 startup 期限；超时仍未拿到地址就判失败。
        # 不能只靠 running() 判断——进程活着但连不上 Cloudflare 时，
        # 地址永远拿不到，链接就会一直是死的。
        self._booted_at = asyncio.get_running_loop().time()

    def ready_for(self, seconds: float = 0.0) -> bool:
        """地址是否已经拿到。guard 用：进程在跑 ≠ 链接可用。"""
        return bool(self.url) and self.running()

    async def _drain(self) -> None:
        """cloudflared 把地址打在 stderr。必须一直读干净，
        否则管道写满会把 cloudflared 自己卡死。"""
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                if self.url:
                    continue
                found = None
                if self.mode == "named":
                    hostname = self.named_hostname()
                    found = "https://" + hostname if hostname else None
                else:
                    match = self.QUICK_URL_RE.search(line.decode("utf-8", "replace"))
                    found = match.group(0) if match else None
                if found:
                    self.url = found
                    if self._ready is not None:
                        self._ready.set()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _wait_ready(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.stop()
            raise TunnelError(
                "等了 {} 秒还没拿到地址。可能是这台机器连不上 Cloudflare，"
                "看后台日志里 cloudflared 的报错。".format(int(timeout))
            )

    async def _run_capture(self, args: List[str], timeout: float = 90.0) -> str:
        proc = await asyncio.create_subprocess_exec(
            "cloudflared", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise TunnelError("cloudflared {} 超时了".format(" ".join(args)))
        text = (out or b"").decode("utf-8", "replace")
        if proc.returncode:
            raise TunnelError(
                "cloudflared {} 失败了：\n{}".format(" ".join(args), text.strip()[-500:])
            )
        return text

    # ---------- 临时隧道 ----------

    async def start_quick(self, port: int, keep_watchdog: bool = False) -> str:
        self.mode = "quick"
        self.port = port
        await self._spawn(["tunnel", "--no-autoupdate", "--url",
                           "http://127.0.0.1:{}".format(port)])
        await self._wait_ready(self.BOOT_TIMEOUT)
        if not keep_watchdog:
            self._start_watchdog()
        return self.url or ""

    # ---------- 具名隧道（固定地址） ----------

    async def begin_login(self) -> str:
        """跑 tunnel login，把授权链接抓出来给用户点。

        cloudflared 会一直等回调，所以这个进程必须留着——
        用户在浏览器点完之后它自己会退出并写下 cert.pem。
        """
        if not self.is_available():
            raise TunnelError(INSTALL_HINT)
        await self.cancel_login()
        self._login_proc = await asyncio.create_subprocess_exec(
            "cloudflared", "tunnel", "login",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        url = ""
        try:
            for _ in range(60):
                line = await asyncio.wait_for(self._login_proc.stderr.readline(), timeout=30)
                if not line:
                    break
                found = self.AUTH_URL_RE.search(line.decode("utf-8", "replace"))
                if found:
                    url = found.group(0).rstrip("'\"")
                    break
        except asyncio.TimeoutError:
            pass
        if not url:
            await self.cancel_login()
            raise TunnelError(
                "没拿到 cloudflare 的授权链接。多半是这台机器已经登录过了，"
                "可以直接发 /图鉴隧道 固定 <你的域名>。"
            )
        return url

    async def cancel_login(self) -> None:
        if self._login_proc is not None and self._login_proc.returncode is None:
            self._login_proc.terminate()
            try:
                await asyncio.wait_for(self._login_proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._login_proc.kill()
        self._login_proc = None

    async def create_named(self, hostname: str, port: int = 0) -> str:
        """建隧道 + 把域名指过来 + 写好 config.yml。

        域名必须已经托管在 Cloudflare，否则 route dns 会失败。
        """
        if not self.is_available():
            raise TunnelError(INSTALL_HINT)
        hostname = str(hostname or "").strip().lower()
        if not hostname or "." not in hostname:
            raise TunnelError("用法：/图鉴隧道 固定 help.你的域名.com")
        if not self.has_cert():
            raise TunnelError(
                "还没登录过 cloudflare 账号。先发 /图鉴隧道 登录，"
                "在弹出来的链接上点一下授权。"
            )
        created = await self._run_capture(["tunnel", "create", TUNNEL_NAME])
        joined = ""
        if CLOUDFLARE_DIR.is_dir():
            joined = "|".join(item.name for item in CLOUDFLARE_DIR.glob("*.json"))
        found = _UUID_RE.search(created) or _UUID_RE.search(joined)
        if not found:
            raise TunnelError("建隧道成功但没找到隧道 ID，看后台日志。")
        tunnel_id = found.group(0)
        credentials = CLOUDFLARE_DIR / (tunnel_id + ".json")
        if not credentials.is_file():
            raise TunnelError("找不到隧道凭据文件 {}。".format(credentials))
        await self._run_capture(["tunnel", "route", "dns", tunnel_id, hostname])
        # 必须写实际端口：41783 被占时服务会自动顺延到别的端口，
        # 这里还写 41783 的话固定隧道会指到一个没人监听的端口。
        actual_port = int(port or self.port or DEFAULT_PORT)
        CONFIG_YML.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_YML.write_text(
            "tunnel: {}\ncredentials-file: {}\ningress:\n"
            "  - hostname: {}\n    service: http://127.0.0.1:{}\n"
            "  - service: http_status:404\n".format(
                tunnel_id, credentials, hostname, actual_port
            ),
            encoding="utf-8",
        )
        return hostname

    async def start_named(self, port: int, keep_watchdog: bool = False) -> str:
        hostname = self.named_hostname()
        if not hostname:
            raise TunnelError(
                "还没配具名隧道。发 /图鉴隧道 固定 help.你的域名.com"
            )
        self.mode = "named"
        self.port = port
        await self._spawn(["tunnel", "--no-autoupdate", "run"])
        self.url = "https://" + hostname
        if not keep_watchdog:
            self._start_watchdog()
        return self.url

    # ---------- 统一入口与保活 ----------

    async def start(self, port: int, keep_watchdog: bool = False) -> str:
        """具名隧道优先。配了就用固定的，没配才用临时的。

        keep_watchdog=True 供守护进程自己调用：此时绝不能走 stop()，
        因为 stop() 会 cancel(self._watcher)——守护任务会把自己杀掉，
        表现就是「自动重连只生效一次，之后永不恢复」。
        """
        if not self.is_available():
            raise TunnelError(INSTALL_HINT)
        if self.running() and self.url:
            return self.url
        if keep_watchdog:
            self._kill_proc_only()
        else:
            await self.stop()
        if self.named_hostname():
            return await self.start_named(port, keep_watchdog=keep_watchdog)
        return await self.start_quick(port, keep_watchdog=keep_watchdog)

    def _kill_proc_only(self) -> None:
        """只清进程与状态，**不动守护任务**。守护重启专用。"""
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self.proc = None
        self.url = None
        self.mode = ""
        self._ready = None

    def _start_watchdog(self) -> None:
        """进程死了（休眠、网络抖动、连不上被踢）自动拉起并写入新地址。"""
        if self._watcher is not None and not self._watcher.done():
            return

        async def watch() -> None:
            while True:
                await asyncio.sleep(self.WATCH_INTERVAL)
                if not self.port:
                    continue
                healthy = self.ready_for() and self._url_still_alive()
                if healthy:
                    continue
                # 进程在跑但地址一直拿不到（例如刚启动连不上），也重开
                if self.running() and self.url and self._boot_elapsed() < self.BOOT_TIMEOUT:
                    continue
                try:
                    url = await self.start(self.port, keep_watchdog=True)
                    if self.on_url_changed is not None and url:
                        self.on_url_changed(url)
                except Exception:
                    pass

        self._watcher = asyncio.create_task(watch())

    def _boot_elapsed(self) -> float:
        try:
            return asyncio.get_running_loop().time() - getattr(self, "_booted_at", 0.0)
        except RuntimeError:
            return 0.0

    def _url_still_alive(self) -> bool:
        """拿到地址不算数，进程还得活着——否则地址是死的。"""
        return self.running()

    async def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
        await self.cancel_login()
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
        self.url = None
        self.mode = ""
        self._ready = None


def page_footer(config) -> str:
    """页脚只放用户看得懂的东西。

    之前用 metadata 里的 display_name，那是面板上的内部名，直接贴到公网页面
    等于告诉别人这是个什么插件。改用配置里的标题。
    """
    title = str(getattr(config, "title_help", "") or "").strip() or "指令图鉴"
    version = _read_metadata_value("version").lstrip("v") or "0.1.0"
    author = _read_metadata_value("author")
    return " · ".join(part for part in (title, "v" + version, author) if part)
