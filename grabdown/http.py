"""HTTP 层：统一 header、Range 探测、带重试的字节流。"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import unquote, urlsplit

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CHUNK = 1 << 20  # 1 MiB


class GrabError(Exception):
    pass


@dataclass
class Probe:
    url: str
    status: int
    size: int | None
    accept_ranges: bool
    mime: str
    filename: str
    etag: str | None = None
    last_modified: str | None = None
    final_url: str = ""


def guess_filename(url: str, content_disposition: str | None = None, fallback: str = "download.bin") -> str:
    if content_disposition:
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", content_disposition, re.I)
        if m:
            name = unquote(m.group(1).strip())
            if name:
                return sanitize(name)
    path = unquote(urlsplit(url).path)
    name = os.path.basename(path) or fallback
    return sanitize(name)


def sanitize(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or "download.bin"


class Session:
    def __init__(self, headers: dict[str, str] | None = None, *, timeout: float = 30.0,
                 retries: int = 3, proxy: str | None = None, verify: bool = True, ua: str = DEFAULT_UA):
        self.s = requests.Session()
        self.s.trust_env = True
        self.timeout = timeout
        self.retries = retries
        self.proxy = proxy
        self.verify = verify
        self.base_headers = {
            "User-Agent": ua,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        }
        if headers:
            self.base_headers.update(headers)

    # ---- 基础 ----
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                stream: bool = False, **kw) -> requests.Response:
        h = dict(self.base_headers)
        if headers:
            h.update(headers)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.s.request(method, url, headers=h, timeout=self.timeout,
                                   stream=stream, verify=self.verify,
                                   proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                                   **kw)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    wait = float(r.headers.get("retry-after") or 0) or min(1.5 * (attempt + 1), 6)
                    time.sleep(wait)
                    continue
                return r
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt >= self.retries:
                    break
                time.sleep(min(1.5 * (attempt + 1), 6))
        raise GrabError(f"请求失败 {url}: {last}")

    def probe(self, url: str, headers: dict[str, str] | None = None) -> Probe:
        r = self.request("HEAD", url, headers=headers)
        if r.status_code >= 400 or "content-length" not in {k.lower() for k in r.headers}:
            # 有些 CDN 不给 HEAD，用 Range: bytes=0-0 探
            r2 = self.request("GET", url, headers={**(headers or {}), "Range": "bytes=0-0"}, stream=True)
            try:
                r2.close()
            except Exception:
                pass
            r = r2 if r2.status_code in (200, 206) else r

        size = None
        cr = r.headers.get("content-range")
        if cr and "/" in cr:
            tail = cr.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                size = int(tail)
        if size is None and r.headers.get("content-length", "").isdigit():
            size = int(r.headers["content-length"])

        ranges = r.headers.get("accept-ranges", "").lower() == "bytes" or r.status_code == 206
        return Probe(
            url=url,
            status=r.status_code,
            size=size,
            accept_ranges=ranges,
            mime=r.headers.get("content-type", ""),
            filename=guess_filename(r.url or url, r.headers.get("content-disposition")),
            etag=r.headers.get("etag"),
            last_modified=r.headers.get("last-modified"),
            final_url=r.url or url,
        )

    def stream_bytes(self, url: str, *, headers: dict[str, str] | None = None,
                     start: int | None = None, end: int | None = None) -> Iterator[bytes]:
        h = dict(headers or {})
        if start is not None and end is not None:
            h["Range"] = f"bytes={start}-{end}"
        r = self.request("GET", url, headers=h, stream=True)
        if r.status_code >= 400:
            raise GrabError(f"HTTP {r.status_code} ({url})")
        try:
            for chunk in r.iter_content(CHUNK):
                if chunk:
                    yield chunk
        finally:
            r.close()

    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        r = self.request("GET", url, headers=headers)
        if r.status_code >= 400:
            raise GrabError(f"HTTP {r.status_code} ({url})")
        return r.content

    def get_text(self, url: str, headers: dict[str, str] | None = None) -> tuple[str, str]:
        r = self.request("GET", url, headers=headers)
        if r.status_code >= 400:
            raise GrabError(f"HTTP {r.status_code} ({url})")
        return r.text, r.url or url
