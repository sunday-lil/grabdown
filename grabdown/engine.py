"""分段下载引擎：Range 切块 + 线程池 + 续传状态 + 合并落盘。

策略
- 支持 Range 且体积已知 → 切成 N 段，每段一个 .partN，全部完成后按序拼接；
- 不支持 Range → 单流下载，不做多线程（硬切只会把文件写坏）；
- 续传：<out>.grabstate.json 记录 url/etag/size 和各段完成情况，重启时跳过已完成段；
- 段大小下限 1 MiB，线程数自动收敛（避免把小文件切出一堆碎片）。

为什么用 per-part 临时文件而不是 seek+pwrite：
  Windows 上没有 os.pwrite，跨平台一致的写法就是分开写再拼，
  坏处是多占一份磁盘，好处是任意段失败都能单独重试、进度可精确恢复。
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .http import GrabError, Probe, Session

MIN_SEG = 1 << 20
STATE_SUFFIX = ".grabstate.json"


@dataclass
class Segment:
    index: int
    start: int
    end: int          # inclusive
    done: int = 0     # 已写入字节
    path: str = ""

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def complete(self) -> bool:
        return self.done >= self.size


@dataclass
class Plan:
    url: str
    out: Path
    size: int
    segments: list[Segment] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


class Progress:
    """极简进度条（不引 tqdm，避免多线程下刷屏）。"""

    def __init__(self, total: int | None, label: str = "", enabled: bool = True, hook=None):
        self.total = total or 0
        self.hook = hook
        self.label = label
        self.enabled = enabled
        self.done = 0
        self.t0 = time.time()
        self._lock = threading.Lock()
        self._last = 0.0

    def add(self, n: int) -> None:
        if not self.enabled:
            self.done += n
            if self.hook:
                try:
                    self.hook(self.done, self.total)
                except Exception:
                    pass
            return
        with self._lock:
            self.done += n
            if self.hook:
                try:
                    self.hook(self.done, self.total)
                except Exception:
                    pass
            now = time.time()
            if now - self._last < 0.25 and self.done < self.total:
                return
            self._last = now
            self._render()

    def _render(self) -> None:
        el = max(time.time() - self.t0, 1e-6)
        speed = self.done / el
        if self.total:
            pct = min(100.0, self.done * 100.0 / self.total)
            filled = int(pct / 2.5)
            bar = "█" * filled + "░" * (40 - filled)
            eta = (self.total - self.done) / speed if speed > 0 else 0
            line = f"\r{self.label} {bar} {pct:5.1f}%  {_h(self.done)}/{_h(self.total)}  {_h(speed)}/s  ETA {_t(eta)}"
        else:
            line = f"\r{self.label} {_h(self.done)}  {_h(speed)}/s"
        print(line, end="", flush=True)

    def close(self) -> None:
        if self.enabled:
            self._render()
            print()


def _h(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:,.1f}{u}" if u != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def _t(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def build_plan(session: Session, url: str, out: Path, threads: int,
               headers: dict[str, str] | None = None, probe: Probe | None = None) -> Plan:
    p = probe or session.probe(url, headers=headers)
    if p.size is None:
        raise GrabError("服务器没给 Content-Length，无法分段（改用单流模式）")
    if not p.accept_ranges:
        raise GrabError("服务器不支持 Range，无法分段（改用单流模式）")

    n = max(1, int(threads))
    seg_size = max(MIN_SEG, p.size // n if n else MIN_SEG)
    segs: list[Segment] = []
    start = 0
    idx = 0
    while start < p.size:
        end = min(start + seg_size - 1, p.size - 1)
        seg = Segment(index=idx, start=start, end=end, path=f"{out}.part{idx}")
        segs.append(seg)
        start = end + 1
        idx += 1
    return Plan(url=url, out=out, size=p.size, segments=segs, etag=p.etag,
                last_modified=p.last_modified, headers=dict(headers or {}))


def load_state(plan: Plan) -> None:
    path = Path(str(plan.out) + STATE_SUFFIX)
    if not path.exists():
        return
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if st.get("size") != plan.size or st.get("url") != plan.url:
        return
    if st.get("etag") and plan.etag and st["etag"] != plan.etag:
        return  # 远端文件换了，旧分段作废
    saved = {s["index"]: s for s in st.get("segments", [])}
    for seg in plan.segments:
        info = saved.get(seg.index)
        if not info:
            continue
        if os.path.exists(seg.path) and os.path.getsize(seg.path) == seg.size:
            seg.done = seg.size
        else:
            seg.done = 0


def save_state(plan: Plan) -> None:
    path = Path(str(plan.out) + STATE_SUFFIX)
    data = {
        "url": plan.url,
        "size": plan.size,
        "etag": plan.etag,
        "segments": [{"index": s.index, "done": s.done} for s in plan.segments],
        "updated": time.time(),
    }
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _fetch_segment(session: Session, plan: Plan, seg: Segment, progress: Progress,
                   stop: threading.Event) -> None:
    attempts = 0
    while not seg.complete:
        if stop.is_set():
            raise GrabError("已取消")
        attempts += 1
        try:
            remaining_start = seg.start + seg.done
            hdr = dict(plan.headers)
            hdr["Range"] = f"bytes={remaining_start}-{seg.end}"
            r = session.request("GET", plan.url, headers=hdr, stream=True)
            if r.status_code not in (200, 206):
                raise GrabError(f"分段 {seg.index} HTTP {r.status_code}")
            with open(seg.path, "ab") as f:
                for chunk in r.iter_content(1 << 16):
                    if not chunk:
                        continue
                    f.write(chunk)
                    seg.done += len(chunk)
                    progress.add(len(chunk))
            r.close()
            if seg.done < seg.size:
                raise GrabError(f"分段 {seg.index} 提前结束 ({seg.done}/{seg.size})")
        except Exception as e:  # noqa: BLE001
            if attempts > session.retries + 2:
                raise GrabError(f"分段 {seg.index} 反复失败: {e}") from e
            time.sleep(min(1.5 * attempts, 6))


def download(session: Session, url: str, out: Path, *, threads: int = 8,
             headers: dict[str, str] | None = None, resume: bool = True,
             show_progress: bool = True, label: str = "", hook=None) -> Path:
    """下载到 out。返回最终路径。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    probe = session.probe(url, headers=headers)

    # 不支持分段 → 单流
    if probe.size is None or not probe.accept_ranges:
        return _single_stream(session, url, out, headers, probe, show_progress, label, hook)

    plan = build_plan(session, url, out, threads, headers, probe)
    if resume:
        load_state(plan)

    pending = [s for s in plan.segments if not s.complete]
    done_bytes = sum(s.done for s in plan.segments)
    prog = Progress(plan.size, label or out.name, show_progress, hook)
    prog.done = done_bytes
    if show_progress and done_bytes:
        prog._render()

    if pending:
        stop = threading.Event()
        with ThreadPoolExecutor(max_workers=min(len(pending), max(1, threads))) as pool:
            futs = {pool.submit(_fetch_segment, session, plan, s, prog, stop): s for s in pending}
            try:
                for fut in as_completed(futs):
                    fut.result()
            except Exception:
                stop.set()
                save_state(plan)
                raise
    prog.close()
    save_state(plan)

    # 合并
    tmp = Path(str(out) + ".merging")
    with open(tmp, "wb") as dst:
        for seg in plan.segments:
            with open(seg.path, "rb") as src:
                while True:
                    buf = src.read(1 << 20)
                    if not buf:
                        break
                    dst.write(buf)
    os.replace(tmp, out)
    for seg in plan.segments:
        try:
            os.remove(seg.path)
        except OSError:
            pass
    try:
        os.remove(str(out) + STATE_SUFFIX)
    except OSError:
        pass
    return out


def _single_stream(session: Session, url: str, out: Path, headers: dict[str, str] | None,
                   probe: Probe, show_progress: bool, label: str, hook=None) -> Path:
    tmp = Path(str(out) + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0
    hdr = dict(headers or {})
    if have and probe.accept_ranges:
        hdr["Range"] = f"bytes={have}-"
    r = session.request("GET", url, headers=hdr, stream=True)
    if r.status_code not in (200, 206):
        raise GrabError(f"HTTP {r.status_code} ({url})")
    total = None
    if r.headers.get("content-length", "").isdigit():
        total = int(r.headers["content-length"]) + (have if r.status_code == 206 else 0)
    prog = Progress(total, label or out.name, show_progress, hook)
    prog.done = have
    mode = "ab" if (have and r.status_code == 206) else "wb"
    if mode == "wb":
        have = 0
        prog.done = 0
    with open(tmp, mode) as f:
        for chunk in r.iter_content(1 << 16):
            if not chunk:
                continue
            f.write(chunk)
            prog.add(len(chunk))
    prog.close()
    os.replace(tmp, out)
    return out
