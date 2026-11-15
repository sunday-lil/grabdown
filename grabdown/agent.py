"""本地下载代理（给浏览器扩展用的 HTTP 桥）。

扩展里点「下载」→ POST /jobs → 这里用真·多线程引擎去下，扩展轮询进度。

    python -m grabdown agent --port 8899 [--token SECRET] [--out ./downloads]

接口（全部 CORS 放开，但只监听 127.0.0.1）：
    GET  /ping                → {ok, version, agent}
    GET  /jobs                → 任务列表
    POST /jobs                → 新建任务 {url, kind?, quality?, referer?, cookie?, out_dir?, threads?}
    GET  /jobs/<id>           → 单任务状态
    POST /jobs/<id>/cancel    → 取消（已完成的段保留，可续传）
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__, platforms, ws
from .engine import download
from .extract import resolve
from .hls import download_hls
from .http import GrabError, Session
from .sites import PornHubExtractor  # noqa: F401  (注册提取器)
from .extract import resolve as resolve_media

DEFAULT_PORT = 8899
MAX_JOBS = 200


@dataclass
class Job:
    id: str
    url: str
    kind: str = "auto"          # auto | file | hls
    quality: str = "best"
    referer: str = ""
    cookie: str = ""
    threads: int = 8
    out_dir: str = "downloads"
    state: str = "queued"       # queued | running | done | error | cancelled
    done_bytes: int = 0
    total_bytes: int = 0
    out_path: str = ""
    title: str = ""
    error: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def view(self) -> dict[str, Any]:
        d = asdict(self)
        d["percent"] = round(self.done_bytes * 100.0 / self.total_bytes, 1) if self.total_bytes else 0.0
        return d


class Agent:
    def __init__(self, out_dir: str = "downloads", token: str | None = None, max_workers: int = 2):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="grabjob")
        self.token = token
        self.out_dir = os.path.abspath(out_dir)   # 日志里必须是绝对路径，不然用户不知道文件在哪
        self.hub = ws.Hub()
        self._push_at: dict[str, float] = {}

    # ---- 实时推送 ----
    def _log_job(self, job: "Job", prev: str | None) -> None:
        if prev == job.state:
            return
        tag = {"running": "开始", "done": "完成", "error": "失败", "cancelled": "取消"}.get(job.state, job.state)
        name = job.title or job.url.rsplit("/", 1)[-1][:60]
        if job.state == "done":
            print(f"  [{tag}] {name}  →  {job.out_path}")
        elif job.state == "error":
            print(f"  [{tag}] {name}  {job.error}")
        else:
            print(f"  [{tag}] {name}")

    def push(self, job: "Job", *, force: bool = False) -> None:
        # 状态变化先写控制台（和有没有 WS 订阅无关），否则纯命令行用就会一片安静
        prev = getattr(job, "_prev_state", None)
        job._prev_state = job.state  # type: ignore[attr-defined]
        self._log_job(job, prev)

        if self.hub.count() == 0:
            return
        now = time.time()
        if not force and now - self._push_at.get(job.id, 0) < 0.15:
            return
        self._push_at[job.id] = now
        self.hub.broadcast({"type": "job", "job": job.view()})

    # ---- 任务 ----
    def submit(self, payload: dict[str, Any]) -> Job:
        url = (payload.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise GrabError("url 必须是 http(s)")
        kind = (payload.get("kind") or "auto").lower()
        # 扩展提交的是页面地址（比如 bilibili 稿件页）时，别当普通文件下，交给平台引擎
        plat = platforms.detect(url)
        if plat and plat.engine == "ytdlp" and not platforms.is_direct_media(url):
            kind = "ytdlp"
        job = Job(
            id=uuid.uuid4().hex[:12],
            url=url,
            kind=kind,
            quality=str(payload.get("quality") or "best"),
            referer=payload.get("referer") or "",
            cookie=payload.get("cookie") or "",
            threads=max(1, min(int(payload.get("threads") or 8), 32)),
            out_dir=payload.get("out_dir") or self.out_dir,
            title=(payload.get("name") or "").strip(),
        )
        with self.lock:
            if len(self.jobs) > MAX_JOBS:
                for k in [k for k, v in self.jobs.items() if v.state in ("done", "error", "cancelled")][:50]:
                    self.jobs.pop(k, None)
            self.jobs[job.id] = job
        self.pool.submit(self._run, job)
        self.push(job, force=True)
        return job

    def get(self, jid: str) -> Job | None:
        with self.lock:
            return self.jobs.get(jid)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [j.view() for j in sorted(self.jobs.values(), key=lambda x: x.created, reverse=True)]

    def cancel(self, jid: str) -> bool:
        j = self.get(jid)
        if not j or j.state in ("done", "error"):
            return False
        j.state = "cancelled"
        self.push(j, force=True)
        return True

    # ---- 执行 ----
    @staticmethod
    def _is_ph(url: str) -> bool:
        return "phncdn.com" in url or "pornhub" in url

    def _ph_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """PH 的 CDN 要 Referer + 那几个 cookie，否则容易 403/410。"""
        h = dict(headers)
        h.setdefault("Referer", "https://www.pornhub.com/")
        h.setdefault("Cookie", PornHubExtractor.COOKIES)
        return h

    @staticmethod
    def _refresh_target(session: Session, referer: str, quality: str) -> tuple[str, str, dict[str, str]] | None:
        """410/403 多半是签名过期：回页面重新解析一次，拿新链接。"""
        if not referer:
            return None
        try:
            _ext, info = resolve_media(session, referer)
            fmt = info.pick(quality)
            return fmt.url, info.title, dict(info.headers or {})
        except Exception as e:  # noqa: BLE001
            print(f"  [重试] 重新解析页面失败：{e}")
            return None

    def _run(self, job: Job) -> None:
        if job.state == "cancelled":
            return
        job.state = "running"
        job.updated = time.time()
        self.push(job, force=True)
        headers: dict[str, str] = {}
        if job.referer:
            headers["Referer"] = job.referer
        if job.cookie:
            headers["Cookie"] = job.cookie
        s = Session(headers=headers, retries=2, timeout=30)

        def hook(done: int, total: int) -> None:
            job.done_bytes, job.total_bytes = done, total or job.total_bytes
            job.updated = time.time()
            self.push(job)

        try:
            kind = job.kind
            target_url, title = job.url, ""
            if kind == "auto" or kind not in ("file", "hls", "ytdlp"):
                try:
                    ext, info = resolve(s, job.url)
                    fmt = info.pick(job.quality)
                    target_url, title = fmt.url, info.title
                    # fmt.kind 决定走哪条路：hls 分片 / ytdlp 交给 yt-dlp / 其余当普通文件
                    kind = "hls" if fmt.kind == "hls" else ("ytdlp" if fmt.kind == "ytdlp" else "file")
                    job.title = title
                    if info.headers:
                        s.base_headers.update({k: v for k, v in info.headers.items() if v})
                except GrabError:
                    kind = "hls" if ".m3u8" in job.url.lower() else "file"

            outdir = Path(job.out_dir)
            outdir.mkdir(parents=True, exist_ok=True)
            stem = _safe(job.title or title or target_url.rsplit("/", 1)[-1].split("?")[0] or "download")
            for ext in (".m3u8", ".mp4", ".ts", ".m4s"):
                if stem.lower().endswith(ext):
                    stem = stem[: -len(ext)]
            if self._is_ph(target_url):
                headers = self._ph_headers(headers)
                job.threads = min(job.threads, 4)   # PH 上开太多线程容易被 CDN 掐

            refreshed = False
            while True:
                try:
                    if kind == "ytdlp":
                        from . import ytdlp_backend as ytb

                        out = ytb.download(job.url, outdir, quality=job.quality, hook=hook,
                                           cookie=job.cookie or None,
                                           referer=job.referer or None)
                        dest = out
                    elif kind == "hls":
                        dest = _unique(outdir / f"{stem}.mp4")
                        out = download_hls(s, target_url, dest, quality=job.quality, threads=job.threads,
                                           headers=headers, remux=True, show_progress=False, hook=hook)
                    else:
                        dest = _unique(outdir / (stem if "." in stem else stem + ".bin"))
                        out = download(s, target_url, dest, threads=job.threads, headers=headers,
                                       resume=True, show_progress=False, hook=hook)
                    break
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    stale = ("410" in msg) or ("403" in msg)
                    if not refreshed and stale and job.referer:
                        print(f"  [重试] 链接失效（{msg[:60]}），回页面取新链…")
                        got = self._refresh_target(s, job.referer, job.quality)
                        if got and got[0] == target_url:
                            print("  [重试] 页面给的还是同一条失效链接：浏览器里刷新页面再点下载")
                            got = None
                        if got:
                            target_url, new_title, new_hdr = got
                            job.title = job.title or new_title
                            headers.update({k: v for k, v in new_hdr.items() if v})
                            if self._is_ph(target_url):
                                headers = self._ph_headers(headers)
                            refreshed = True
                            print(f"  [重试] 新链接已就绪，重新下载")
                            continue
                    raise
            if job.state != "cancelled":
                job.out_path = str(out)
                job.state = "done"
                try:
                    job.done_bytes = job.total_bytes = os.path.getsize(out)
                except OSError:
                    pass
        except Exception as e:  # noqa: BLE001
            msg = f"{e.__class__.__name__}: {e}"
            if "410" in msg:
                msg = "链接已过期（HTTP 410）：回页面重新播放一下、重新解析再点下载"
            elif "403" in msg:
                msg = "被 CDN 拒绝（HTTP 403）：签名过期或缺 Referer，重新解析页面再试"
            job.error = msg
            if job.state != "cancelled":
                job.state = "error"
        finally:
            job.updated = time.time()
            self.push(job, force=True)


def _unique(path: Path) -> Path:
    """同名不覆盖：a.mp4 → a (2).mp4，避免第二次下载把第一次的成果盖掉。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 200):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{stem} ({int(time.time())}){suffix}")


def _safe(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    bad = '<>:"|?*\x00-\x1f'
    name = "".join("_" if c in bad else c for c in name).strip(" .")
    return (name or "download")[:150]


class Handler(BaseHTTPRequestHandler):
    agent: Agent | None = None
    quiet = True

    def log_message(self, fmt, *args):
        if not self.quiet:
            super().log_message(fmt, *args)

    # ---- helpers ----
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Grab-Token")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        tok = self.agent.token if self.agent else None
        if not tok:
            return True
        return self.headers.get("X-Grab-Token") == tok

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/ws" and "websocket" in (self.headers.get("upgrade") or "").lower():
            return self._ws_upgrade()
        if not self._auth_ok():
            return self._json(403, {"ok": False, "error": "bad token"})
        if path == "/ping":
            return self._json(200, {"ok": True, "agent": "grabdown", "version": __version__})
        if path == "/jobs":
            return self._json(200, {"ok": True, "jobs": self.agent.list()})
        if path.startswith("/jobs/"):
            j = self.agent.get(path.split("/")[2])
            if not j:
                return self._json(404, {"ok": False, "error": "no such job"})
            return self._json(200, {"ok": True, "job": j.view()})
        return self._json(404, {"ok": False, "error": "not found"})

    def _ws_upgrade(self) -> None:
        if not self._auth_ok():
            self.send_response(403)
            self.end_headers()
            return
        key = self.headers.get("Sec-WebSocket-Key") or ""
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
        self.end_headers()
        self.close_connection = True
        peer = ws.Peer(self.connection, mask=False)
        agent = self.agent
        agent.hub.add(peer)
        peer.send_json({"type": "hello", "agent": "grabdown", "version": __version__,
                        "jobs": agent.list()})
        try:
            peer.pump()
        finally:
            agent.hub.drop(peer)
            try:
                self.connection.close()
            except Exception:
                pass

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if not self._auth_ok():
            return self._json(403, {"ok": False, "error": "bad token"})
        if path == "/jobs":
            try:
                n = int(self.headers.get("content-length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                job = self.agent.submit(payload)
            except GrabError as e:
                return self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._json(400, {"ok": False, "error": f"bad request: {e}"})
            return self._json(200, {"ok": True, "job": job.view()})
        if path == "/shutdown":
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._json(200, {"ok": True, "bye": True})
        if path.startswith("/jobs/") and path.endswith("/cancel"):
            jid = path.split("/")[2]
            ok = self.agent.cancel(jid)
            return self._json(200 if ok else 404, {"ok": ok})
        return self._json(404, {"ok": False, "error": "not found"})


def serve(port: int = DEFAULT_PORT, *, host: str = "127.0.0.1", out_dir: str = "downloads",
          token: str | None = None, verbose: bool = False, workers: int = 2) -> ThreadingHTTPServer:
    agent = Agent(out_dir=out_dir, token=token, max_workers=workers)

    # 每个 server 用自己的 Handler 子类：Handler.agent 是类属性，
    # 同一进程里起多个 agent（测试、多实例）时不能互相覆盖。
    class _Handler(Handler):
        pass

    _Handler.agent = agent
    _Handler.quiet = not verbose
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.agent = agent  # type: ignore[attr-defined]
    return httpd


def is_running(port: int = DEFAULT_PORT, timeout: float = 1.5) -> dict | None:
    """端口上是否已经有一个 grabdown agent（同一个 8899 被多开会让任务乱窜）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=timeout) as r:
            data = json.loads(r.read() or b"{}")
        if data.get("agent") == "grabdown":
            return data
    except Exception:
        return None
    return None


def stop(port: int = DEFAULT_PORT) -> bool:
    import urllib.request

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/shutdown", method="POST",
                                     data=b"{}", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def main(port: int = DEFAULT_PORT, **kw) -> int:
    alive = is_running(port)
    if alive:
        print(f"grabdown agent 已经在跑：http://127.0.0.1:{port}  (v{alive.get('version', '?')})")
        print("  不用再开一个；要重启就先跑：python -m grabdown agent --stop")
        return 3
    httpd = serve(port, **kw)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"grabdown agent v{__version__} → {url}")
    print(f"  实时通道 ws://127.0.0.1:{httpd.server_address[1]}/ws")
    print("  扩展设置里的地址填这个；Ctrl-C 结束")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0
