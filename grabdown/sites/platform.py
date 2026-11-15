"""通用「主流平台」提取器：认平台 → 交给 yt-dlp 取格式。

只有 platforms 表里 engine == 'ytdlp' 的站点会走这里；
PornHub 有原生解析器（不依赖 yt-dlp），直链走 direct，互不抢。
"""

from __future__ import annotations

from .. import platforms
from ..extract import Extractor, Format, MediaInfo, register
from ..http import GrabError, Session
from .. import ytdlp_backend as ytdlp


class PlatformExtractor(Extractor):
    name = "platform"
    label = "主流平台（yt-dlp）"

    def _platform(self, url: str):
        p = platforms.detect(url)
        if not p or p.engine != "ytdlp":
            return None
        if platforms.is_direct_media(url):
            return None          # .m3u8/.mp4 直链走 direct
        return p

    def match(self, url: str) -> bool:
        p = self._platform(url)
        return bool(p) and ytdlp.available()

    def extract(self, session: Session, url: str) -> MediaInfo:
        p = self._platform(url)
        if not p:
            raise GrabError("不是受支持的平台")
        if not ytdlp.available():
            raise GrabError("需要 yt-dlp：python -m pip install -U yt-dlp")

        cookie = session.base_headers.get("Cookie") if session else None
        info = ytdlp.probe(url, cookie=cookie)
        if not info.get("formats"):
            raise GrabError(f"{p.name} 没解析出任何格式（可能要登录，或该视频需大会员）")

        # 按高度聚合：同高度保留码率最高的那档；纯音频单独列一条
        by_height: dict[int, tuple[Any, int]] = {}
        audio_only = None
        for f in info["formats"]:
            size = f.filesize or 0
            if f.height:
                cur = by_height.get(f.height)
                if cur is None or size > cur[1]:
                    by_height[f.height] = (f, size)
            elif not audio_only or (size or 0) > (audio_only[1] or 0):
                audio_only = (f, size)

        formats: list[Format] = []
        for height in sorted(by_height, reverse=True):
            f, size = by_height[height]
            formats.append(Format(
                quality=f"{height}p",
                url=url,                      # yt-dlp 拿的是页面地址，不是直链
                kind="ytdlp",
                height=height,
                note=f"{f.ext}{' · ' + f.note if f.note else ''}",
            ))
        if audio_only and not formats:
            formats.append(Format(quality="audio", url=url, kind="ytdlp", note="仅音频"))

        if not formats:
            raise GrabError(f"{p.name} 没有可下载的格式")

        headers = {}
        if p.referer:
            headers["Referer"] = p.referer
        return MediaInfo(
            site=p.key,
            title=info.get("title") or p.name,
            page_url=info.get("webpage_url") or url,
            formats=formats,
            headers=headers,
            note=p.note,
        )


register(PlatformExtractor())
