"""최소 WebSocket (RFC 6455) — 표준 라이브러리만.

중계 서버(받는 쪽)와 집 PC 의 커넥터(여는 쪽)가 같이 쓴다. 필요한 것만 있다.
  - 텍스트·바이너리 프레임, 조각(continuation) 합치기, ping/pong, close
  - 클라이언트 → 서버 프레임 마스킹(규격상 필수), 서버 → 클라이언트는 마스킹 없음
  - 확장(permessage-deflate)·서브프로토콜 없음
"""
import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class Closed(Exception):
    pass


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


class WebSocket:
    """연결 하나. send 는 여러 스레드에서 불러도 된다(락). recv 는 한 스레드만."""

    def __init__(self, sock, is_client, max_message=4 * 1024 * 1024, leftover=b""):
        self.sock = sock
        self._buf = bytearray(leftover)  # 핸드셰이크와 같이 읽힌 첫 프레임 조각
        self.is_client = is_client
        self.max_message = max_message
        self._send_lock = threading.Lock()
        self.closed = False

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise Closed("eof")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode, payload):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        mask_bit = 0x80 if self.is_client else 0
        if n < 126:
            head.append(mask_bit | n)
        elif n < 65536:
            head.append(mask_bit | 126); head += struct.pack("!H", n)
        else:
            head.append(mask_bit | 127); head += struct.pack("!Q", n)
        if self.is_client:
            mask = os.urandom(4)
            head += mask
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            if self.closed:
                raise Closed("send after close")
            self.sock.sendall(bytes(head) + payload)

    def send_text(self, text):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self, data=b""):
        self._send_frame(OP_PING, data)

    def close(self, code=1000):
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", code))
        except (OSError, Closed):
            pass
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def recv(self):
        """다음 데이터 메시지(str 또는 bytes). ping 엔 알아서 pong. 닫히면 Closed."""
        parts, first_op = [], None
        while True:
            b0, b1 = self._recv_exact(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            masked, n = b1 & 0x80, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            if n > self.max_message:
                self.close(1009)
                raise Closed("frame too big")
            mask = self._recv_exact(4) if masked else None
            data = self._recv_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == OP_PING:
                self._send_frame(OP_PONG, data)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                self.close()
                raise Closed("peer closed")
            if op in (OP_TEXT, OP_BIN):
                first_op, parts = op, [data]
            elif op == OP_CONT and first_op is not None:
                parts.append(data)
            if sum(map(len, parts)) > self.max_message:
                self.close(1009)
                raise Closed("message too big")
            if fin and first_op is not None:
                msg = b"".join(parts)
                return msg.decode("utf-8") if first_op == OP_TEXT else msg


def server_handshake(handler):
    """BaseHTTPRequestHandler 안에서 업그레이드 응답을 보내고 WebSocket 을 돌려준다. 실패 시 None."""
    h = handler.headers
    if h.get("Upgrade", "").lower() != "websocket" or not h.get("Sec-WebSocket-Key"):
        return None
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(h["Sec-WebSocket-Key"]))
    handler.end_headers()
    handler.wfile.flush()
    handler.close_connection = True
    return WebSocket(handler.connection, is_client=False)


def connect(url, headers=None, timeout=15):
    """ws:// 또는 wss:// 로 연다. headers 에 Authorization 등을 넣는다."""
    u = urllib.parse.urlsplit(url)
    secure = u.scheme == "wss"
    port = u.port or (443 if secure else 80)
    raw = socket.create_connection((u.hostname, port), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if secure else raw
    key = base64.b64encode(os.urandom(16)).decode()
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    lines = [f"GET {path} HTTP/1.1", f"Host: {u.netloc}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise Closed("handshake eof")
        resp += chunk
        if len(resp) > 16384:
            raise Closed("handshake too long")
    raw_head, leftover = resp.split(b"\r\n\r\n", 1)
    head = raw_head.decode("latin-1")
    status = head.split("\r\n", 1)[0]
    if " 101 " not in status + " ":
        raise Closed(f"handshake refused: {status}")
    if accept_key(key) not in head:
        raise Closed("bad accept key")
    sock.settimeout(None)
    return WebSocket(sock, is_client=True, leftover=leftover)
