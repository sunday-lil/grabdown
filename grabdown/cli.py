"""grabdown CLI。

    grabdown probe URL                       # 看能不能分段、多大、什么类型
    grabdown list  URL                       # 列出可选清晰度
    grabdown get   URL [-q 1080]             # 提取 + 下载（HLS 自动合流 mp4）
    grabdown dl    URL [-o FILE]             # 纯文件分段下载（IDM 行为）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .engine import download
from .extract import MediaInfo, resolve
from .hls import download_hls
from .http import DEFAULT_UA, GrabError, Session
from .http import guess_filename as _guess
from .sites import *  # noqa: F401,F403  注册提取器


def _session(args) -> Session:
    h: dict[str, str] = {}
    if args.referer:
        h["Referer"] = args.referer
    if args.cookie:
        h["Cookie"] = args.cookie
    return Session(headers=h, timeout=args.timeout, retries=args.retries,
                   proxy=args.proxy, ua=args.ua or DEFAULT_UA)


def _fmt_table(info: MediaInfo) -> str:
    rows = [("kind", "quality", "height", "bandwidth", "url")]
    for f in sorted(info.formats, key=lambda x: x.sort_key):
        rows.append((f.kind, f.quality, str(f.height or ""),
                     f"{f.bandwidth // 1000}k" if f.bandwidth else "", f.url))
    w = [max(len(r[i]) for r in rows) for i in range(4)]
    out = []
    for i, r in enumerate(rows):
        line = "  ".join(f"{r[j]:<{w[j]}}" for j in range(4)) + "  " + r[4]
        out.append(line)
        if i == 0:
            out.append("-" * min(len(line), 110))
    return "\n".join(out)


def cmd_probe(args) -> int:
    s = _session(args)
    p = s.probe(args.url)
    print(f"url      : {p.url}")
    print(f"status   : {p.status}  {p.mime}")
    print(f"size     : {p.size if p.size is not None else '未知'}"
          + (f"  ({p.size/1048576:.2f} MB)" if p.size else ""))
    print(f"ranges   : {'支持（可多线程/续传）' if p.accept_ranges else '不支持（只能单流）'}")
    print(f"filename : {p.filename}")
    if p.etag:
        print(f"etag     : {p.etag}")
    return 0


def cmd_list(args) -> int:
    s = _session(args)
    ext, info = resolve(s, args.url)
    if args.json:
        print(json.dumps({"site": info.site, "title": info.title, "page": info.page_url,
                          "note": info.note,
                          "formats": [f.__dict__ for f in info.formats]}, ensure_ascii=False, indent=2))
        return 0
    print(f"站点 : {info.site}")
    print(f"标题 : {info.title}")
    if info.note:
        print(f"备注 : {info.note}")
    print()
    print(_fmt_table(info))
    return 0


def _target_path(args, info: MediaInfo, fmt) -> Path:
    out = Path(args.out or ".")
    if out.suffix and not args.out_is_dir:
        return out
    stem = info.title if args.keep_title else _guess(fmt.url, None, "media")
    stem = os.path.splitext(stem)[0] if stem.lower().endswith((".m3u8", ".mp4", ".ts")) else stem
    q = fmt.height or fmt.quality or "src"
    ext = ".mp4" if fmt.kind in ("hls", "mp4") else ".bin"
    return out / f"{stem}_{q}{ext}"


def cmd_get(args) -> int:
    s = _session(args)
    ext, info = resolve(s, args.url)
    fmt = info.pick(args.quality)
    print(f"[{info.site}] {info.title}")
    print(f"  → {fmt.kind} {fmt.quality} {fmt.url[:90]}{'…' if len(fmt.url) > 90 else ''}")
    if args.dry_run:
        print(json.dumps({"title": info.title, "kind": fmt.kind, "url": fmt.url,
                          "headers": {**info.headers, **({"Referer": args.referer} if args.referer else {})}},
                         ensure_ascii=False, indent=2))
        return 0

    dest = _target_path(args, info, fmt)
    if fmt.kind == "hls" and args.no_remux:
        dest = dest.with_suffix(".ts")
    headers = dict(info.headers)
    if args.referer:
        headers["Referer"] = args.referer
    if fmt.kind == "ytdlp":
        from . import ytdlp_backend as ytb

        out = ytb.download(fmt.url, dest.parent, quality=args.quality,
                           cookie=headers.get("Cookie"), referer=headers.get("Referer"),
                           remux=not args.no_remux)
    elif fmt.kind == "hls":
        out = download_hls(s, fmt.url, dest, quality=args.quality, threads=args.threads,
                           headers=headers, remux=not args.no_remux, show_progress=not args.quiet)
    else:
        out = download(s, fmt.url, dest, threads=args.threads, headers=headers,
                       resume=not args.no_resume, show_progress=not args.quiet)
    size = os.path.getsize(out) if os.path.exists(out) else 0
    print(f"\n完成: {out}  ({size/1048576:.2f} MB)")
    return 0


def cmd_dl(args) -> int:
    s = _session(args)
    p = s.probe(args.url)
    dest = Path(args.out) if args.out else Path(p.filename)
    print(f"{'分段' if p.accept_ranges and p.size else '单流'}下载 → {dest}")
    out = download(s, args.url, dest, threads=args.threads, resume=not args.no_resume,
                   show_progress=not args.quiet)
    size = os.path.getsize(out) if os.path.exists(out) else 0
    print(f"完成: {out}  ({size/1048576:.2f} MB)")
    return 0


def cmd_agent(args) -> int:
    from . import agent as agent_mod

    if getattr(args, "stop", False):
        ok = agent_mod.stop(args.port)
        print("已停止 agent" if ok else f"没有在跑的 agent（{args.port}）")
        return 0 if ok else 1
    return agent_mod.main(port=args.port, out_dir=args.out, token=args.token,
                          verbose=args.verbose, workers=args.workers)


def cmd_doctor(args) -> int:
    from . import tools
    from .agent import DEFAULT_PORT

    from . import ytdlp_backend as ytb

    info = tools.diagnose()
    print(f"grabdown {__version__}")
    print(f"  python      : {info['python']}  ({info['platform']})")
    print(f"  executable  : {info['executable']}")
    y = ytb.diagnose()
    print(f"  yt-dlp      : {('v' + y['version']) if y['available'] else '未安装（主流平台解析需要它：' + y['hint'] + '）'}")
    print(f"  ffmpeg      : {info['ffmpeg'] or '未找到'}")
    if info["ffmpeg_version"]:
        print(f"  ffmpeg ver  : {info['ffmpeg_version']}")
    if info["ffmpeg_hint"]:
        print(f"  ! {info['ffmpeg_hint']}")
    import urllib.request

    base = f"http://127.0.0.1:{getattr(args, 'port', DEFAULT_PORT)}"
    try:
        with urllib.request.urlopen(base + "/ping", timeout=2) as r:
            data = json.loads(r.read() or b"{}")
        print(f"  agent       : 在线 {base}  v{data.get('version', '?')}")
    except Exception:
        print(f"  agent       : 离线（{base}）→ 运行 python -m grabdown agent")
    print(f"  下载目录    : {os.path.abspath(args.out)}")
    return 0


def cmd_platforms(args) -> int:
    from . import platforms

    rows = platforms.describe()
    w = max(len(r["name"]) for r in rows)
    print(f"{'平台':<{w}}  {'引擎':<12}{'域名'}")
    print("-" * 78)
    for r in rows:
        print(f"{r['name']:<{w}}  {r['engine']:<12}{r['hosts']}")
    y = __import__("grabdown.ytdlp_backend", fromlist=["diagnose"]).diagnose()
    print()
    print("yt-dlp:", ("v" + y["version"]) if y["available"] else "未安装 → " + y["hint"])
    print("native：PornHub、蓝奏式直链；unsupported：Netflix/Spotify 等 DRM 站点")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grabdown",
                                description="IDM 式多线程下载器 + 网页媒体提取（HLS/mp4）")
    p.add_argument("--version", action="version", version=f"grabdown {__version__}")

    def common(sp):
        sp.add_argument("--ua", help="覆盖 User-Agent")
        sp.add_argument("--referer", help="Referer（很多 CDN 校验）")
        sp.add_argument("--cookie", help="Cookie 串")
        sp.add_argument("--proxy", help="http://host:port")
        sp.add_argument("--timeout", type=float, default=30.0)
        sp.add_argument("--retries", type=int, default=3)
        sp.add_argument("--quiet", action="store_true")

    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("probe", help="探测 URL（大小/Range/类型）")
    pr.add_argument("url")
    common(pr)
    pr.set_defaults(func=cmd_probe)

    ls = sub.add_parser("list", help="列出可选清晰度")
    ls.add_argument("url")
    ls.add_argument("--json", action="store_true")
    common(ls)
    ls.set_defaults(func=cmd_list)

    g = sub.add_parser("get", help="提取并下载（HLS 自动合流）")
    g.add_argument("url")
    g.add_argument("-q", "--quality", default="best", help="best/1080/720/worst…")
    g.add_argument("-t", "--threads", type=int, default=8)
    g.add_argument("-o", "--out", help="输出目录或文件")
    g.add_argument("--out-is-dir", action="store_true")
    g.add_argument("--keep-title", action="store_true", default=True)
    g.add_argument("--no-remux", action="store_true", help="HLS 不调用 ffmpeg，保留 .ts")
    g.add_argument("--no-resume", action="store_true")
    g.add_argument("--dry-run", action="store_true", help="只打印最终直链")
    common(g)
    g.set_defaults(func=cmd_get)

    pf = sub.add_parser("platforms", help="列出支持的平台与对应引擎")
    pf.set_defaults(func=cmd_platforms)

    doc = sub.add_parser("doctor", help="自检：ffmpeg / yt-dlp / agent / 下载目录")
    doc.add_argument("--port", type=int, default=8899)
    doc.add_argument("--out", default="downloads")
    doc.set_defaults(func=cmd_doctor)

    ag = sub.add_parser("agent", help="起本地下载代理（Chrome 扩展用）")
    ag.add_argument("--port", type=int, default=8899)
    ag.add_argument("--out", default="downloads", help="下载目录")
    ag.add_argument("--token", help="可选：要求扩展带 X-Grab-Token")
    ag.add_argument("--workers", type=int, default=2, help="同时下载的任务数")
    ag.add_argument("--verbose", action="store_true")
    ag.add_argument("--stop", action="store_true", help="停掉正在跑的 agent")
    ag.set_defaults(func=cmd_agent)

    d = sub.add_parser("dl", help="纯文件分段下载")
    d.add_argument("url")
    d.add_argument("-t", "--threads", type=int, default=8)
    d.add_argument("-o", "--out")
    d.add_argument("--no-resume", action="store_true")
    common(d)
    d.set_defaults(func=cmd_dl)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = build_parser()
    if not argv:
        p.print_help()
        return 2
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    try:
        return args.func(args)
    except GrabError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
