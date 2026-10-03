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
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import zipfile
from html import escape
from ipaddress import ip_address
from pathlib import Path
from string import Template
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web

# page.py 是独立层：不依赖 astrbot 的任何模块（测试/工具箱能单独 import）。
logger = logging.getLogger("help_dex.page")

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
    """Logo 缩到 128px 高再内嵌，避免一张 2MB 原图把整个页面拖慢。

    只缩放、**不抠图**：透明底图保持透明，不透明图原样上页。
    """
    path = style.logo_path()
    if not path:
        return None
    try:
        from PIL import Image

        image = Image.open(path).convert("RGBA")
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

/* ---------- 开场动画（参考 orbit_command 的幕布结构，浅色版）----------
   只碰 opacity/transform；模糊交给 .intro-ghost 一层的**静态** blur，
   动画只让它淡出——blur 是合成属性重灾区，逐帧重算就会掉帧。
   幕布底色和页面底色完全一致，退幕时没有「闪一下」。 */
:root{
  --intro-title-delay:160ms;
  --intro-title-in:1100ms;
  --intro-ghost-out:1200ms;
  --intro-sub-delay:820ms;
  --intro-sub-in:600ms;
  --intro-exit:700ms;
}
.intro{position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;
  overflow:hidden;background:linear-gradient(180deg,#F7F9FC 0%,#E9EFF7 100%);
  opacity:1;visibility:visible;pointer-events:auto;
  animation:intro-bail .1s linear 9s forwards}
@keyframes intro-bail{to{opacity:0;visibility:hidden;pointer-events:none}}
html.intro-done .intro{opacity:0;visibility:hidden;pointer-events:none;animation:none;transition:none}
html.intro-on.intro-out .intro{opacity:0;visibility:hidden;pointer-events:none;
  transition:opacity var(--intro-exit) ease,visibility 0s linear var(--intro-exit)}
.intro-title-wrap{position:absolute;left:0;right:0;top:calc(50% - 30px);display:grid;text-align:center}
.intro-ghost,.intro-title{grid-area:1/1}
.intro-title-wrap .intro-ghost,.intro-title-wrap .intro-title{
  font-size:clamp(26px,4.5vw,40px);font-weight:800;letter-spacing:.12em;margin-right:-.12em;
  color:var(--ink);overflow-wrap:anywhere}
.intro-ghost{color:var(--accent);filter:blur(12px);opacity:0;will-change:opacity,transform}
html.intro-on .intro-ghost{animation:intro-ghost-out var(--intro-ghost-out) var(--intro-title-delay) ease forwards}
@keyframes intro-ghost-out{from{opacity:.5;transform:scale(1.08)}to{opacity:0;transform:scale(1)}}
.intro-title{position:relative;opacity:0;will-change:transform,opacity;
  text-shadow:0 2px 18px rgba(47,128,216,.35)}
.intro-sub{position:absolute;left:0;right:0;top:calc(50% + 40px);text-align:center;
  padding:0 20px;font-size:14px;color:var(--ink-2);letter-spacing:.3em;margin-right:-.3em;
  overflow-wrap:anywhere;opacity:0}
.intro-title.breathe{animation:intro-breathe 5s ease-in-out infinite}
@keyframes intro-breathe{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.9;transform:scale(1.01)}}
.intro-stars{position:absolute;inset:0;pointer-events:none}
.intro-stars i{position:absolute;display:block;border-radius:50%;background:var(--accent);
  animation:intro-twinkle linear infinite}
html.intro-on .intro-stars{animation:intro-stars-in 1400ms ease-out both}
@keyframes intro-stars-in{from{opacity:0}to{opacity:1}}
@keyframes intro-twinkle{0%,100%{opacity:var(--lo)}50%{opacity:var(--hi)}}
"""

# 只做本地滚动动画：不发任何网络请求，也没有 $ 字面量（Template 转义要求）
SCRIPT = """
(function(){
  // ---------- 开场动画（最先起跑，不等其它绑定）----------
  // 页面是静态的，不需要等数据。节奏常量在 :root 的 --intro-* 里，
  // CSS 动画和 JS 插值读同一份，改一处两边同步。
  function introMs(name, fallback){
    var raw="";
    try{raw=getComputedStyle(document.documentElement).getPropertyValue(name).trim();}
    catch(e){}
    var value=parseFloat(raw);
    if(isFinite(value)&&value>0){
      return raw.indexOf("ms")>-1?value:value*1000;
    }
    return fallback;
  }
  function easeOutExpo(t){return t===1?1:1-Math.pow(2,-10*t)}
  function animate(el, from, to, duration, delay, fadeOnly){
    setTimeout(function(){
      var start=performance.now();
      function frame(now){
        var t=Math.min((now-start)/duration,1);
        t=easeOutExpo(t);
        el.style.opacity=(from.o+(to.o-from.o)*t).toFixed(4);
        if(!fadeOnly){
          el.style.transform='translateY('+(from.y+(to.y-from.y)*t).toFixed(2)+'px) '
            +'scale('+(from.s+(to.s-from.s)*t).toFixed(4)+')';
        }
        if(t<1){requestAnimationFrame(frame)}
        else{
          el.style.opacity='1';
          if(!fadeOnly){el.style.transform=''}
        }
      }
      requestAnimationFrame(frame);
    },delay);
  }
  function finishIntro(stage){
    if(stage.dataset.done2==='1'){return}
    stage.dataset.done2='1';
    var root=document.documentElement;
    requestAnimationFrame(function(){requestAnimationFrame(function(){
      root.classList.add('intro-out');
    })});
    setTimeout(function(){
      // 留一个永久隐藏的 class，别把 intro-on/intro-out 摘干净：
      // .intro 默认样式是「不透明可见」（第一帧不闪），摘干净幕布会回来。
      root.classList.add('intro-done');
      root.classList.remove('intro-on');
      root.classList.remove('intro-out');
      stage.dataset.done2='';
    },introMs('--intro-exit',700));
  }
  function makeStars(host, count){
    if(!host){return}
    var seed=20261003;
    function rand(){seed=(seed*1103515245+12345)&0x7fffffff;return seed/0x7fffffff}
    var parts=[];
    for(var i=0;i<count;i++){
      var size=(1.4+rand()*2.2).toFixed(2);
      var left=(rand()*100).toFixed(2);
      var top=(rand()*100).toFixed(2);
      var dur=(2.4+rand()*4.2).toFixed(2);
      var delay=(-rand()*5).toFixed(2);
      var lo=(0.08+rand()*0.14).toFixed(2);
      var hi=(0.35+rand()*0.4).toFixed(2);
      parts.push('<i style="width:'+size+'px;height:'+size+'px;left:'+left+'%;top:'+top+'%;'
        +'animation-duration:'+dur+'s;animation-delay:'+delay+'s;--lo:'+lo+';--hi:'+hi+'"></i>');
    }
    host.innerHTML=parts.join('');
  }
  function playIntro(){
    var stage=document.getElementById('intro');
    var title=document.getElementById('intro-title');
    var sub=document.getElementById('intro-sub');
    if(!stage||!title||!sub){return}
    if(stage.dataset.done==='1'){return}
    stage.dataset.done='1';
    var root=document.documentElement;
    var reduced=false;
    try{reduced=matchMedia('(prefers-reduced-motion: reduce)').matches}catch(e){}
    // 开了「减少动效」就立刻撤幕，不是什么都不做——幕布默认是盖住的不透明层。
    if(reduced){root.classList.add('intro-done');return}
    makeStars(document.getElementById('intro-stars'),22);
    root.classList.add('intro-on');
    var step=introMs('--intro-title-delay',160);
    var main=introMs('--intro-title-in',1100);
    var subDelay=introMs('--intro-sub-delay',820);
    var subIn=introMs('--intro-sub-in',600);
    var hold=450;
    // 兜底：rAF 被挂起或中间抛错，到点必退幕，不能把页面永久挡住
    var bail=setTimeout(function(){finishIntro(stage)},9000);
    var skip=function(){
      clearTimeout(bail);
      finishIntro(stage);
      document.removeEventListener('keydown',skip);
      stage.removeEventListener('click',skip);
    };
    document.addEventListener('keydown',skip);
    stage.addEventListener('click',skip);
    // 两条动画同时起跑，各自算绝对延迟——不要写成「等标题完再开始副标题」。
    // 标题只做位移+缩放，模糊由 .intro-ghost 静态层承担，两帧一淡一显。
    animate(title,{o:0,s:1.08,y:10},{o:1,s:1,y:0},main,step,false);
    animate(sub,{o:0,s:1,y:0},{o:1,s:1,y:0},subIn,subDelay,true);
    setTimeout(function(){title.classList.add('breathe')},step+main);
    setTimeout(function(){
      clearTimeout(bail);
      document.removeEventListener('keydown',skip);
      stage.removeEventListener('click',skip);
      // 到位后停一拍再退幕：不给这一拍，最终状态看不清就被抽走了
      setTimeout(function(){finishIntro(stage)},hold);
    },Math.max(step+main,subDelay+subIn));
  }
  playIntro();

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
${intro}<div id="bar"></div>
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


def _intro_block(title: str, subtitle: str) -> str:
    """开场幕布。两层标题叠在一个网格里：底下 .intro-ghost 静态模糊，
    上面 .intro-title 清晰——一淡一显，看起来就是「从模糊里出来」。"""
    return (
        '<div class="intro" id="intro" aria-hidden="true">'
        '<div class="intro-stars" id="intro-stars"></div>'
        '<div class="intro-title-wrap">'
        f'<div class="intro-ghost">{_esc(title)}</div>'
        f'<div class="intro-title" id="intro-title">{_esc(title)}</div>'
        "</div>"
        f'<div class="intro-sub" id="intro-sub">{_esc(subtitle)}</div>'
        "</div>"
    )


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
        intro=_intro_block(title, subtitle),
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
    """隧道没开起来，或者环境里根本没有 cpolar。"""


# 自动安装都失败时的于工落底（正常情况下用户永远看不到这段）
INSTALL_HINT = (
    "cpolar 自动安装没成功。可以手动装（命令会自动识别架构）：\n"
    "  cd /tmp && curl -L -o cpolar.zip \\\n"
    "  \"https://www.cpolar.com/static/downloads/cpolar-stable-linux-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').zip\"\n"
    "  && python3 -m zipfile -e cpolar.zip /usr/local/bin/ && chmod +x /usr/local/bin/cpolar\n"
    "装完再发一次同样的指令。"
)

# cpolar 客户端的配置目录：authtoken 就存在这里，认证只认这一个文件。
CPOLAR_DIR = Path.home() / ".cpolar"
CPOLAR_YML = CPOLAR_DIR / "cpolar.yml"
# 自动安装的落地位置（与配置目录在一起，重装容器也保留）
CPOLAR_BIN_DIR = CPOLAR_DIR / "bin"
CPOLAR_BIN = CPOLAR_BIN_DIR / "cpolar"

CPOLAR_DOWNLOAD_BASE = "https://www.cpolar.com/static/downloads/cpolar-stable-linux-{}.zip"
# 真二进制解压后有 15MB；小于这个数说明下载被截断了（官网偶发）
MIN_CPOLAR_BYTES = 5 * 1024 * 1024

_ARCH_MAP = {
    "x86_64": "amd64", "amd64": "amd64",
    "aarch64": "arm64", "arm64": "arm64",
    "armv7l": "arm", "armv6l": "arm", "armv7": "arm",
    "i386": "386", "i686": "386", "x86": "386",
    "mips": "mips", "mipsel": "mipsle",
}

# 本地 inspect 页面用的端口（日志里拿不到地址时的备用来源）。
# 挑几个不连号的候选；都占不到就放弃备用源、只靠日志解析。
INSPECT_PORT_CANDIDATES = (41979, 41989, 41999)


def pick_inspect_port() -> int:
    for candidate in INSPECT_PORT_CANDIDATES:
        if port_is_free(candidate):
            return candidate
    return 0


def cpolar_arch() -> str:
    """把平台架构名映射到 cpolar 下载页用的名字。认不出来返回空串。"""
    return _ARCH_MAP.get(platform.machine().lower(), "")


def extract_cpolar_binary(data: bytes) -> bytes:
    """从下载的 zip 里取出 cpolar 可执行文件；不完整就抛 ValueError。"""
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in archive.namelist()
             if n == "cpolar" or n.endswith("/cpolar")]
    if not names:
        raise ValueError("压缩包里没有 cpolar 文件")
    binary = archive.read(names[0])
    if len(binary) < MIN_CPOLAR_BYTES:
        raise ValueError("解出来的 cpolar 只有 {} 字节，不完整".format(len(binary)))
    return binary


def is_cpolar_url(url: object) -> bool:
    """这个地址是不是 cpolar 隧道域名。

    「配置里存着地址 + 隧道进程没跑」这类判断只该对 cpolar 域名生效：
    只有它会随进程生死、随免费版轮换而失效；用户自己配的反代地址不该被这样对待。
    """
    text = str(url or "").strip().lower()
    if not text:
        return False
    host = text.split("://", 1)[-1].split("/", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    return (
        host.endswith(".cpolar.cn")
        or host.endswith(".cpolar.io")
        or host.endswith(".cpolar.top")
    )


class TunnelManager:
    """把欢迎页开到公网（cpolar 极点云）。

    为什么从 Cloudflare 换过来：那套要走浏览器授权链接 + 域名托管 + CNAME，
    整条链太长。cpolar 只要注册一次、把后台的 authtoken 粘进来，就没有别的了。
    免费版限制（官网核实）：1Mbps 带宽；随机地址会周期性重置。
    地址重置没关系：守护循环盯着进程和地址，变了就回写配置、旧链自动补发。

    地址来源两处（实测）：
      1. 子进程日志里的隧道地址（主）
      2. 本地 inspect 页面 /http/in 里内嵌的 PublicUrl（备，日志没抓到兜底）
    """

    URL_RE = re.compile(
        r"https://[A-Za-z0-9][A-Za-z0-9.-]*\.cpolar\.(?:cn|io|top)\b"
    )
    AUTH_FAIL_MARKERS = ("authtoken auth failed", "authentication failed")
    REGION = "cn_top"           # 中国节点；要换地区改这一个值
    BOOT_TIMEOUT = 45.0
    WATCH_INTERVAL = 20.0
    INSPECT_WAIT = 8.0          # 日志里迟迟没有地址时，多久后开始查本地页面

    def __init__(self, on_url_changed: Optional[Callable[[str], None]] = None) -> None:
        self.mode = ""
        self.port = 0
        self.proc = None
        self.url = None
        self.on_url_changed = on_url_changed
        self._reader = None
        self._watcher = None
        self._booted_at = 0.0
        self._last_error = ""
        self._inspect_port = 0

    # ---------- 环境 ----------

    @staticmethod
    def find_cpolar() -> Optional[str]:
        """找到可用的 cpolar 可执行文件。先在 PATH 里找，再找自动装的。"""
        found = shutil.which("cpolar")
        if found:
            return found
        if CPOLAR_BIN.is_file() and os.access(str(CPOLAR_BIN), os.X_OK):
            return str(CPOLAR_BIN)
        return None

    @staticmethod
    def is_available() -> bool:
        return TunnelManager.find_cpolar() is not None

    @staticmethod
    async def ensure_binary() -> str:
        """确保有 cpolar 可用：没有就**自己下载安装**，不让用户去装。

        官网下载偶发截断（实测过 3.9MB / 7.4MB 两种大小），所以拿到手
        先验完整性：解出二进制、字节数过关，才落盘。落盘后回读比对，
        对不上就当这次失败（下次再试）。
        """
        found = TunnelManager.find_cpolar()
        if found:
            return found
        arch = cpolar_arch()
        if not arch:
            raise TunnelError(
                "认不出这台机器的 CPU 架构（{}），没法自动下载 cpolar。"
                "{} ".format(platform.machine(), INSTALL_HINT)
            )
        url = CPOLAR_DOWNLOAD_BASE.format(arch)
        last_error = ""
        # 官网下载会偶发截断（实测），重试次数给足；每次之间歇一下，
        # 连续三次失败往往是同一波网络抖动，隔开重试才有意义。
        for attempt in range(5):
            if attempt:
                await asyncio.sleep(2.0 * attempt)
            try:
                timeout = aiohttp.ClientTimeout(total=180)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            last_error = "下载服务器返回 {}".format(resp.status)
                            continue
                        data = await resp.read()
                binary = extract_cpolar_binary(data)
                CPOLAR_BIN_DIR.mkdir(parents=True, exist_ok=True)
                # 先写临时文件再改名，避免半截文件被当成装好了
                tmp_path = CPOLAR_BIN_DIR / "cpolar.part"
                tmp_path.write_bytes(binary)
                tmp_path.chmod(0o755)
                if tmp_path.read_bytes() != binary:
                    last_error = "落盘回读不一致"
                    continue
                tmp_path.replace(CPOLAR_BIN)
                logger.info(
                    "[help_dex] cpolar 已自动安装：{}（{}）".format(CPOLAR_BIN, arch)
                )
                return str(CPOLAR_BIN)
            except Exception as exc:
                last_error = str(exc)
        raise TunnelError(
            "cpolar 自动下载失败（试了 5 次）：{}。\n".format(last_error or "原因不明")
            + INSTALL_HINT
        )

    @staticmethod
    def token_status() -> Tuple[Optional[str], str]:
        """读 cpolar 配置里的 authtoken。返回 (token, 问题描述)。"""
        try:
            text = CPOLAR_YML.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, ""
        except Exception as exc:
            return None, "{} 读不了：{}".format(CPOLAR_YML, exc)
        found = re.search(r"^authtoken:\s*(\S+)\s*$", text, re.M)
        if not found:
            return None, ""
        token = found.group(1).strip().strip('"').strip("'")
        return (token or None), ""

    @staticmethod
    def has_token() -> bool:
        return TunnelManager.token_status()[0] is not None

    @staticmethod
    def set_token(token: str) -> None:
        """把 authtoken 写进 cpolar 的配置文件——这就是全部的「授权」。"""
        token = str(token or "").strip()
        if not token or any(ch.isspace() for ch in token):
            raise TunnelError(
                "用法：/图鉴隧道 令牌 <token>\n"
                "（token 在 cpolar 后台的「验证」页复制，形如一串字母数字）"
            )
        CPOLAR_DIR.mkdir(parents=True, exist_ok=True)
        CPOLAR_YML.write_text("authtoken: {}\n".format(token), encoding="utf-8")

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def ready_for(self, seconds: float = 0.0) -> bool:
        """地址是否已经拿到。进程在跑 ≠ 链接可用。"""
        return bool(self.url) and self.running()

    # ---------- 进程 ----------

    async def _spawn(self) -> None:
        binary = await self.ensure_binary()
        self._inspect_port = pick_inspect_port()
        args = ["http", "-region=" + self.REGION, "-log=stdout", "-log-level=info"]
        if self._inspect_port:
            args.append("-inspect-addr=127.0.0.1:{}".format(self._inspect_port))
        else:
            args.append("-inspect-addr=false")
        args.append(str(int(self.port)))
        self.proc = await asyncio.create_subprocess_exec(
            binary, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._last_error = ""
        self.url = None
        self._reader = asyncio.create_task(self._drain())
        self._booted_at = asyncio.get_running_loop().time()

    async def _drain(self) -> None:
        """cpolar 的日志走 stdout（-log=stdout）。必须一直读干净，
        否则管道写满会把 cpolar 自己卡死；顺便抓地址与「token 被拒」。"""
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace")
                if not self._last_error:
                    low = text.lower()
                    for marker in self.AUTH_FAIL_MARKERS:
                        if marker in low:
                            self._last_error = (
                                "cpolar 说这个 token 不认。去 https://dashboard.cpolar.com "
                                "的「验证」页重新复制，再发 /图鉴隧道 令牌 <token>"
                            )
                            break
                if not self.url:
                    found = self._extract_url(text)
                    if found:
                        self.url = found
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    @classmethod
    def _extract_url(cls, text: str) -> Optional[str]:
        for match in cls.URL_RE.finditer(str(text or "")):
            candidate = match.group(0).rstrip("，。；;,.'\"")
            host = candidate.split("://", 1)[1].split("/", 1)[0].lower()
            # cpolar 自己的站点不是隧道地址，别拿它们当公网地址
            if host.startswith(("www.", "dashboard.", "api.", "scpolard.")):
                continue
            return candidate
        return None

    async def _poll_inspect(self) -> Optional[str]:
        """从本地 inspect 页拿隧道地址（日志没抓到时的备用来源）。"""
        if not self._inspect_port:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=3.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "http://127.0.0.1:{}/http/in".format(self._inspect_port)
                ) as resp:
                    text = await resp.text()
        except Exception:
            return None
        # 页面里是转义过的 JSON 字符串，先把反斜杠转义还原再找 PublicUrl
        normalized = text.replace('\\"', '"')
        found = re.search(r'"PublicUrl"\s*:\s*"(https://[^"]+)"', normalized)
        if not found:
            return None
        return self._extract_url(found.group(1))

    async def _wait_ready(self, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        next_inspect = start + self.INSPECT_WAIT
        while True:
            if self._last_error:
                # 先把原因抓在手里：stop() 会清掉 _last_error，
                # 先 stop 再读的话抛出去的就是空消息。
                message = self._last_error
                await self.stop()
                raise TunnelError(message)
            if self.url:
                return
            now = loop.time()
            if now - start >= timeout:
                await self.stop()
                raise TunnelError(
                    "等了 {} 秒还没拿到地址。看后台日志里 cpolar 的输出。".format(
                        int(timeout)
                    )
                )
            if self._inspect_port and now >= next_inspect:
                found = await self._poll_inspect()
                if found:
                    self.url = found
                    return
                next_inspect = now + 2.0
            if self.proc is not None and self.proc.returncode is not None:
                # 进程已退出：把 reader 读剩的最后几行处理完再判
                await asyncio.sleep(0.3)
                if self._last_error or self.url:
                    continue
                raise TunnelError("cpolar 进程退出了，没拿到地址。看后台日志。")
            await asyncio.sleep(0.4)

    # ---------- 启停与保活 ----------

    async def start(self, port: int, keep_watchdog: bool = False) -> str:
        """拉起 cpolar 隧道。没有 token 就直接报错给用户。

        keep_watchdog=True 供守护进程自己调用：此时绝不能走 stop()，
        因为 stop() 会 cancel(self._watcher)——守护任务会把自己杀掉，
        表现就是「自动重连只生效一次，之后永不恢复」。
        """
        if not TunnelManager.is_available():
            # is_available 只看「本地有没有」；没有就当场下载安装。
            # 用户的任务不是去装软件，所以这一步不往外抛安装教程，
            # 而是真的把事办了——失败才会带着原因和手工命令抛错。
            await self.ensure_binary()
        token, problem = self.token_status()
        if problem:
            raise TunnelError(problem)
        if not token:
            raise TunnelError(
                "还没配 cpolar token（免费，注册一次就行）：\n"
                "1. 到 https://www.cpolar.com 注册\n"
                "2. 后台「验证」页复制 authtoken\n"
                "3. 发 /图鉴隧道 令牌 <粘进来>"
            )
        if self.running() and self.url:
            return self.url
        if keep_watchdog:
            self._kill_proc_only()
        else:
            await self.stop()
        self.mode = "cpolar"
        self.port = int(port)
        await self._spawn()
        await self._wait_ready(self.BOOT_TIMEOUT)
        if not keep_watchdog:
            self._start_watchdog()
        return self.url or ""

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
        self._last_error = ""

    def _start_watchdog(self) -> None:
        """进程死了自动拉起；地址变了（免费版会轮换）自动回写。"""
        if self._watcher is not None and not self._watcher.done():
            return

        async def watch() -> None:
            while True:
                await asyncio.sleep(self.WATCH_INTERVAL)
                if not self.port:
                    continue
                try:
                    if not self.running():
                        url = await self.start(self.port, keep_watchdog=True)
                        if url and self.on_url_changed is not None:
                            self.on_url_changed(url)
                        continue
                    if self._inspect_port:
                        fresh = await self._poll_inspect()
                        if fresh and fresh != self.url:
                            self.url = fresh
                            if self.on_url_changed is not None:
                                self.on_url_changed(fresh)
                except Exception:
                    pass

        self._watcher = asyncio.create_task(watch())

    def _boot_elapsed(self) -> float:
        try:
            return asyncio.get_running_loop().time() - getattr(self, "_booted_at", 0.0)
        except RuntimeError:
            return 0.0

    async def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
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
        self._last_error = ""


def page_footer(config) -> str:
    """页脚只放用户看得懂的东西。

    之前用 metadata 里的 display_name，那是面板上的内部名，直接贴到公网页面
    等于告诉别人这是个什么插件。改用配置里的标题。
    """
    title = str(getattr(config, "title_help", "") or "").strip() or "指令图鉴"
    version = _read_metadata_value("version").lstrip("v") or "0.1.0"
    author = _read_metadata_value("author")
    return " · ".join(part for part in (title, "v" + version, author) if part)
