"""HLS：master 选码率 → 分片并发下载 → AES-128 解密 → 拼接 →（可选）ffmpeg 无重编码合流。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse as up
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .engine import Progress, _h
from .http import GrabError, Session
from .tools import find_ffmpeg

ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line: str) -> dict[str, str]:
    body = line.split(":", 1)[1] if ":" in line else ""
    out: dict[str, str] = {}
    for k, v in ATTR_RE.findall(body):
        out[k] = v.strip('"')
    return out


def _abs(base: str, ref: str) -> str:
    return up.urljoin(base, ref)


@dataclass
class Variant:
    url: str
    height: int | None = None
    bandwidth: int | None = None
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or (f"{self.height}p" if self.height else "") or f"{self.bandwidth or 0 // 1000}kbps"


@dataclass
class Key:
    method: str = "NONE"
    uri: str | None = None
    iv: bytes | None = None


@dataclass
class Seg:
    seq: int
    url: str
    duration: float = 0.0
    key: Key | None = None


@dataclass
class Media:
    segments: list[Seg] = field(default_factory=list)
    init: str | None = None
    media_sequence: int = 0

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)


def parse_master(text: str, base: str) -> list[Variant]:
    out: list[Variant] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            a = _attrs(ln)
            res = a.get("RESOLUTION", "")
            h = int(res.split("x")[-1]) if "x" in res else None
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt and not nxt.startswith("#"):
                out.append(Variant(url=_abs(base, nxt), height=h,
                                   bandwidth=int(a["BANDWIDTH"]) if a.get("BANDWIDTH", "").isdigit() else None,
                                   name=a.get("NAME", "")))
    return out


def parse_media(text: str, base: str) -> Media:
    m = Media()
    cur_key: Key | None = None
    seq = 0
    dur = 0.0
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            m.media_sequence = int(ln.split(":", 1)[1])
            seq = m.media_sequence
        elif ln.startswith("#EXT-X-KEY:"):
            a = _attrs(ln)
            cur_key = Key(method=a.get("METHOD", "NONE"), uri=_abs(base, a["URI"]) if a.get("URI") else None,
                          iv=bytes.fromhex(a["IV"][2:]) if a.get("IV", "").startswith("0x") else None)
        elif ln.startswith("#EXT-X-MAP:"):
            a = _attrs(ln)
            if a.get("URI"):
                m.init = _abs(base, a["URI"])
        elif ln.startswith("#EXTINF:"):
            try:
                dur = float(ln.split(":", 1)[1].split(",")[0])
            except ValueError:
                dur = 0.0
        elif not ln.startswith("#"):
            m.segments.append(Seg(seq=seq, url=_abs(base, ln), duration=dur,
                                  key=(cur_key if cur_key and cur_key.method != "NONE" else None)))
            seq += 1
    return m


def pick_variant(variants: list[Variant], quality: str) -> Variant:
    if not variants:
        raise GrabError("master 里没有可用码率")
    q = (quality or "best").lower()
    if q in ("best", "max"):
        return max(variants, key=lambda v: (v.height or 0, v.bandwidth or 0))
    if q in ("worst", "min"):
        return min(variants, key=lambda v: (v.height or 10**9, v.bandwidth or 10**9))
    want = int(re.sub(r"\D", "", q) or 0)
    exact = [v for v in variants if v.height == want]
    if exact:
        return exact[0]
    lower = [v for v in variants if (v.height or 0) <= want]
    return max(lower, key=lambda v: (v.height or 0, v.bandwidth or 0)) if lower else max(
        variants, key=lambda v: (v.height or 0, v.bandwidth or 0))


def crypto_available() -> bool:
    try:
        import cryptography  # noqa: F401

        return True
    except Exception:
        return False


def _aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception as e:  # noqa: BLE001
        raise GrabError(
            "这条 HLS 用了 AES-128 加密，需要装 cryptography：python -m pip install cryptography"
        ) from e

    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = dec.update(data) + dec.finalize()
    if out:
        pad = out[-1]
        if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
            out = out[:-pad]
    return out


def _iv_for(seg: Seg, seq_base: int) -> bytes:
    if seg.key and seg.key.iv:
        return seg.key.iv
    return (seg.seq).to_bytes(16, "big")


def _fetch_one(session: Session, seg: Seg, path: str, headers: dict[str, str],
               key_cache: dict[str, bytes], prog: Progress) -> None:
    body = session.get_bytes(seg.url, headers=headers)
    if seg.key and seg.key.uri:
        k = key_cache.get(seg.key.uri)
        if k is None:
            k = session.get_bytes(seg.key.uri, headers=headers)
            key_cache[seg.key.uri] = k
        body = _aes_cbc_decrypt(body, k, _iv_for(seg, 0))
    with open(path, "wb") as f:
        f.write(body)
    prog.add(len(body))


def _which_ffmpeg() -> str | None:
    """PATH 可能被裁掉（pythonw/计划任务），所以走 tools 的多路径查找 + 真跑一次验证。"""
    return find_ffmpeg()


def download_hls(session: Session, url: str, out: Path, *, quality: str = "best",
                 threads: int = 8, headers: dict[str, str] | None = None,
                 remux: bool = True, show_progress: bool = True, label: str = "",
                 hook=None) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text, final_url = session.get_text(url, headers=headers)
    if not text.lstrip().startswith("#EXTM3U"):
        raise GrabError("这不是 m3u8（响应里没有 #EXTM3U）")

    if "#EXT-X-STREAM-INF" in text:
        variants = parse_master(text, final_url)
        want = pick_variant(variants, quality)
        # 优先想要的码率；它若 410/403（该清晰度被下架/签名失效），按高度降级依次试
        order = [want] + sorted([v for v in variants if v is not want],
                                key=lambda v: -(v.height or 0))
        if show_progress:
            print(f"  码率: {want.label}  ({len(variants)} 个可选)")
        last_err = None
        for i, v in enumerate(order):
            try:
                text, final_url = session.get_text(v.url, headers=headers)
                if i:
                    print(f"  ! {want.label} 拉不到，已改用 {v.label}")
                break
            except GrabError as e:
                msg = str(e)
                last_err = e
                if ("410" in msg or "403" in msg) and i + 1 < len(order):
                    print(f"  ! {v.label} 不可用（{msg.split('(')[-1].split(')')[0][:60]}），换下一个")
                    continue
                raise
        else:
            raise last_err or GrabError("所有码率都拉不到")

    media = parse_media(text, final_url)
    if not media.segments:
        raise GrabError("播放列表里没有分片")

    total_hint = None
    prog = Progress(total_hint, label or out.name, show_progress, hook)
    key_cache: dict[str, bytes] = {}
    tmpdir = Path(tempfile.mkdtemp(prefix="grabhls_"))
    try:
        paths: list[str] = []
        if media.init:
            init_path = str(tmpdir / "init.bin")
            data = session.get_bytes(media.init, headers=headers)
            Path(init_path).write_bytes(data)
            paths.append(init_path)

        seg_paths = [str(tmpdir / f"seg_{i:05d}.ts") for i in range(len(media.segments))]
        with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
            futs = {
                pool.submit(_fetch_one, session, seg, p, headers or {}, key_cache, prog): i
                for i, (seg, p) in enumerate(zip(media.segments, seg_paths))
            }
            for fut in as_completed(futs):
                fut.result()
        prog.close()
        paths.extend(seg_paths)

        ts_path = out.with_suffix(".ts") if remux else out
        with open(ts_path, "wb") as dst:
            for p in paths:
                with open(p, "rb") as src:
                    shutil.copyfileobj(src, dst, 1 << 20)

        if remux and _which_ffmpeg():
            mp4 = out
            tmp_mp4 = Path(str(mp4) + ".tmp.mp4")
            cmd = [_which_ffmpeg(), "-y", "-loglevel", "error", "-i", str(ts_path),
                   "-c", "copy", "-bsf:a", "aac_adtstoasc", str(tmp_mp4)]
            rc = subprocess.run(cmd, capture_output=True).returncode
            if rc == 0 and tmp_mp4.exists():
                os.replace(tmp_mp4, mp4)
                try:
                    os.remove(ts_path)
                except OSError:
                    pass
                return mp4
            print("  ! ffmpeg 合流失败，保留 .ts")
        return ts_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
