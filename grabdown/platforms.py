"""平台识别：先认出「这是哪个站」，再决定用哪种引擎去下。

三种引擎：
  native  —— 我们自己写的解析器（pornhub），不依赖外部工具
  ytdlp   —— 交给 yt-dlp（B 站、YouTube、抖音…1800+ 站点，签名/DASH 合并它管）
  direct  —— URL 本身就是 m3u8/mp4，直接下

识别只做「域名 + 路径特征」，不发请求，所以可以在扩展、CLI、agent 里随便调。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    hosts: tuple[str, ...]           # 后缀匹配
    engine: str = "ytdlp"            # native | ytdlp | direct
    referer: str = ""
    note: str = ""
    emoji: str = "🎬"

    def matches(self, host: str) -> bool:
        h = (host or "").lower()
        return any(h == x or h.endswith("." + x) for x in self.hosts)


def _p(key, name, hosts, engine="ytdlp", referer="", note="", emoji="🎬"):
    return Platform(key=key, name=name, hosts=tuple(hosts), engine=engine,
                    referer=referer, note=note, emoji=emoji)


PLATFORMS: tuple[Platform, ...] = (
    # ---- 国内 ----
    _p("bilibili", "哔哩哔哩", ("bilibili.com", "b23.tv", "bilivideo.com", "bilivideo.cn"),
       referer="https://www.bilibili.com", emoji="📺",
       note="DASH 音视频分离，需要 ffmpeg 合并；大会员清晰度要登录 cookie"),
    _p("douyin", "抖音", ("douyin.com", "iesdouyin.com"), emoji="🎵"),
    _p("kuaishou", "快手", ("kuaishou.com", "chenzhongtech.com"), emoji="⚡"),
    _p("ixigua", "西瓜视频", ("ixigua.com",), emoji="🍉"),
    _p("weibo", "微博", ("weibo.com", "weibo.cn"), emoji="🌐"),
    _p("xiaohongshu", "小红书", ("xiaohongshu.com", "xhslink.com"), emoji="📕"),
    _p("netease", "网易云音乐", ("music.163.com",), emoji="🎧"),

    # ---- 海外视频 ----
    _p("youtube", "YouTube", ("youtube.com", "youtu.be", "youtube-nocookie.com"), emoji="▶️",
       note="签名会变，yt-dlp 需保持更新"),
    _p("tiktok", "TikTok", ("tiktok.com",), emoji="🎵"),
    _p("twitter", "X / Twitter", ("twitter.com", "x.com", "t.co"), emoji="🐦"),
    _p("instagram", "Instagram", ("instagram.com",), emoji="📷"),
    _p("facebook", "Facebook", ("facebook.com", "fb.watch"), emoji="📘"),
    _p("vimeo", "Vimeo", ("vimeo.com",), emoji="🎥"),
    _p("dailymotion", "Dailymotion", ("dailymotion.com", "dai.ly"), emoji="📼"),
    _p("twitch", "Twitch", ("twitch.tv",), emoji="🟣"),
    _p("soundcloud", "SoundCloud", ("soundcloud.com",), emoji="🔊"),

    # ---- 成人（PH 有原生解析器，其余走 yt-dlp）----
    _p("pornhub", "PornHub", ("pornhub.com", "phncdn.com"), engine="native",
       referer="https://www.pornhub.com/", emoji="🔞"),
    _p("xvideos", "XVideos", ("xvideos.com",), emoji="🔞"),
    _p("xnxx", "XNXX", ("xnxx.com",), emoji="🔞"),
    _p("missav", "MissAV", ("missav.ws", "missav.com"), emoji="🔞"),

    # ---- 只做识别、明确不支持的（版权 / DRM）----
    _p("netflix", "Netflix", ("netflix.com",), engine="unsupported", emoji="🚫",
       note="DRM（Widevine），不做"),
    _p("disney", "Disney+", ("disneyplus.com",), engine="unsupported", emoji="🚫",
       note="DRM，不做"),
    _p("spotify", "Spotify", ("spotify.com",), engine="unsupported", emoji="🚫",
       note="DRM，不做"),
)

BY_KEY = {p.key: p for p in PLATFORMS}

# 直链特征（优先级最高：已经是媒体文件就不用认平台）
_MEDIA_RE = re.compile(r"\.(m3u8|mpd|mp4|m4a|mp3|flac|webm|mkv|ts|mov)(\?|#|$)", re.I)


def detect(url: str) -> Platform | None:
    """返回平台；认不出来返回 None（调用方再决定 generic 兜底）。"""
    if not url:
        return None
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return None
    for p in PLATFORMS:
        if p.matches(host):
            return p
    return None


def is_direct_media(url: str) -> bool:
    try:
        return bool(_MEDIA_RE.search(urlsplit(url).path or ""))
    except ValueError:
        return False


def label(url: str) -> str:
    """给 UI 用的短标签：平台名，认不出就给域名。"""
    p = detect(url)
    if p:
        return p.name
    try:
        return urlsplit(url).hostname or "未知"
    except ValueError:
        return "未知"


def describe() -> list[dict[str, str]]:
    return [
        {"key": p.key, "name": p.name, "engine": p.engine, "emoji": p.emoji,
         "hosts": ", ".join(p.hosts), "note": p.note}
        for p in PLATFORMS
    ]
