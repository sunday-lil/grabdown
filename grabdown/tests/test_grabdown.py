"""grabdown 测试：分段引擎 / 续传 / HLS(AES) / 提取器。

跑：python -m unittest discover -s grabdown/tests -t . -v
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from grabdown import engine, hls  # noqa: E402
from grabdown.extract import Format, MediaInfo, scan_media_links  # noqa: E402
from grabdown.http import GrabError, Session  # noqa: E402
from grabdown.sites import PornHubExtractor  # noqa: E402
from grabdown.tests import serve  # noqa: E402

BLOB = bytes((i * 7 + 13) % 256 for i in range(3 * 1024 * 1024 + 12345))  # 3MB 可复现


def _pkcs7(data: bytes, block: int = 16) -> bytes:
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def _aes_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(_pkcs7(data)) + enc.finalize()


class TestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = {
            "/multi.bin": (BLOB, "application/octet-stream", True),
            "/single.bin": (BLOB, "application/octet-stream", False),
            "/small.txt": (b"hello grabdown", "text/plain", True),
        }
        cls.httpd, cls.base = serve.serve(cls.routes)

    @classmethod
    def tearDownClass(cls):
        serve.stop(cls.httpd)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.s = Session(retries=1, timeout=10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_probe(self):
        p = self.s.probe(self.base + "/multi.bin")
        self.assertEqual(p.size, len(BLOB))
        self.assertTrue(p.accept_ranges)
        p2 = self.s.probe(self.base + "/single.bin")
        self.assertFalse(p2.accept_ranges)

    def test_multithread_matches_hash(self):
        out = self.dir / "multi.bin"
        engine.download(self.s, self.base + "/multi.bin", out, threads=6, show_progress=False)
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(),
                         hashlib.sha256(BLOB).hexdigest())
        ranges = [r for (m, p, r) in serve.Handler.log if p == "/multi.bin" and r]
        self.assertGreaterEqual(len(ranges), 2, "应该发起了多个 Range 请求")

    def test_no_range_falls_back_to_single_stream(self):
        out = self.dir / "single.bin"
        before = len(serve.Handler.log)
        engine.download(self.s, self.base + "/single.bin", out, threads=8, show_progress=False)
        self.assertEqual(out.read_bytes(), BLOB)
        gets = [1 for (m, p, r) in serve.Handler.log[before:] if m == "GET" and p == "/single.bin"]
        self.assertEqual(len(gets), 1, "不支持 Range 时只应发一次 GET（不做分段）")

    def test_resume_skips_completed_segments(self):
        url = self.base + "/multi.bin"
        out = self.dir / "resume.bin"
        plan = engine.build_plan(self.s, url, out, threads=4)
        self.assertGreaterEqual(len(plan.segments), 4)

        # 手工把前 n-1 段写成"已完成"，只留最后一段
        for seg in plan.segments[:-1]:
            with open(seg.path, "wb") as f:
                f.write(BLOB[seg.start:seg.end + 1])
            seg.done = seg.size
        engine.save_state(plan)

        before = len(serve.Handler.log)
        engine.download(self.s, url, out, threads=4, show_progress=False)
        fetched = [p for (m, p, r) in serve.Handler.log[before:] if p == "/multi.bin" and r]
        self.assertEqual(len(fetched), 1, f"续传应只拉 1 段，实际 {len(fetched)}")
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(),
                         hashlib.sha256(BLOB).hexdigest())

    def test_small_file(self):
        out = self.dir / "small.txt"
        engine.download(self.s, self.base + "/small.txt", out, threads=8, show_progress=False)
        self.assertEqual(out.read_text(), "hello grabdown")


class TestHlsParse(unittest.TestCase):
    MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080/index.m3u8
"""
    MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXTINF:4.0,
seg0.ts
#EXTINF:4.0,
seg1.ts
#EXT-X-ENDLIST
"""

    def test_master_variants(self):
        vs = hls.parse_master(self.MASTER, "https://cdn.test/play/master.m3u8")
        self.assertEqual([v.height for v in vs], [360, 720, 1080])
        self.assertEqual(vs[1].url, "https://cdn.test/play/720/index.m3u8")

    def test_pick_variant(self):
        vs = hls.parse_master(self.MASTER, "https://cdn.test/play/master.m3u8")
        self.assertEqual(hls.pick_variant(vs, "720").height, 720)
        self.assertEqual(hls.pick_variant(vs, "best").height, 1080)
        self.assertEqual(hls.pick_variant(vs, "900").height, 720)   # 就近向下
        self.assertEqual(hls.pick_variant(vs, "worst").height, 360)

    def test_media_with_key(self):
        m = hls.parse_media(self.MEDIA, "https://cdn.test/play/720/index.m3u8")
        self.assertEqual(len(m.segments), 2)
        self.assertEqual(m.segments[0].key.method, "AES-128")
        self.assertEqual(m.segments[0].key.uri, "https://cdn.test/play/720/key.bin")
        self.assertEqual(m.segments[1].url, "https://cdn.test/play/720/seg1.ts")


def _need_crypto():
    try:
        import cryptography  # noqa: F401

        return True
    except Exception:
        return False


def _need_ffmpeg():
    from grabdown.tools import find_ffmpeg

    return bool(find_ffmpeg())


class TestHlsDownload(unittest.TestCase):
    KEY = bytes(range(16))
    SEGS = [
        b"#EXTM3U\nsegment-one-payload-" + bytes(30),
        b"segment-two-payload-" + bytes(40),
        b"segment-three-payload-" + bytes(50),
    ]

    @classmethod
    def setUpClass(cls):
        if not _need_crypto():
            raise unittest.SkipTest("缺 cryptography，跳过 AES 用例")
        routes = {}
        media = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4", "#EXT-X-MEDIA-SEQUENCE:0",
                 '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"']
        for i, seg in enumerate(cls.SEGS):
            iv = i.to_bytes(16, "big")
            routes[f"/play/seg{i}.ts"] = (_aes_encrypt(seg, cls.KEY, iv), "video/mp2t", True)
            media += [f"#EXTINF:4.0,", f"seg{i}.ts"]
        media.append("#EXT-X-ENDLIST")
        routes["/play/index.m3u8"] = ("\n".join(media).encode(), "application/vnd.apple.mpegurl", True)
        routes["/play/key.bin"] = (cls.KEY, "application/octet-stream", True)
        # 明文版本（无 KEY）
        plain = ["#EXTM3U", "#EXT-X-TARGETDURATION:4"]
        for i, seg in enumerate(cls.SEGS):
            routes[f"/plain/seg{i}.ts"] = (seg, "video/mp2t", True)
            plain += [f"#EXTINF:4.0,", f"seg{i}.ts"]
        plain.append("#EXT-X-ENDLIST")
        routes["/plain/index.m3u8"] = ("\n".join(plain).encode(), "application/vnd.apple.mpegurl", True)
        # master 指向明文
        master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n"
                  "plain/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\n"
                  "plain/index.m3u8\n")
        routes["/master.m3u8"] = (master.encode(), "application/vnd.apple.mpegurl", True)
        cls.httpd, cls.base = serve.serve(routes)

    @classmethod
    def tearDownClass(cls):
        serve.stop(cls.httpd)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.s = Session(retries=1, timeout=10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_aes_decrypt_and_concat(self):
        out = self.dir / "v.mp4"
        p = hls.download_hls(self.s, self.base + "/play/index.m3u8", out,
                             threads=3, remux=False, show_progress=False)
        self.assertEqual(p.read_bytes(), b"".join(self.SEGS))

    def test_plain_playlist(self):
        out = self.dir / "plain.mp4"
        p = hls.download_hls(self.s, self.base + "/plain/index.m3u8", out,
                             threads=2, remux=False, show_progress=False)
        self.assertEqual(p.read_bytes(), b"".join(self.SEGS))

    def test_master_selects_variant(self):
        out = self.dir / "m.mp4"
        p = hls.download_hls(self.s, self.base + "/master.m3u8", out, quality="720",
                             threads=2, remux=False, show_progress=False)
        self.assertEqual(p.read_bytes(), b"".join(self.SEGS))


class _FakeSession:
    """只实现 get_text/get_bytes，供提取器单测用（不联网）。"""

    def __init__(self, page: str, final: str = "", blobs: dict | None = None):
        self.page = page
        self.final = final or "https://www.pornhub.com/view_video.php?viewkey=ph123"
        self.blobs = blobs or {}
        self.seen_headers = None

    def get_text(self, url, headers=None):
        self.seen_headers = headers
        return self.page, self.final

    def get_bytes(self, url, headers=None):
        return self.blobs.get(url, b"")


PH_PAGE = """<!DOCTYPE html><html><head>
<meta property="og:title" content="Test Video Title &amp; more" />
</head><body>
<script>
var flashvars_123456 = {
  "mediaDefinitions": [
    {"format":"hls","videoUrl":"https://ev.phncdn.com/videos/202401/01/123/master.m3u8?validfrom=1&hash=abc","quality":"auto","defaultQuality":true},
    {"format":"mp4","videoUrl":"https://ev.phncdn.com/videos/202401/01/123/1080P_4M.mp4?hash=abc","quality":"1080","height":1080},
    {"format":"mp4","videoUrl":"https://ev.phncdn.com/videos/202401/01/123/720P_2M.mp4?hash=abc","quality":"720","height":720}
  ],
  "video_title": "x"
};
</script></body></html>"""


class TestExtractors(unittest.TestCase):
    def test_pornhub_formats(self):
        ext = PornHubExtractor()
        info = ext.extract(_FakeSession(PH_PAGE), "https://www.pornhub.com/view_video.php?viewkey=ph123")
        self.assertEqual(info.site, "pornhub")
        self.assertIn("Test Video Title", info.title)
        self.assertEqual(len(info.formats), 3)
        kinds = {f.kind for f in info.formats}
        self.assertEqual(kinds, {"hls", "mp4"})
        # best 应该优先 hls
        best = info.pick("best")
        self.assertEqual(best.kind, "hls")
        # 720 精确命中 mp4
        self.assertEqual(info.pick("720").height, 720)
        # 只带 1080 时降级到 720
        self.assertEqual(info.pick("1440").height, 1080)

    def test_pornhub_match(self):
        ext = PornHubExtractor()
        self.assertTrue(ext.match("https://www.pornhub.com/view_video.php?viewkey=ph1"))
        self.assertTrue(ext.match("https://ev.phncdn.com/x/master.m3u8"))
        self.assertFalse(ext.match("https://example.com/v.mp4"))

    def test_pornhub_age_gate_message(self):
        ext = PornHubExtractor()
        with self.assertRaises(GrabError) as ctx:
            ext.extract(_FakeSession("<html>Please verify your age</html>"),
                        "https://www.pornhub.com/view_video.php?viewkey=ph1")
        self.assertIn("flashvars", str(ctx.exception))

    def test_scan_media_links(self):
        html = '''<script>var a="https://cdn.test/x/master.m3u8?k=1"; var b='/media/v.mp4';</script>'''
        links = scan_media_links(html, "https://page.test/p/1")
        self.assertIn("https://cdn.test/x/master.m3u8?k=1", links)
        self.assertIn("https://page.test/media/v.mp4", links)

    def test_media_pick(self):
        info = MediaInfo(site="t", title="t", page_url="u", formats=[
            Format(quality="360", url="u1", kind="mp4", height=360),
            Format(quality="720", url="u2", kind="mp4", height=720),
            Format(quality="auto", url="u3", kind="hls", height=None),
        ])
        self.assertEqual(info.pick("best").url, "u3")      # hls 优先
        self.assertEqual(info.pick("720").url, "u2")
        self.assertEqual(info.pick("worst").url, "u1")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestToolsAndExtractorOrder(unittest.TestCase):
    """ffmpeg 定位 + 提取器优先级（每一条都对应踩过的坑）。"""

    def test_find_ffmpeg_env_override(self):
        import os
        from grabdown import tools

        real = tools.find_ffmpeg()
        if not real:
            self.skipTest("这台机器没装 ffmpeg，跳过（CI 的 windows 镜像默认没有）")
        os.environ["GRAB_FFMPEG"] = real
        tools.find_ffmpeg.cache_clear()
        try:
            self.assertEqual(tools.find_ffmpeg(), real)
        finally:
            os.environ.pop("GRAB_FFMPEG", None)
            tools.find_ffmpeg.cache_clear()

    def test_find_ffmpeg_ignores_broken_path(self):
        import os
        from grabdown import tools

        os.environ["GRAB_FFMPEG"] = os.path.join(tempfile.gettempdir(), "definitely-not-ffmpeg.exe")
        tools.find_ffmpeg.cache_clear()
        try:
            got = tools.find_ffmpeg()
            # 坏路径要被跳过；这台机器上仍应找到真的 ffmpeg
            self.assertNotEqual(got, os.environ["GRAB_FFMPEG"])
            if got:
                self.assertIn("ffmpeg", got.lower())
        finally:
            os.environ.pop("GRAB_FFMPEG", None)
            tools.find_ffmpeg.cache_clear()

    def test_diagnose_fields(self):
        from grabdown.tools import diagnose

        d = diagnose()
        for k in ("python", "executable", "ffmpeg", "ffmpeg_version"):
            self.assertIn(k, d)

    def test_direct_extractor_wins_for_m3u8(self):
        """回归：以前 .m3u8 直链会被 generic 当网页扫，导致「扫不到媒体」。"""
        from grabdown.extract import REGISTRY

        names = [e.name for e in REGISTRY]
        self.assertEqual(names[0], "direct", names)
        hit = [e for e in REGISTRY if e.match("https://cdn.test/x/master.m3u8")][0]
        self.assertEqual(hit.name, "direct")

    def test_hls_falls_back_to_ts_without_ffmpeg(self):
        from grabdown import hls as hlsmod
        from grabdown.http import Session
        from grabdown.tests import serve as srv

        seg = b"plain-ts-segment-" * 10
        routes = {
            "/i.m3u8": (b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\ns0.ts\n#EXT-X-ENDLIST\n",
                        "application/vnd.apple.mpegurl", True),
            "/s0.ts": (seg, "video/mp2t", True),
        }
        httpd, base = srv.serve(routes)
        tmp = tempfile.TemporaryDirectory()
        old = hlsmod.find_ffmpeg
        hlsmod.find_ffmpeg = lambda *a, **k: None
        try:
            out = Path(tmp.name) / "v.mp4"
            got = hlsmod.download_hls(Session(retries=0), base + "/i.m3u8", out,
                                      threads=1, remux=True, show_progress=False)
            self.assertEqual(got.suffix, ".ts")
            self.assertEqual(got.read_bytes(), seg)
        finally:
            hlsmod.find_ffmpeg = old
            srv.stop(httpd)
            tmp.cleanup()


class TestVariantFallback(unittest.TestCase):
    """想要的码率 410 时，自动退到其它码率（PH 常见：某清晰度被下架）。"""

    def test_fallback_to_lower_variant(self):
        from grabdown.hls import download_hls
        from grabdown.http import Session
        from grabdown.tests import serve as srv

        seg = b"ts-payload-" * 20
        routes = {
            "/master.m3u8": (("#EXTM3U\n"
                              "#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1280x720\ngone720.m3u8\n"
                              "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360\nok360.m3u8\n"
                              ).encode(), "application/vnd.apple.mpegurl", True),
            "/gone720.m3u8": (b"", "text/plain", True),   # 会被 410 覆盖
            "/ok360.m3u8": (("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\ns0.ts\n"
                             "#EXT-X-ENDLIST\n").encode(), "application/vnd.apple.mpegurl", True),
            "/s0.ts": (seg, "video/mp2t", True),
        }
        httpd, base = srv.serve(routes)
        srv.Handler.routes["/gone720.m3u8"] = (b"", "text/plain", True)

        # 让 /gone720.m3u8 返回 410：临时包一层
        orig = srv.Handler._serve

        def patched(self, head=False):
            if self.path.startswith("/gone720.m3u8"):
                self.send_response(410)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return orig(self, head)

        srv.Handler._serve = patched
        tmp = tempfile.TemporaryDirectory()
        try:
            out = download_hls(Session(retries=0), base + "/master.m3u8",
                               Path(tmp.name) / "v.mp4", quality="720",
                               threads=1, remux=False, show_progress=False)
            self.assertEqual(Path(out).read_bytes(), seg)
        finally:
            srv.Handler._serve = orig
            srv.stop(httpd)
            tmp.cleanup()


class TestPlatforms(unittest.TestCase):
    """平台识别 + 引擎路由（不联网）。"""

    def test_detect(self):
        from grabdown import platforms as pf

        cases = {
            "https://www.bilibili.com/video/BV1xx411c7mD": "bilibili",
            "https://b23.tv/abc123": "bilibili",
            "https://v.douyin.com/xyz/": "douyin",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "youtube",
            "https://youtu.be/dQw4w9WgXcQ": "youtube",
            "https://x.com/user/status/123": "twitter",
            "https://twitter.com/user/status/123": "twitter",
            "https://www.instagram.com/reel/abc/": "instagram",
            "https://www.pornhub.com/view_video.php?viewkey=ph1": "pornhub",
            "https://www.xvideos.com/video123/x": "xvideos",
            "https://www.netflix.com/watch/1": "netflix",
            "https://example.com/video.mp4": None,
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                got = pf.detect(url)
                self.assertEqual(got.key if got else None, want)

    def test_engines(self):
        from grabdown import platforms as pf

        self.assertEqual(pf.detect("https://www.pornhub.com/view_video.php?viewkey=1").engine, "native")
        self.assertEqual(pf.detect("https://www.bilibili.com/video/BV1x").engine, "ytdlp")
        self.assertEqual(pf.detect("https://www.netflix.com/watch/1").engine, "unsupported")

    def test_direct_media_is_not_platform(self):
        from grabdown import platforms as pf

        self.assertTrue(pf.is_direct_media("https://cdn.x/film.m3u8?t=1"))
        self.assertTrue(pf.is_direct_media("https://cdn.x/a.mp4"))
        self.assertFalse(pf.is_direct_media("https://www.bilibili.com/video/BV1x"))
        self.assertEqual(pf.label("https://www.bilibili.com/video/BV1x"), "哔哩哔哩")
        self.assertEqual(pf.label("https://foo.bar/x"), "foo.bar")

    def test_selector(self):
        from grabdown.ytdlp_backend import selector

        self.assertIn("bestvideo*+bestaudio", selector("best"))
        self.assertIn("height<=1080", selector("1080"))
        self.assertIn("height<=720", selector("720p"))
        self.assertIn("worst", selector("worst"))

    def test_platform_extractor_with_stub(self):
        """把 ytdlp.probe 换成假数据，验证格式聚合与 kind。"""
        from grabdown import ytdlp_backend as ytb
        from grabdown.extract import resolve
        from grabdown.http import Session
        from grabdown.sites import platform as pl

        fake = {
            "title": "测试视频",
            "webpage_url": "https://www.bilibili.com/video/BVtest",
            "formats": [
                ytb.YtFormat("137", 1080, "mp4", 5_000_000, False, "1080p"),
                ytb.YtFormat("137", 1080, "mp4", 8_000_000, False, "1080p"),   # 同高度更高码率
                ytb.YtFormat("80", 720, "mp4", 3_000_000, False, "720p"),
                ytb.YtFormat("30216", None, "m4a", 1_000_000, True, "audio"),
            ],
        }
        old = ytb.probe
        ytb.probe = lambda *a, **k: fake
        try:
            ext, info = resolve(Session(), "https://www.bilibili.com/video/BVtest")
            self.assertEqual(info.site, "bilibili")
            self.assertEqual(info.title, "测试视频")
            kinds = {f.kind for f in info.formats}
            self.assertEqual(kinds, {"ytdlp"})
            self.assertEqual([f.height for f in info.formats], [1080, 720])
            self.assertEqual(info.pick("best").height, 1080)
            self.assertEqual(info.pick("1080").height, 1080)
            self.assertEqual(info.pick("480").height, 720)   # 就近向下 → 没有 480 时给 720? 见下
        finally:
            ytb.probe = old
