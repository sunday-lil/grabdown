"""yt-dlp 后端：给「主流平台」兜底。

为什么不自己写 B 站/YouTube 的解析：
  YouTube 要解 player JS 签名、B 站要 wbi 签名 + DASH 音视频分离、
  抖音要 a_bogus… 这些每周都在变。自己维护 20 个站 = 20 个会烂的解析器。
  yt-dlp 已经养了 1800+ 站点，我们只负责：认平台 → 交给它拿格式/下载 → 用它的 hook 报进度。

我们的分段引擎仍然负责「直链 / HLS」；这里是互补关系，不是替代。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

BACKEND = "yt-dlp"


def _mod():
    try:
        import yt_dlp  # noqa: F401

        return yt_dlp
    except Exception:
        return None


def available() -> bool:
    return _mod() is not None


def version() -> str:
    m = _mod()
    if not m:
        return ""
    try:
        return m.version.__version__
    except Exception:
        return "unknown"


def selector(quality: str) -> str:
    """把 best / 1080 / 720 变成 yt-dlp 的 format selector。"""
    q = (quality or "best").lower()
    if q in ("best", "max"):
        return "bestvideo*+bestaudio/best"
    if q in ("worst", "min"):
        return "worstvideo*+worstaudio/worst"
    digits = "".join(ch for ch in q if ch.isdigit())
    if not digits:
        return "bestvideo*+bestaudio/best"
    return f"bestvideo*[height<={digits}]+bestaudio/best[height<={digits}]/best"


@dataclass
class YtFormat:
    format_id: str
    height: int | None
    ext: str
    filesize: int | None
    has_audio: bool
    note: str = ""


def _pick_formats(info: dict[str, Any]) -> list[YtFormat]:
    out: list[YtFormat] = []
    for f in info.get("formats") or []:
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue
        height = f.get("height")
        has_audio = f.get("acodec") not in (None, "none")
        out.append(YtFormat(
            format_id=str(f.get("format_id") or ""),
            height=height,
            ext=str(f.get("ext") or ""),
            filesize=f.get("filesize") or f.get("filesize_approx"),
            has_audio=has_audio,
            note=str(f.get("format_note") or ""),
        ))
    return out


def probe(url: str, *, cookies_from_browser: str | None = None,
          cookie: str | None = None, timeout: int = 30) -> dict[str, Any]:
    """只取元数据，不下载。返回 {'title','duration','thumbnail','formats':[YtFormat]}"""
    m = _mod()
    if not m:
        raise RuntimeError("未安装 yt-dlp：python -m pip install -U yt-dlp")

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "socket_timeout": timeout,
        "noplaylist": True,
        "extract_flat": False,
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if cookie:
        opts.setdefault("http_headers", {})["Cookie"] = cookie

    with m.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if entries:
            info = entries[0]
    return {
        "title": info.get("title") or "video",
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or "",
        "thumbnail": info.get("thumbnail") or "",
        "formats": _pick_formats(info),
        "webpage_url": info.get("webpage_url") or url,
    }


def download(url: str, out_dir: str | Path, *, quality: str = "best",
             hook: Callable[[int, int], None] | None = None,
             cookie: str | None = None, referer: str | None = None,
             cookies_from_browser: str | None = None,
             remux: bool = True) -> Path:
    """交给 yt-dlp 下（DASH 音视频自动合并，需要 ffmpeg）。返回最终文件路径。"""
    m = _mod()
    if not m:
        raise RuntimeError("未安装 yt-dlp：python -m pip install -U yt-dlp")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _progress(d: dict[str, Any]) -> None:
        if not hook:
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        try:
            hook(int(done), int(total))
        except Exception:
            pass

    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": selector(quality),
        "outtmpl": str(out_dir / "%(title).120B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "merge_output_format": "mp4" if remux else None,
        "progress_hooks": [_progress],
        "retries": 3,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "nocheckcertificate": False,
    }
    if headers:
        opts["http_headers"] = headers
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if not remux:
        opts.pop("merge_output_format", None)

    with m.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if entries:
            info = entries[0]

    # yt-dlp 会把最终落盘路径放在 requested_downloads[].filepath
    path = None
    for rd in info.get("requested_downloads") or []:
        path = rd.get("filepath") or rd.get("_filename") or path
    if not path:
        path = info.get("filepath") or info.get("_filename")
    if not path:
        guess = ydl.prepare_filename(info)
        path = guess
    return Path(path)


def diagnose() -> dict[str, Any]:
    return {
        "backend": BACKEND,
        "available": available(),
        "version": version(),
        "hint": "" if available() else "python -m pip install -U yt-dlp",
    }
