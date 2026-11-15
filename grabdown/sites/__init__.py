"""站点提取器：PornHub（flashvars → mediaDefinitions）、以及通用兜底。"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from ..extract import DirectExtractor, Extractor, Format, MediaInfo, register, scan_media_links
from ..http import GrabError, Session


def _json_array_after(text: str, anchor: str) -> str | None:
    """从 anchor 之后的第一个 '[' 起做括号配对，返回完整 JSON 数组文本。"""
    i = text.find(anchor)
    if i < 0:
        return None
    start = text.find("[", i)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return None


def _title_of(html: str) -> str:
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
    if m:
        return _unescape(m.group(1))
    m = re.search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</h1>', html, re.S | re.I)
    if m:
        return _unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    return _unescape(m.group(1)).strip() if m else "pornhub_video"


def _unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&quot;", '"').replace("&#039;", "'")
             .replace("&lt;", "<").replace("&gt;", ">").replace("\\/", "/")).strip()


class PornHubExtractor(Extractor):
    name = "pornhub"
    label = "PornHub"

    COOKIES = "platform=pc; age_verified=1; accessAgeDisclaimerPH=1; accessPH=1; cookiesBannerSeen=1"

    def match(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return "pornhub" in host or "phncdn" in host

    def extract(self, session: Session, url: str) -> MediaInfo:
        headers = {
            "Cookie": self.COOKIES,
            "Referer": "https://www.pornhub.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if urlsplit(url).path.lower().endswith(".m3u8"):
            return MediaInfo(site=self.name, title=url.rsplit("/", 1)[-1].split("?")[0] or "pornhub",
                             page_url=url,
                             formats=[Format(quality="auto", url=url, kind="hls")],
                             headers={"Referer": "https://www.pornhub.com/"})

        html, final = session.get_text(url, headers=headers)

        if "flashvars_" not in html:
            hint = "拿不到 flashvars：可能需要登录/年龄确认，或该区被墙"
            if re.search(r"(verify|age|登录|captcha)", html, re.I):
                hint += "（页面里有验证/年龄门）"
            raise GrabError(hint)

        arr = _json_array_after(html, '"mediaDefinitions"')
        if not arr:
            raise GrabError("页面里有 flashvars 但没找到 mediaDefinitions 数组")
        try:
            defs = json.loads(arr)
        except json.JSONDecodeError as e:
            raise GrabError(f"mediaDefinitions 不是合法 JSON: {e}") from e

        formats: list[Format] = []
        for d in defs:
            if not isinstance(d, dict):
                continue
            vu = d.get("videoUrl") or d.get("src")
            if not vu:
                continue
            fmt = (d.get("format") or "").lower()
            quality = str(d.get("quality") or d.get("height") or "auto")
            height = int(quality) if quality.isdigit() else (
                int(re.sub(r"\D", "", quality) or 0) or None)
            kind = "hls" if (fmt == "hls" or ".m3u8" in vu) else "mp4"
            formats.append(Format(quality=quality, url=vu, kind=kind, height=height,
                                  bandwidth=int(d["bandwidth"]) if str(d.get("bandwidth", "")).isdigit() else None))

        if not formats:
            raise GrabError("mediaDefinitions 里没有可用 videoUrl")

        # 去重（同 url 同质量）
        seen = set()
        uniq: list[Format] = []
        for f in formats:
            key = (f.kind, f.height, f.url)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(f)

        return MediaInfo(
            site=self.name,
            title=_title_of(html),
            page_url=final,
            formats=uniq,
            headers={"Referer": "https://www.pornhub.com/", "Cookie": self.COOKIES},
            note="PH 的 CDN 换了签名就重新抓一次页面即可",
        )


class GenericExtractor(Extractor):
    """兜底：任何 http(s) 页面里扫 m3u8/mp4。"""

    name = "generic"
    label = "通用扫描"

    def match(self, url: str) -> bool:
        return url.lower().startswith(("http://", "https://"))

    def extract(self, session: Session, url: str) -> MediaInfo:
        html, final = session.get_text(url)
        links = scan_media_links(html, final)
        if not links:
            raise GrabError("页面里没扫到 m3u8/mp4（可能需要登录，或媒体由 JS 动态请求）")
        fmts: list[Format] = []
        for u in links[:40]:
            kind = "hls" if ".m3u8" in u.lower() else "mp4"
            q = re.search(r"(\d{3,4})[pP]", u)
            fmts.append(Format(quality=(q.group(1) + "p") if q else "auto", url=u, kind=kind,
                               height=int(q.group(1)) if q else None))
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        title = _unescape(m.group(1)) if m else "page"
        return MediaInfo(site=self.name, title=title, page_url=final, formats=fmts,
                         headers={"Referer": final})


# 顺序就是匹配优先级：
#   direct  —— 已经是 .m3u8/.mp4，别再当网页扫
#   pornhub —— 原生解析，不依赖 yt-dlp
#   platform—— 认得出平台就交 yt-dlp（b站/YouTube/抖音…）
#   generic —— 兜底扫 HTML
register(DirectExtractor())
register(PornHubExtractor())
from . import platform as _platform  # noqa: E402,F401  (register 在模块末尾执行)
register(GenericExtractor())
