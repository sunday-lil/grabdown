"""外部工具定位：ffmpeg 之类的可执行文件，别指望 PATH 一定干净。

GUI / pythonw / 计划任务里启动时，PATH 常常被裁掉，`shutil.which` 会失灵。
所以按「显式配置 → PATH → 常见安装路径」的顺序找，并且**真的跑一次 -version** 验证可用，
避免拿到一个坏 exe 后面合流才炸。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

_WIN_CANDIDATES = [
    r"D:\ffmpeg\bin\ffmpeg.exe",
    r"D:\tools\ffmpeg\bin\ffmpeg.exe",
    r"D:\soft\ffmpeg\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
]
_POSIX_CANDIDATES = [
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/snap/bin/ffmpeg",
    "/Applications/ffmpeg",
]


def _works(exe: str) -> bool:
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, timeout=8)
        return r.returncode == 0 and (b"ffmpeg version" in r.stdout or b"ffmpeg version" in r.stderr)
    except Exception:
        return False


@lru_cache(maxsize=1)
def find_ffmpeg(refresh: bool = False) -> str | None:
    """返回可用的 ffmpeg 路径；找不到返回 None。"""
    if refresh:
        find_ffmpeg.cache_clear()

    env = os.environ.get("GRAB_FFMPEG") or os.environ.get("FFMPEG")
    if env and Path(env).exists() and _works(env):
        return env

    hit = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if hit and _works(hit):
        return hit

    home_bin = Path.home() / ".grabdown" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if home_bin.exists() and _works(str(home_bin)):
        return str(home_bin)

    for cand in (_WIN_CANDIDATES if os.name == "nt" else _POSIX_CANDIDATES):
        if Path(cand).exists() and _works(cand):
            return cand
    return None


def ffmpeg_version(exe: str | None = None) -> str:
    path = exe or find_ffmpeg()
    if not path:
        return ""
    try:
        r = subprocess.run([path, "-version"], capture_output=True, timeout=8)
        first = (r.stdout or r.stderr).decode("utf-8", "replace").splitlines()
        return first[0].strip() if first else ""
    except Exception:
        return ""


def diagnose() -> dict[str, object]:
    """给 `grabdown doctor` 用的体检结果。"""
    exe = find_ffmpeg()
    try:
        import cryptography

        crypto = getattr(cryptography, "__version__", "ok")
    except Exception:
        crypto = ""

    return {
        "crypto": crypto,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": sys.platform,
        "ffmpeg": exe or "",
        "ffmpeg_version": ffmpeg_version(exe),
        "ffmpeg_hint": "" if exe else (
            "装一个静态包把 ffmpeg.exe 放到 D:\\ffmpeg\\bin\\，"
            "或设环境变量 GRAB_FFMPEG 指向它；没有也能下，只是 HLS 会保留 .ts"
        ),
    }
