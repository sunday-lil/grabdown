"""grabdown — IDM 风格的多线程下载器 + 网页媒体提取（HLS / mp4）。

设计目标：
  1. 分段并发 + 断点续传（Range），像 IDM 那样把单文件切成 N 段；
  2. 网页 → 媒体地址（HLS/mp4）的提取层，插件式；
  3. HLS 支持 master 选码率、AES-128 解密、分片并发、ffmpeg 无重编码合流。

不做：DRM（Widevine/PlayReady）绕过、付费墙绕过、登录态盗用。
"""

__version__ = "0.3.1"


def _safe_streams() -> None:
    """非中文 Windows（cp1252/437）下 print 中文或 → 会直接抛 UnicodeEncodeError。

    这里只把 stdout/stderr 的 errors 改成 replace：不换编码（换了会和控制台代码页打架），
    只是保证永远不会因为一个符号把程序打死。
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


_safe_streams()
