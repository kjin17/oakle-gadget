#!/usr/bin/env python3
"""oakle 중계 서버 (인터넷의 작은 VM) — 기기 요청을 집 PC 의 커넥터에 넘기고 답을 돌려준다.

여기에 있는 것: 기기 토큰 해시, 커넥터 토큰 해시, 한도 계수, 본문 없는 접근 기록.
여기에 없는 것: OpenClaw Gateway 토큰, 모델 키, 기기 서명 키, 에이전트. (docs/relay-protocol.md)

  기기  ──POST /v1/turn (JSON) · /v1/ask (WAV)──▶  [이 서버]  ◀──WS /v1/connect── 집 PC 커넥터(밖으로 연다)
  GET /healthz → 200 "ok" + X-Connector: up|down

/v1/turn 의 type=memory_note 는 「기억해 줘」 메모(500자 이하). 커넥터가 승인 대기열 파일에 덧붙이고 끝낸다.
중계는 type 만 들여다본다(하루 상한을 따로 세려고).
본문은 서명째 그대로 넘기고 로그엔 남기지 않는다. 하루 메모 상한은 기기당 MEMO_DAILY_LIMIT(50), 질의 한도와 따로 센다.

오류 본문은 JSON {"error": 코드, "fallback": bool}. fallback=true 면 클라이언트가 자체 예비 경로로 넘어가도 된다는 뜻이다.
  401 auth · 400 bad_request · 413 size · 429 limit|rate · 503 connector_offline|connector_lost(fallback)
  504 timeout(fallback) · 그 밖의 상태·본문은 커넥터가 정한 값을 그대로(서명 포함) 넘긴다

비밀값은 코드에 적지 않는다. 환경변수 → 없으면 CRED_DIR 파일. 표준 라이브러리만.
"""
import base64
import hashlib
import hmac
import itertools
import json
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path

try:
    from http.server import ThreadingHTTPServer
except ImportError:  # python 3.6 호환 (ThreadingHTTPServer 는 3.7부터)
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wsmin  # noqa: E402

CRED_DIR = Path(os.environ.get("CRED_DIR", str(Path.home() / ".openclaw" / "credentials")))
STATE_DIR = Path(os.environ.get("STATE_DIR", str(Path.home() / ".local" / "share" / "oakle-relay")))
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "200"))        # 기기당 하루 성공 요청(도구 왕복 포함)
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "20"))       # 기기당 분당 시도
MAX_TURN_BYTES = int(os.environ.get("MAX_TURN_BYTES", str(64 * 1024)))
MAX_MEMO_BYTES = int(os.environ.get("MAX_MEMO_BYTES", str(4 * 1024)))   # 500자 메모 + 머리
MEMO_DAILY_LIMIT = int(os.environ.get("MEMO_DAILY_LIMIT", "50"))      # 기기당 하루 메모(성공만)
MEMO_TIMEOUT = float(os.environ.get("MEMO_TIMEOUT", "15"))
MAX_ASK_BYTES = int(os.environ.get("MAX_ASK_BYTES", str(1024 * 1024)))   # 15초 WAV ≈ 480KB
MAX_RESP_BYTES = int(os.environ.get("MAX_RESP_BYTES", str(3 * 1024 * 1024)))
TURN_TIMEOUT = float(os.environ.get("TURN_TIMEOUT", "120"))
ASK_TIMEOUT = float(os.environ.get("ASK_TIMEOUT", "150"))
CLOCK_SKEW = int(os.environ.get("CLOCK_SKEW", "300"))          # 커넥터가 ±120초로 다시 엄하게 본다
PING_EVERY = float(os.environ.get("PING_EVERY", "20"))


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def read_secret(env_name, cred_file):
    v = os.environ.get(env_name)
    if v:
        return v.strip()
    try:
        return (CRED_DIR / cred_file).read_text().strip()
    except OSError:
        log(f"secret missing: {env_name} / {cred_file}")
        return None


def sha256hex(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------- 기기 등록부 / 한도

class Registry:
    """기기 auth 값의 sha256 만 둔다(평문 토큰·서명 키 없음). 폐기 = revoked=1."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices(
              device_id TEXT PRIMARY KEY, auth_sha256 TEXT NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage(
              device_id TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(device_id, day));
            CREATE TABLE IF NOT EXISTS memo_usage(
              device_id TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(device_id, day));
            CREATE TABLE IF NOT EXISTS requests(
              ts TEXT NOT NULL, device_id TEXT, path TEXT, status INTEGER, code TEXT,
              in_bytes INTEGER, out_bytes INTEGER, ms INTEGER);
            """
        )
        self.recent = defaultdict(deque)  # device → 최근 1분 시도 시각

    def check(self, device_id, auth):
        with self.lock:
            row = self.db.execute(
                "SELECT auth_sha256, revoked FROM devices WHERE device_id=?", (device_id,)
            ).fetchone()
        return bool(row) and row[1] == 0 and hmac.compare_digest(row[0], sha256hex(auth))

    def rate_ok(self, device_id):
        now = time.monotonic()
        with self.lock:
            q = self.recent[device_id]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                return False
            q.append(now)
            return True

    def used_today(self, device_id, table="usage"):
        with self.lock:
            row = self.db.execute(f"SELECT count FROM {table} WHERE device_id=? AND day=?",
                                  (device_id, time.strftime("%Y-%m-%d"))).fetchone()
        return row[0] if row else 0

    def consume(self, device_id, table="usage"):
        """성공한 요청만 센다(커넥터가 꺼져 실패한 요청이 한도를 깎지 않도록). table: usage | memo_usage"""
        with self.lock:
            self.db.execute(
                f"INSERT INTO {table}(device_id, day, count) VALUES(?,?,1) "
                "ON CONFLICT(device_id, day) DO UPDATE SET count=count+1",
                (device_id, time.strftime("%Y-%m-%d")))
            self.db.commit()

    def record(self, **kw):
        cols = ",".join(kw)
        with self.lock:
            self.db.execute(f"INSERT INTO requests(ts,{cols}) VALUES(?{',?' * len(kw)})",
                            (time.strftime("%Y-%m-%dT%H:%M:%S"), *kw.values()))
            self.db.commit()


# ---------------------------------------------------------------- 커넥터 링크

class Link:
    """커넥터 WebSocket 1개. 새로 붙으면 옛 연결을 닫는다."""

    def __init__(self):
        self.ws = None
        self.lock = threading.Lock()
        self.pending = {}  # id → [Event, 응답 dict | None]
        self.ids = itertools.count(1)
        self.since = None

    def up(self):
        return self.ws is not None and not self.ws.closed

    def attach(self, ws):
        with self.lock:
            old, self.ws, self.since = self.ws, ws, time.time()
        if old:
            old.close(4000)
        self._fail_pending("connector_lost")

    def detach(self, ws):
        with self.lock:
            if self.ws is not ws:
                return
            self.ws = None
        self._fail_pending("connector_lost")

    def _fail_pending(self, code):
        with self.lock:
            items = list(self.pending.items())
            self.pending.clear()
        for _, slot in items:
            slot[1] = {"_relay_error": code}
            slot[0].set()

    def call(self, frame, timeout):
        """커넥터에 요청을 보내고 답을 기다린다. (응답 dict | None, 오류 코드)"""
        ws = self.ws
        if ws is None or ws.closed:
            return None, "connector_offline"
        rid = str(next(self.ids))
        frame["id"] = rid
        slot = [threading.Event(), None]
        with self.lock:
            self.pending[rid] = slot
        try:
            ws.send_text(json.dumps(frame))
        except (OSError, wsmin.Closed):
            with self.lock:
                self.pending.pop(rid, None)
            return None, "connector_lost"
        if not slot[0].wait(timeout):
            with self.lock:
                self.pending.pop(rid, None)
            try:  # 커넥터가 하던 일을 그만두게 알린다
                ws.send_text(json.dumps({"t": "cancel", "id": rid}))
            except (OSError, wsmin.Closed):
                pass
            return None, "timeout"
        resp = slot[1]
        if "_relay_error" in resp:
            return None, resp["_relay_error"]
        return resp, ""

    def deliver(self, msg):
        with self.lock:
            slot = self.pending.pop(str(msg.get("id")), None)
        if slot:
            slot[1] = msg
            slot[0].set()


REG = None
LINK = Link()
CONNECTOR_SHA256 = None


def pinger():
    while True:
        time.sleep(PING_EVERY)
        ws = LINK.ws
        if ws is not None:
            try:
                ws.ping()
            except (OSError, wsmin.Closed):
                LINK.detach(ws)


# ---------------------------------------------------------------- HTTP

def is_memo(body):
    """type 만 본다. 본문은 그대로 넘기고 어디에도 적지 않는다."""
    try:
        return json.loads(body.decode("utf-8")).get("type") == "memory_note"
    except (ValueError, AttributeError):
        return False


ERR_STATUS = {"connector_offline": 503, "connector_lost": 503, "timeout": 504}


class Handler(BaseHTTPRequestHandler):
    server_version = "oakle-relay/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 기본 접근 로그(경로·IP)는 끄고 record() 로 남긴다
        pass

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, err, fallback=False):
        self._send(code, json.dumps({"error": err, "fallback": fallback}).encode())

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok", "text/plain", {"X-Connector": "up" if LINK.up() else "down"})
        if self.path == "/v1/connect":
            return self._connect()
        self._err(404, "not_found")

    def _connect(self):
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        if not CONNECTOR_SHA256 or not tok or not hmac.compare_digest(sha256hex(tok), CONNECTOR_SHA256):
            log("connector auth fail")
            self.close_connection = True
            return self._err(401, "auth")
        ws = wsmin.server_handshake(self)
        if ws is None:
            return self._err(400, "not_websocket")
        # 수신·송신 시한. 없으면 커넥터가 멈췄을 때(SIGSTOP 등) recv 가 영원히 막혀 healthz 가 up 으로
        # 남고 요청은 504 로 매달리며, 송신 버퍼가 차면 sendall 도 막힌다. ping 3번 동안 아무것도 없으면 끊는다.
        ws.sock.settimeout(3 * PING_EVERY)
        LINK.attach(ws)
        log("connector up")
        try:
            while True:
                raw = ws.recv()
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if msg.get("t") == "resp":
                    LINK.deliver(msg)
        except (OSError, wsmin.Closed) as e:
            log(f"connector down: {type(e).__name__}")
        finally:
            LINK.detach(ws)
            ws.close()

    def do_POST(self):
        t0 = time.monotonic()
        path = self.path
        # /v1/ 은 API 경로 버전이다 — 서명에 경로가 들어가니 바꾸지 말 것
        if path not in ("/v1/turn", "/v1/ask"):
            return self._err(404, "not_found")
        device_id = self.headers.get("X-Device-Id", "")[:64]
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        rec = dict(device_id=device_id, path=path)
        if not device_id or not REG.check(device_id, tok):
            REG.record(**rec, status=401, code="auth")
            self.close_connection = True
            return self._err(401, "auth")
        if not REG.rate_ok(device_id):
            REG.record(**rec, status=429, code="rate")
            return self._err(429, "rate")
        cap = MAX_TURN_BYTES if path == "/v1/turn" else MAX_ASK_BYTES
        try:
            n = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            n = -1
        if n <= 0 or n > cap:
            REG.record(**rec, status=413, code="size", in_bytes=max(n, 0))
            self.close_connection = True
            return self._err(413, "size")
        body = self.rfile.read(n)
        memo = path == "/v1/turn" and is_memo(body)
        table = "memo_usage" if memo else "usage"
        if memo and n > MAX_MEMO_BYTES:
            REG.record(**rec, status=413, code="size", in_bytes=n)
            return self._err(413, "size")
        if REG.used_today(device_id, table) >= (MEMO_DAILY_LIMIT if memo else DAILY_LIMIT):
            REG.record(**rec, status=429, code="memo_limit" if memo else "limit", in_bytes=n)
            return self._err(429, "limit", fallback=not memo)  # 메모는 예비 두뇌로 갈 일이 아니다(클라이언트가 나중에 다시 보낸다)
        try:
            ts = int(self.headers.get("X-Oakle-Ts", "0"))
        except ValueError:
            ts = 0
        nonce = self.headers.get("X-Oakle-Nonce", "")[:64]
        sig = self.headers.get("X-Oakle-Sig", "")[:128]
        if not nonce or not sig or abs(time.time() - ts) > CLOCK_SKEW:
            REG.record(**rec, status=400, code="unsigned", in_bytes=n)
            return self._err(400, "unsigned")
        frame = {"t": "req", "path": path, "device": device_id, "ts": ts, "nonce": nonce, "sig": sig,
                 "ctype": self.headers.get("Content-Type", ""), "body_b64": base64.b64encode(body).decode()}
        resp, err = LINK.call(frame, MEMO_TIMEOUT if memo else TURN_TIMEOUT if path == "/v1/turn" else ASK_TIMEOUT)
        ms = int((time.monotonic() - t0) * 1000)
        if err:
            REG.record(**rec, status=ERR_STATUS[err], code=err, in_bytes=n, ms=ms)
            log(f"{device_id} {path} {err} {ms}ms")
            return self._err(ERR_STATUS[err], err, fallback=True)
        out = base64.b64decode(resp.get("body_b64", ""))[:MAX_RESP_BYTES]
        status = int(resp.get("status", 502))
        headers = {k: v for k, v in (resp.get("headers") or {}).items()
                   if k.startswith("X-") and "\n" not in str(v) and "\r" not in str(v)}
        headers["X-Oakle-Sig"] = str(resp.get("sig", ""))
        headers["X-Relay-Ms"] = str(ms)
        if status == 200:
            REG.consume(device_id, table)
        code = "memo" if memo and status == 200 else str(resp.get("code", ""))[:32]
        REG.record(**rec, status=status, code=code, in_bytes=n, out_bytes=len(out), ms=ms)
        log(f"{device_id} {path}{' memo' if memo else ''} {status} {ms}ms in={n}B out={len(out)}B")  # 본문은 남기지 않는다
        self._send(status, out, str(resp.get("ctype") or "application/json"), headers)


# ---------------------------------------------------------------- 관리 명령

def admin(argv):
    """python3 relay_server.py add-device <id> <auth_sha256>   (커넥터 connector.py add-device 가 출력한 줄)
       python3 relay_server.py revoke-device <id>
       python3 relay_server.py usage                              (오늘 기기별 사용량)"""
    if argv[0] == "add-device" and len(argv) == 3:
        h = argv[2].strip().lower()
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            sys.exit("auth_sha256 는 64자리 hex 여야 해요 (평문 토큰을 넣지 마세요)")
        REG.db.execute("INSERT OR REPLACE INTO devices(device_id, auth_sha256, revoked, created_at) VALUES(?,?,0,?)",
                       (argv[1], h, time.strftime("%Y-%m-%dT%H:%M:%S")))
        REG.db.commit(); print("added", argv[1])
    elif argv[0] == "revoke-device" and len(argv) == 2:
        REG.db.execute("UPDATE devices SET revoked=1 WHERE device_id=?", (argv[1],))
        REG.db.commit(); print("revoked", argv[1])
    elif argv[0] == "usage":
        for t in ("usage", "memo_usage"):
            for row in REG.db.execute(f"SELECT device_id, count FROM {t} WHERE day=?", (time.strftime("%Y-%m-%d"),)):
                print(t, *row)
    else:
        sys.exit(admin.__doc__)


if __name__ == "__main__":
    REG = Registry(STATE_DIR / "relay.sqlite3")
    if len(sys.argv) > 1:
        admin(sys.argv[1:]); sys.exit()
    CONNECTOR_SHA256 = (read_secret("RELAY_CONNECTOR_SHA256", "oakle_relay_connector_token.sha256") or "").lower()
    if not CONNECTOR_SHA256:
        log("connector hash missing → 커넥터를 받지 않는다 (기기 요청은 모두 503 fallback)")
    host = os.environ.get("BIND", "127.0.0.1")   # nginx 가 TLS 를 끝내고 넘긴다
    port = int(os.environ.get("PORT", "8766"))
    threading.Thread(target=pinger, daemon=True).start()
    log(f"relay bind={host}:{port} limit/day={DAILY_LIMIT} memo/day={MEMO_DAILY_LIMIT} rate/min={RATE_PER_MIN}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
