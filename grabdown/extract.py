"""提取层：网页 → 媒体地址列表（插件式）。"""

from __future__ import annotations

import re
import urllib.parse as up
from dataclasses import dataclass, field
from typing import Callable

from .http import GrabError, Session


@dataclass
class Format:
    quality: str
    url: str
    kind: str = "hls"          # hls | mp4
    height: int | None = None
    bandwidth: int | None = None
    note: str = ""

    @property
    def sort_key(self) -> tuple:
        order = 0 if self.kind == "hls" else 1
        return (order, -(self.height or 0), -(self.bandwidth or 0))


@dataclass
class MediaInfo:
    site: str
    title: str
    page_url: str
    formats: list[Format] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    note: str = ""

    def pick(self, quality: str) -> Format:
        q = (quality or "best").lower()
        if not self.formats:
            raise GrabError("没有解析到任何媒体地址")
        if q in ("best", "max"):
            return sorted(self.formats, key=lambda f: f.sort_key)[0]
        if q in ("worst", "min"):
            return sorted(self.formats, key=lambda f: f.sort_key)[-1]
        want = int(re.sub(r"\D", "", q) or 0)
        sized = [f for f in self.formats if f.height]
        if sized:
            exact = [f for f in sized if f.height == want]
            if exact:
                return sorted(exact, key=lambda f: f.sort_key)[0]
            lower = [f for f in sized if f.height <= want]
            if lower:  # 就近向下取最大
                return max(lower, key=lambda f: (f.height, -f.sort_key[0]))
            return min(sized, key=lambda f: f.height)  # 请求的比最高还高 → 给最高
        return sorted(self.formats, key=lambda f: f.sort_key)[0]


class Extractor:
    name = "base"
    label = "基类"

    def match(self, url: str) -> bool:
        raise NotImplementedError

    def extract(self, session: Session, url: str) -> MediaInfo:
        raise NotImplementedError


REGISTRY: list[Extractor] = []


def register(ext: Extractor) -> Extractor:
    REGISTRY.append(ext)
    return ext


def resolve(session: Session, url: str) -> tuple[Extractor, MediaInfo]:
    for ext in REGISTRY:
        try:
            if ext.match(url):
                return ext, ext.extract(session, url)
        except GrabError:
            raise
        except Exception as e:  # noqa: BLE001
            raise GrabError(f"{ext.name} 解析失败: {e}") from e
    raise GrabError(f"没有匹配的提取器: {url}")


# ---------------------------------------------------------------- 直链
class DirectExtractor(Extractor):
    """URL 本身就是 m3u8 / mp4 / 常见媒体文件。"""

    name = "direct"
    label = "直链"
    MEDIA_EXT = (".m3u8", ".mp4", ".m4a", ".mp3", ".flv", ".webm", ".mkv", ".ts", ".aac", ".wav")

    def match(self, url: str) -> bool:
        path = up.urlsplit(url).path.lower()
        return any(path.endswith(e) for e in self.MEDIA_EXT)

    def extract(self, session: Session, url: str) -> MediaInfo:
        path = up.urlsplit(url).path
        kind = "hls" if path.lower().endswith(".m3u8") else "mp4"
        name = path.rsplit("/", 1)[-1] or "media"
        return MediaInfo(site="direct", title=name, page_url=url,
                         formats=[Format(quality="source", url=url, kind=kind)],
                         note="链接直接是媒体文件")


def scan_media_links(html: str, base: str) -> list[str]:
    """从 HTML/JS 里捞 m3u8 与 mp4（宽松匹配，交给上层去重排序）。"""
    pat = re.compile(r"""https?://[^\s"'<>\\]+?\.(?:m3u8|mp4)(?:\?[^\s"'<>\\]+)?""", re.I)
    found = [m.group(0) for m in pat.finditer(html)]
    # 相对路径的 m3u8
    rel = re.compile(r"""["'](/[^\s"'<>]+?\.(?:m3u8|mp4)(?:\?[^\s"'<>]+)?)["']""", re.I)
    found += [up.urljoin(base, m.group(1)) for m in rel.finditer(html)]
    out: list[str] = []
    for u in found:
        u = u.replace("\\/", "/")
        if u not in out:
            out.append(u)
    return out
