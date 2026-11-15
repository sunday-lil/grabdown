"""测试用本地 HTTP 服务：支持 Range、可配置是否忽略 Range、记录请求。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[bytes, str, bool]] = {}
    log: list[tuple[str, str, str | None]] = []

    def log_message(self, *a):  # 静音
        pass

    def do_HEAD(self):  # noqa: N802
        self._serve(head=True)

    def do_GET(self):  # noqa: N802
        self._serve(head=False)

    def _serve(self, head: bool) -> None:
        path = urlsplit(self.path).path
        rng = self.headers.get("Range")
        Handler.log.append((self.command, path, rng))
        item = Handler.routes.get(path)
        if item is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, mime, ranges_ok = item
        start, end = 0, len(body) - 1
        status = 200
        if rng and ranges_ok:
            part = rng.split("=", 1)[1]
            s, _, e = part.partition("-")
            start = int(s or 0)
            end = int(e) if e else len(body) - 1
            end = min(end, len(body) - 1)
            status = 206
        chunk = body[start:end + 1] if status == 206 else body
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(chunk)))
        if ranges_ok:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        self.send_header("ETag", '"test-etag"')
        self.end_headers()
        if not head:
            self.wfile.write(chunk)


def serve(routes: dict[str, tuple[bytes, str, bool]]):
    Handler.routes = routes
    Handler.log = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def stop(httpd) -> None:
    httpd.shutdown()
    httpd.server_close()
