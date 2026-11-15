"""最小 WebSocket（RFC 6455）实现，只为本地回环用。

为什么自己写：
  环境里没有 websockets / aiohttp，装一个要多几百 KB 依赖；
  这里只需要「服务端广播进度 + 客户端订阅进度」，文本帧足够，
  代码 ~200 行，还能顺便在测试里当客户端用。

支持：握手、文本帧、ping/pong、close、分片（拼接）、掩码（客户端必须带）。
不支持：扩展协商、二进制分片流、permessage-deflate —— 本地进度推送用不到。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


class _Reader:
    """带预读缓冲的字节源 —— 握手响应可能和第一帧挤在同一个 TCP 段里。"""

    def __init__(self, sock: socket.socket, initial: bytes = b""):
        self.sock = sock
        self.buf = initial

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _recv_exact(src, n: int) -> bytes:
    if hasattr(src, "read"):
        return src.read(n)
    if isinstance(src, _Reader):
        return src.read(n)
    buf = b""
    while len(buf) < n:
        chunk = src.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def read_frame(src) -> tuple[int, bytes]:
    b1, b2 = _recv_exact(src, 2)
    fin = b1 & 0x80
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(src, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(src, 8))[0]
    mask = _recv_exact(src, 4) if masked else b""
    data = _recv_exact(src, length) if length else b""
    if masked:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    if not fin:
        # 把后续帧拼上（本地场景很少出现，做个兜底）
        op2, data2 = read_frame(src)
        data += data2
        if op2 != OP_CONT:
            opcode = op2
    return opcode, data


def write_frame(sock: socket.socket, opcode: int, data: bytes, *, mask: bool = False) -> None:
    header = bytearray()
    header.append(0x80 | opcode)
    ln = len(data)
    mbit = 0x80 if mask else 0
    if ln < 126:
        header.append(mbit | ln)
    elif ln < (1 << 16):
        header.append(mbit | 126)
        header += struct.pack("!H", ln)
    else:
        header.append(mbit | 127)
        header += struct.pack("!Q", ln)
    if mask:
        key = os.urandom(4)
        header += key
        data = bytes(c ^ key[i % 4] for i, c in enumerate(data))
    sock.sendall(bytes(header) + data)


class Peer:
    """一条连接：收线程 + 发送锁。"""

    def __init__(self, sock: socket.socket, *, mask: bool = False, initial: bytes = b""):
        self.sock = sock
        self.mask = mask
        self.alive = True
        self._lock = threading.Lock()
        self._reader = _Reader(sock, initial)

    def send_json(self, obj: Any) -> bool:
        return self.send_text(json.dumps(obj, ensure_ascii=False))

    def send_text(self, text: str) -> bool:
        if not self.alive:
            return False
        try:
            with self._lock:
                write_frame(self.sock, OP_TEXT, text.encode(), mask=self.mask)
            return True
        except Exception:
            self.alive = False
            return False

    def pump(self, on_message=None, stop: threading.Event | None = None) -> None:
        """读循环：处理 ping/close，把文本消息交给 on_message。"""
        try:
            while self.alive and not (stop and stop.is_set()):
                op, data = read_frame(self._reader)
                if op == OP_TEXT and on_message:
                    try:
                        on_message(json.loads(data.decode("utf-8")))
                    except Exception:
                        pass
                elif op == OP_PING:
                    write_frame(self.sock, OP_PONG, data, mask=self.mask)
                elif op == OP_CLOSE:
                    try:
                        write_frame(self.sock, OP_CLOSE, b"", mask=self.mask)
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        self.alive = False

    def close(self) -> None:
        if not self.alive:
            return
        try:
            with self._lock:
                write_frame(self.sock, OP_CLOSE, b"", mask=self.mask)
        except Exception:
            pass
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass


class Hub:
    """把事件广播给所有订阅者（线程安全）。"""

    def __init__(self) -> None:
        self.peers: list[Peer] = []
        self._lock = threading.Lock()

    def add(self, peer: Peer) -> None:
        with self._lock:
            self.peers.append(peer)

    def drop(self, peer: Peer) -> None:
        with self._lock:
            if peer in self.peers:
                self.peers.remove(peer)

    def broadcast(self, obj: Any) -> None:
        with self._lock:
            peers = list(self.peers)
        dead = []
        for p in peers:
            if not p.send_json(obj):
                dead.append(p)
        for p in dead:
            self.drop(p)

    def count(self) -> int:
        with self._lock:
            return len(self.peers)


class Client:
    """客户端：连 ws://127.0.0.1:port/ws，后台线程收消息。"""

    def __init__(self, url: str, *, timeout: float = 8.0, on_message=None, on_close=None):
        self.url = url
        self.timeout = timeout
        self.on_message = on_message
        self.on_close = on_close
        self.peer: Peer | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def connect(self) -> bool:
        u = self.url
        if u.startswith("ws://"):
            u = u[5:]
        if "/" in u:
            hostport, path = u.split("/", 1)
            path = "/" + path
        else:
            hostport, path = u, "/ws"
        host, _, port = hostport.partition(":")
        port = int(port or 80)
        sock = socket.create_connection((host, port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise ConnectionError("handshake failed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.split(b"\r\n", 1)[0].decode("latin1"))
        sock.settimeout(None)
        # 握手响应后面的字节可能是第一帧（hello），不能丢
        _, _, leftover = head.partition(b"\r\n\r\n")
        self.peer = Peer(sock, mask=True, initial=leftover)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        assert self.peer
        self.peer.pump(self.on_message, self._stop)
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass

    def close(self) -> None:
        self._stop.set()
        if self.peer:
            self.peer.close()

    def wait(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and self.peer and self.peer.alive:
            time.sleep(0.05)
