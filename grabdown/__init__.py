"""grabdown — IDM 风格的多线程下载器 + 网页媒体提取（HLS / mp4）。

设计目标：
  1. 分段并发 + 断点续传（Range），像 IDM 那样把单文件切成 N 段；
  2. 网页 → 媒体地址（HLS/mp4）的提取层，插件式；
  3. HLS 支持 master 选码率、AES-128 解密、分片并发、ffmpeg 无重编码合流。

不做：DRM（Widevine/PlayReady）绕过、付费墙绕过、登录态盗用。
"""

__version__ = "0.3.0"
