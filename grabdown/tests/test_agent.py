"""agent（本地下载代理）测试：真起 HTTP、真下载、真进度。"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from grabdown import agent as agent_mod  # noqa: E402
from grabdown.tests import serve as srv  # noqa: E402

BLOB = bytes((i * 31 + 7) % 256 for i in range(1024 * 1024 + 4096))


def _post(url: str, payload: dict, token: str | None = None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"X-Grab-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        finally:
            e.close()


def _get(url: str, token: str | None = None):
    req = urllib.request.Request(url, headers={"X-Grab-Token": token} if token else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read())


class TestAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.base = srv.serve({"/big.bin": (BLOB, "application/octet-stream", True)})
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ag = agent_mod.serve(port=0, out_dir=cls.tmp.name, workers=2)
        import threading

        threading.Thread(target=cls.ag.serve_forever, daemon=True).start()
        cls.agent_url = f"http://127.0.0.1:{cls.ag.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.ag.shutdown()
        cls.ag.server_close()
        srv.stop(cls.httpd)
        cls.tmp.cleanup()

    def test_ping(self):
        code, data = _get(self.agent_url + "/ping")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["agent"], "grabdown")

    def test_cors_preflight(self):
        req = urllib.request.Request(self.agent_url + "/jobs", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 204)
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIn("POST", r.headers.get("Access-Control-Allow-Methods", ""))

    def test_bad_url_rejected(self):
        code, data = _post(self.agent_url + "/jobs", {"url": "ftp://x/y"})
        self.assertEqual(code, 400)
        self.assertFalse(data["ok"])

    def test_download_job_completes(self):
        code, data = _post(self.agent_url + "/jobs",
                           {"url": self.base + "/big.bin", "kind": "file", "threads": 4})
        self.assertEqual(code, 200, data)
        jid = data["job"]["id"]

        deadline = time.time() + 20
        job = None
        while time.time() < deadline:
            _, st = _get(f"{self.agent_url}/jobs/{jid}")
            job = st["job"]
            if job["state"] in ("done", "error"):
                break
            time.sleep(0.2)

        self.assertIsNotNone(job)
        self.assertEqual(job["state"], "done", job)
        out = Path(job["out_path"])
        self.assertTrue(out.exists(), job)
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(),
                         hashlib.sha256(BLOB).hexdigest())
        self.assertGreaterEqual(job["percent"], 100.0)

    def test_jobs_listed(self):
        _, data = _get(self.agent_url + "/jobs")
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["jobs"], list)

    def test_cancel_unknown(self):
        code, data = _post(self.agent_url + "/jobs/nope/cancel", {})
        self.assertEqual(code, 404)


class TestAgentToken(unittest.TestCase):
    def test_token_required(self):
        tmp = tempfile.TemporaryDirectory()
        httpd = agent_mod.serve(port=0, out_dir=tmp.name, token="s3cret")
        import threading

        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                _get(url + "/ping")
            self.assertEqual(ctx.exception.code, 403)
            code, data = _get(url + "/ping", token="s3cret")
            self.assertEqual(code, 200)
            self.assertTrue(data["ok"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAgentWebSocket(unittest.TestCase):
    """WS 通道：连上收 hello，提交任务后收到递增的进度帧，最后收到 done。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.base = srv.serve({"/big.bin": (BLOB, "application/octet-stream", True)})
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ag = agent_mod.serve(port=0, out_dir=cls.tmp.name, workers=2)
        import threading

        threading.Thread(target=cls.ag.serve_forever, daemon=True).start()
        cls.agent_url = f"http://127.0.0.1:{cls.ag.server_address[1]}"
        cls.ws_url = f"ws://127.0.0.1:{cls.ag.server_address[1]}/ws"

    @classmethod
    def tearDownClass(cls):
        cls.ag.shutdown()
        cls.ag.server_close()
        srv.stop(cls.httpd)
        cls.tmp.cleanup()

    def test_progress_stream(self):
        from grabdown import ws as wsmod

        msgs = []
        done = threading.Event() if False else None
        import threading

        got_done = threading.Event()

        def on_msg(m):
            msgs.append(m)
            if m.get("type") == "job" and m["job"]["state"] in ("done", "error"):
                got_done.set()

        cli = wsmod.Client(self.ws_url, on_message=on_msg)
        self.assertTrue(cli.connect())
        try:
            # hello 帧
            deadline = time.time() + 5
            while time.time() < deadline and not any(m.get("type") == "hello" for m in msgs):
                time.sleep(0.05)
            self.assertTrue(any(m.get("type") == "hello" for m in msgs), msgs[:3])

            code, data = _post(self.agent_url + "/jobs",
                               {"url": self.base + "/big.bin", "kind": "file", "threads": 4})
            self.assertEqual(code, 200)
            self.assertTrue(got_done.wait(25), "没收到完成帧")

            jobs = [m["job"] for m in msgs if m.get("type") == "job"]
            self.assertGreaterEqual(len(jobs), 2, "进度帧太少，可能是没推送")
            self.assertEqual(jobs[-1]["state"], "done")
            self.assertGreater(jobs[-1]["done_bytes"], 0)
            # 至少有一帧是中间态（running）
            self.assertTrue(any(j["state"] == "running" for j in jobs), jobs)
        finally:
            cli.close()

    def test_bad_token_ws_rejected(self):
        from grabdown import ws as wsmod

        tmp = tempfile.TemporaryDirectory()
        h = agent_mod.serve(port=0, out_dir=tmp.name, token="need")
        import threading

        threading.Thread(target=h.serve_forever, daemon=True).start()
        try:
            cli = wsmod.Client(f"ws://127.0.0.1:{h.server_address[1]}/ws")
            with self.assertRaises(Exception):
                cli.connect()
        finally:
            h.shutdown()
            h.server_close()
            tmp.cleanup()


class TestWsCodec(unittest.TestCase):
    def test_accept_key(self):
        from grabdown import ws as wsmod

        self.assertEqual(wsmod.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_roundtrip_small_and_large(self):
        import socket
        import struct
        import threading
        from grabdown import ws as wsmod

        a, b = socket.socketpair()
        try:
            def sender():
                wsmod.write_frame(a, wsmod.OP_TEXT, b"hi")
                wsmod.write_frame(a, wsmod.OP_TEXT, b"x" * 70000)  # 触发 64 位长度分支

            t = threading.Thread(target=sender, daemon=True)
            t.start()
            op1, d1 = wsmod.read_frame(b)
            op2, d2 = wsmod.read_frame(b)
            t.join(2)
            self.assertEqual((op1, d1), (wsmod.OP_TEXT, b"hi"))
            self.assertEqual(op2, wsmod.OP_TEXT)
            self.assertEqual(len(d2), 70000)
        finally:
            a.close()
            b.close()

    def test_client_mask_roundtrip(self):
        import socket
        import threading
        from grabdown import ws as wsmod

        a, b = socket.socketpair()
        try:
            def sender():
                wsmod.write_frame(a, wsmod.OP_TEXT, "掩码测试".encode(), mask=True)

            t = threading.Thread(target=sender, daemon=True)
            t.start()
            op, data = wsmod.read_frame(b)
            t.join(2)
            self.assertEqual(op, wsmod.OP_TEXT)
            self.assertEqual(data.decode(), "掩码测试")
        finally:
            a.close()
            b.close()


class TestAgentLifecycle(unittest.TestCase):
    """单实例守卫 + /shutdown：之前一次能悄悄起 4 个，任务全乱。"""

    def test_single_instance_and_stop(self):
        import threading

        tmp = tempfile.TemporaryDirectory()
        httpd = agent_mod.serve(port=0, out_dir=tmp.name)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            info = agent_mod.is_running(port)
            self.assertIsNotNone(info, "刚起的 agent 应该被 is_running 认出来")
            self.assertEqual(info["agent"], "grabdown")

            self.assertTrue(agent_mod.stop(port), "/shutdown 应该返回 200")
            t.join(5)
            self.assertFalse(t.is_alive(), "stop 之后 serve_forever 应该退出")
            self.assertIsNone(agent_mod.is_running(port, timeout=0.8), "停掉后不应再被探到")
        finally:
            httpd.server_close()
            tmp.cleanup()

    def test_is_running_false_on_dead_port(self):
        self.assertIsNone(agent_mod.is_running(9, timeout=0.5))


class TestAgentNamingAndErrors(unittest.TestCase):
    """文件名用页面标题 / 输出目录是绝对路径 / 410 说人话。"""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/expired.bin"):
                    self.send_response(410)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = BLOB
                rng = self.headers.get("Range")
                start, end, code = 0, len(body) - 1, 200
                if rng:
                    part = rng.split("=", 1)[1]
                    a, _, b = part.partition("-")
                    start = int(a or 0)
                    end = int(b) if b else len(body) - 1
                    code = 206
                chunk = body[start:end + 1] if code == 206 else body
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(chunk)))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                self.end_headers()
                self.wfile.write(chunk)

            do_HEAD = do_GET

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ag = agent_mod.serve(port=0, out_dir=cls.tmp.name, workers=2)
        threading.Thread(target=cls.ag.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.ag.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.ag.shutdown()
        cls.ag.server_close()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _wait(self, jid, timeout=25):
        end = time.time() + timeout
        job = None
        while time.time() < end:
            _, st = _get(f"{self.url}/jobs/{jid}")
            job = st["job"]
            if job["state"] in ("done", "error"):
                return job
            time.sleep(0.2)
        return job

    def test_name_from_page_title(self):
        code, data = _post(self.url + "/jobs", {
            "url": self.base + "/blob.bin", "kind": "file", "threads": 2,
            "name": "某个视频 标题",
        })
        self.assertEqual(code, 200, data)
        job = self._wait(data["job"]["id"])
        self.assertEqual(job["state"], "done", job)
        self.assertTrue(job["out_path"].endswith("某个视频 标题.bin"), job["out_path"])
        self.assertTrue(Path(job["out_path"]).is_absolute(), job["out_path"])

    def test_410_message_is_human(self):
        code, data = _post(self.url + "/jobs", {
            "url": self.base + "/expired.bin", "kind": "file", "threads": 1,
        })
        self.assertEqual(code, 200, data)
        job = self._wait(data["job"]["id"])
        self.assertEqual(job["state"], "error", job)
        self.assertIn("过期", job["error"], job["error"])


class TestStaleLinkRecovery(unittest.TestCase):
    """410（签名过期）→ 回页面重新解析 → 用新链接重下。这是 PH 最常见的失败。"""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        SEG = b"segment-payload-" * 40

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/old.m3u8"):
                    self.send_response(410)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/page.html"):
                    body = ('<html><title>某视频</title><script>'
                            'var src="FRESH_URL";</script></html>').replace(
                        "FRESH_URL", f"http://127.0.0.1:{self.server.server_address[1]}/fresh.m3u8")
                    self._send(body.encode(), "text/html")
                    return
                if self.path.startswith("/fresh.m3u8"):
                    body = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
                            "#EXTINF:4.0,\ns0.ts\n#EXT-X-ENDLIST\n").encode()
                    self._send(body, "application/vnd.apple.mpegurl")
                    return
                if self.path.startswith("/s0.ts"):
                    self._send(SEG, "video/mp2t")
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(body)

        cls.seg = SEG
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ag = agent_mod.serve(port=0, out_dir=cls.tmp.name, workers=1)
        threading.Thread(target=cls.ag.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.ag.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.ag.shutdown()
        cls.ag.server_close()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def test_410_then_refresh(self):
        code, data = _post(self.url + "/jobs", {
            "url": self.base + "/old.m3u8",
            "kind": "hls",
            "referer": self.base + "/page.html",
            "name": "某视频",
        })
        self.assertEqual(code, 200, data)
        jid = data["job"]["id"]
        job = None
        end = time.time() + 25
        while time.time() < end:
            _, st = _get(f"{self.url}/jobs/{jid}")
            job = st["job"]
            if job["state"] in ("done", "error"):
                break
            time.sleep(0.2)
        self.assertEqual(job["state"], "done", job)
        out = Path(job["out_path"])
        self.assertTrue(out.exists(), job)
        self.assertEqual(out.read_bytes(), self.seg, "应该是重新解析后拿到的新分片")


class TestCookiePropagation(unittest.TestCase):
    """扩展带过来的 cookie 必须真的用在请求上（PH 的链接和登录态绑定）。"""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        cls.seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                outer.seen.append(self.headers.get("Cookie") or "")
                if self.path.startswith("/p.m3u8"):
                    body = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\ns0.ts\n"
                            "#EXT-X-ENDLIST\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/s0.ts"):
                    body = b"seg"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        outer = cls
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ag = agent_mod.serve(port=0, out_dir=cls.tmp.name, workers=1)
        threading.Thread(target=cls.ag.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.ag.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.ag.shutdown()
        cls.ag.server_close()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def test_cookie_is_sent(self):
        code, data = _post(self.url + "/jobs", {
            "url": self.base + "/p.m3u8", "kind": "hls", "threads": 1,
            "cookie": "ss=abc; bs=def", "name": "c",
        })
        self.assertEqual(code, 200, data)
        jid = data["job"]["id"]
        end = time.time() + 20
        job = None
        while time.time() < end:
            _, st = _get(f"{self.url}/jobs/{jid}")
            job = st["job"]
            if job["state"] in ("done", "error"):
                break
            time.sleep(0.2)
        self.assertEqual(job["state"], "done", job)
        self.assertTrue(any("ss=abc" in c for c in self.seen), self.seen)
