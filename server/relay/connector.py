#!/usr/bin/env python3
"""커넥터 — 집 PC 에서 돈다. 중계 서버에 밖으로 WebSocket 을 열어 두고, 받은 질의를 OpenClaw 에이전트에 넣어 답을 돌려준다.

집 포트를 열지 않는다. Gateway 토큰·기기 서명 키는 이 PC 밖으로 나가지 않는다.

두뇌(BRAIN)
  cli  (기본) `openclaw agent --agent <OAKLE_AGENT> --session-key agent:<에이전트>:relay-<기기> --json`.
       OAKLE_AGENT 기본은 device — 도구를 설정에서 막은 기기 전용 에이전트를 따로 만들어 두는 것을 권한다(README).
       에이전트는 기기 도구를 직접 못 부르므로, 지시문으로 「[기기도구 {...}]」 한 줄을 쓰게 하고 그걸 도구 호출로 바꾼다.
  http Gateway /v1/chat/completions + 함수 도구(정식 tool_calls). gateway.http.endpoints.chatCompletions 를 켜야 한다.
  echo 시험용. 에이전트를 부르지 않는다. 「도구:이름」 이 들어간 질의엔 그 도구를 한 번 부른다.

기기 규약(POST /v1/turn, JSON — 텍스트 클라이언트용) — 자세한 건 docs/relay-protocol.md
  → {"type":"ask","text":"…","context":{"now":"…","memory":"…"},"tools":[{"name","description","input_schema"}]}
  → {"type":"tool_results","turn":"t-…","results":[{"id","content","is_error"}]}
  ← 200 {"type":"final","turn","text"} | {"type":"tool_calls","turn","text","calls":[{"id","name","input"}]}
  ← 400 bad_request · 401 sig · 409 turn_unknown · 502 brain(fallback)
  → {"type":"memory_note","id","text"(≤500자),"ts","device_id"} 「기억해 줘」 메모 — 에이전트를 부르지 않는다.
  서명 확인 뒤 이 코드가 직접 MEMORY_INBOX(jsonl)에 status=pending 한 줄을 덧붙인다. 같은 id 는 무시(200 duplicate).
  ← 200 {"type":"memory_ack","id","status":"queued"|"duplicate"} · 400 bad_request
  대기열을 읽어 반영하는 일은 이 리포 밖(사람 승인 등)에 둔다.
POST /v1/ask (WAV, StickS3 바깥 모드) 는 voice_bridge 의 STT·TTS(whisper·say)만 빌리고, 두뇌는 위 brain 을 쓴다.

비밀값: 환경변수 → CRED_DIR(기본 ~/.openclaw/credentials/)
  oakle_relay_connector_token   이 커넥터가 중계 서버에 붙을 때 쓰는 토큰(평문). 중계 서버엔 sha256 만 둔다
  oakle_relay_device_keys.json  {기기: 서명키 hex}. add-device 가 쓴다
  openclaw_gateway_token        BRAIN=http 일 때만
"""
import base64
import collections
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import relaysig  # noqa: E402
import wsmin  # noqa: E402

CRED_DIR = Path(os.environ.get("CRED_DIR", str(Path.home() / ".openclaw" / "credentials")))
RELAY_URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8766/v1/connect")
BRAIN = os.environ.get("BRAIN", "cli")
AGENT = os.environ.get("OAKLE_AGENT", "device")              # 기본도 device — 변수가 빠져도 main 으로 새지 않게
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "openclaw")    # LaunchAgent 에선 절대경로로 준다
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "110"))      # 중계 TURN_TIMEOUT(120) 보다 짧게
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "6"))
CLOCK_SKEW = int(os.environ.get("CLOCK_SKEW", "120"))
LOG_TEXT = os.environ.get("LOG_TEXT", "0") == "1"
KEYS_FILE = os.environ.get("DEVICE_KEYS_FILE", "oakle_relay_device_keys.json")
MEMORY_INBOX = Path(os.environ.get("MEMORY_INBOX", str(Path.home() / ".local" / "share" / "oakle-gadget" / "memory_inbox.jsonl")))
MEMO_MAX_CHARS = 500


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


def device_keys():
    """매 요청마다 다시 읽는다 — 기기 추가·삭제가 재시작 없이 먹는다."""
    try:
        return {k: bytes.fromhex(v) for k, v in json.loads((CRED_DIR / KEYS_FILE).read_text()).items()}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- 지시문

DEVICE_NOTE = {
    "sticks3": "StickS3 음성 리모컨",
}


def device_kind(device_id):
    return next((v for k, v in DEVICE_NOTE.items() if device_id.startswith(k)), "oakle 기기")


def ask_prefix(device_id, ctx):
    lines = [
        f"[{device_kind(device_id)}에서 중계 서버를 거쳐 온 말입니다. 답은 기기 화면에 뜨고 소리로 읽힙니다.",
        "해요체 한두 문장, 마크다운·목록·링크 없이 답하세요.",
        "기기 요청에는 이 컴퓨터의 파일·셸·외부 전송·설정 변경 도구를 쓰지 말고, 답과 기기 도구 표지만 내세요. 송금·삭제·게시·발송처럼 그런 일이 필요하면 다른 채널에서 확인받겠다고만 답하세요.",
        "답 끝에 [표정:이름] 하나(joy·sad·angry·love·surprised·shy·gloomy·lol·ok·no·thanks·sorry·congrats·fighting·hello·eat·sleepy·curious·yes·mindblown).]",
    ]
    if ctx.get("now"):
        lines.append(f"[기기 시각: {str(ctx['now'])[:40]}]")
    if ctx.get("memory"):
        lines.append("[기기가 기억하는 것:\n" + str(ctx["memory"])[:2000] + "]")
    return "\n".join(lines) + "\n"


def tool_protocol(tools):
    """CLI 두뇌용: 기기 도구를 글로 설명하고 한 줄 표지로 부르게 한다."""
    if not tools:
        return ""
    rows = []
    for t in tools[:20]:
        schema = json.dumps(t.get("input_schema", {}), ensure_ascii=False, separators=(",", ":"))
        rows.append(f"- {t.get('name')}: {str(t.get('description', ''))[:200]} 인자={schema[:400]}")
    return (
        "[기기 도구 — 기기 안에서만 실행됩니다. 쓰려면 답 대신 줄마다 하나씩 정확히 이 꼴로만 쓰고 멈추세요:\n"
        '[기기도구 {"name":"도구이름","input":{...}}]\n'
        "결과는 다음 메시지 「[기기도구 결과]」로 옵니다.\n"
        + "\n".join(rows) + "]\n"
    )


TOOL_TAG = re.compile(r"\[기기도구\s+(\{.*\})\]\s*$", re.M)


def parse_tool_tags(text, allowed):
    """「[기기도구 {...}]」 줄을 떼어 도구 호출로 바꾼다. 목록에 없는 이름은 버린다(기기도 다시 거른다)."""
    calls = []
    for m in TOOL_TAG.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        name = obj.get("name")
        if name in allowed:
            calls.append({"id": "c-" + secrets.token_hex(6), "name": name, "input": obj.get("input") or {}})
    rest = TOOL_TAG.sub("", text).strip()
    return calls, rest


# ---------------------------------------------------------------- 두뇌

class CliBrain:
    """openclaw agent CLI. 에이전트 세션이 기억을 이어 가므로 도구 결과만 다음 메시지로 넣는다."""

    def _run(self, device_id, message):
        r = subprocess.run(
            [OPENCLAW_BIN, "agent", "--agent", AGENT, "--session-key", f"agent:{AGENT}:relay-{device_id}",
             "--message", message, "--json", "--timeout", str(LLM_TIMEOUT)],
            capture_output=True, text=True, timeout=LLM_TIMEOUT + 15,
        )
        if r.returncode != 0:
            raise RuntimeError(f"openclaw agent exit {r.returncode}")
        data = json.loads(r.stdout)
        if data.get("status") != "ok":
            raise RuntimeError(f"openclaw agent status {data.get('status')}")
        res = data.get("result") or {}
        # openclaw 2026.9.x 출력: result.payloads[].text, result.meta.finalAssistantVisibleText
        text = (res.get("meta") or {}).get("finalAssistantVisibleText")
        if not text:
            text = "\n".join(p.get("text") or "" for p in res.get("payloads") or []).strip()
        return text or ""

    def start(self, device_id, text, ctx, tools, state):
        state["tools"] = {t.get("name") for t in tools}
        return self._step(device_id, ask_prefix(device_id, ctx) + tool_protocol(tools) + text, state)

    def resume(self, device_id, results, state):
        lines = ["[기기도구 결과]"]
        for r in results:
            name = state["names"].get(r.get("id"), "?")
            flag = " (오류)" if r.get("is_error") else ""
            lines.append(f"- {name}{flag}: {str(r.get('content', ''))[:1500]}")
        return self._step(device_id, "\n".join(lines), state)

    def _step(self, device_id, message, state):
        answer = self._run(device_id, message)
        calls, rest = parse_tool_tags(answer, state["tools"])
        state["names"] = {c["id"]: c["name"] for c in calls}
        return rest, calls


class HttpBrain:
    """Gateway /v1/chat/completions (정식 함수 도구). 턴 안의 메시지만 들고 다니고, 대화는 user 로 이어진다.
    실호출 시험은 아직 못 했다(echo·cli 만 시험)."""

    def __init__(self):
        self.url = os.environ.get("OPENCLAW_URL", "http://127.0.0.1:18789") + "/v1/chat/completions"

    def _call(self, device_id, messages, tools):
        token = read_secret("OPENCLAW_GATEWAY_TOKEN", "openclaw_gateway_token")
        if not token:
            raise RuntimeError("gateway token missing")
        body = {"model": f"openclaw/{AGENT}", "user": f"relay:{device_id}", "messages": messages}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}} for t in tools]
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            if "json" not in r.headers.get("Content-Type", ""):
                raise RuntimeError("non-json response")
            return json.loads(r.read())["choices"][0]

    def start(self, device_id, text, ctx, tools, state):
        state["tools"] = tools
        state["messages"] = [{"role": "user", "content": ask_prefix(device_id, ctx) + text}]
        return self._step(device_id, state)

    def resume(self, device_id, results, state):
        for r in results:
            state["messages"].append({"role": "tool", "tool_call_id": r.get("id"),
                                      "content": str(r.get("content", ""))[:4000]})
        return self._step(device_id, state)

    def _step(self, device_id, state):
        choice = self._call(device_id, state["messages"], state["tools"])
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append({"id": tc.get("id"), "name": fn.get("name"), "input": args})
        if calls:
            state["messages"].append({"role": "assistant", "content": msg.get("content") or "",
                                      "tool_calls": msg.get("tool_calls")})
        return msg.get("content") or "", calls


class EchoBrain:
    """시험용 — 에이전트를 부르지 않는다."""

    def start(self, device_id, text, ctx, tools, state):
        m = re.search(r"도구:(\w+)", text)
        names = {t.get("name") for t in tools}
        if m and m.group(1) in names:
            return "잠깐만요.", [{"id": "c-" + secrets.token_hex(6), "name": m.group(1), "input": {}}]
        if "느리게" in text:
            time.sleep(float(os.environ.get("ECHO_SLOW", "5")))
        return f"메아리: {text} [표정:ok]", []

    def resume(self, device_id, results, state):
        return f"도구 결과 {len(results)}건 받았어요: {results[0].get('content', '')} [표정:joy]", []


BRAINS = {"cli": CliBrain, "http": HttpBrain, "echo": EchoBrain}


# ---------------------------------------------------------------- 요청 처리

class Turns:
    """도구 왕복 중인 턴. 10분 지나면 버린다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.d = {}

    def put(self, tid, device_id, state):
        with self.lock:
            now = time.time()
            for k in [k for k, v in self.d.items() if now - v[2] > 600]:
                del self.d[k]
            self.d[tid] = (device_id, state, now)

    def take(self, tid, device_id):
        with self.lock:
            v = self.d.pop(tid, None)
        return v[1] if v and v[0] == device_id else None


class Nonces:
    def __init__(self, keep=CLOCK_SKEW * 2 + 10):
        self.keep = keep
        self.seen = collections.OrderedDict()
        self.lock = threading.Lock()

    def fresh(self, device_id, nonce):
        k = f"{device_id}:{nonce}"
        now = time.time()
        with self.lock:
            while self.seen and next(iter(self.seen.values())) < now - self.keep:
                self.seen.popitem(last=False)
            if k in self.seen:
                return False
            self.seen[k] = now
            return True


TURNS, NONCES = Turns(), Nonces()
DEVICE_LOCKS = collections.defaultdict(threading.Lock)  # 같은 기기 세션에 에이전트 턴이 겹치지 않게


def j(obj):
    return json.dumps(obj, ensure_ascii=False).encode()


def handle(frame, brain):
    """중계가 넘긴 요청 하나 → (status, ctype, body, headers)."""
    device_id, path = frame.get("device", ""), frame.get("path", "")
    body = base64.b64decode(frame.get("body_b64", ""))
    key = device_keys().get(device_id)
    nonce = str(frame.get("nonce", ""))
    if key is None:
        return 401, "application/json", j({"error": "sig", "fallback": False}), {}, None
    want = relaysig.sign_request(key, device_id, frame.get("ts"), nonce, path, body)
    if not relaysig.equal(want, frame.get("sig")) or abs(time.time() - int(frame.get("ts") or 0)) > CLOCK_SKEW \
            or not NONCES.fresh(device_id, nonce):
        log(f"{device_id} sig/replay reject")
        return 401, "application/json", j({"error": "sig", "fallback": False}), {}, key
    if path == "/v1/ask":
        return (*handle_audio(device_id, body, brain), key)
    try:
        req = json.loads(body)
    except ValueError:
        return 400, "application/json", j({"error": "bad_request"}), {}, key
    if isinstance(req, dict) and req.get("type") == "memory_note":
        return (*handle_memory(device_id, req), key)
    t0 = time.monotonic()
    try:
        with DEVICE_LOCKS[device_id]:
            if req.get("type") == "ask" and str(req.get("text", "")).strip():
                state = {"rounds": 0}
                text, calls = brain.start(device_id, str(req["text"])[:4000], req.get("context") or {},
                                          req.get("tools") or [], state)
                tid = "t-" + secrets.token_hex(8)
            elif req.get("type") == "tool_results":
                tid = str(req.get("turn", ""))
                state = TURNS.take(tid, device_id)
                if state is None:
                    return 409, "application/json", j({"error": "turn_unknown"}), {}, key
                state["rounds"] += 1
                text, calls = brain.resume(device_id, req.get("results") or [], state)
            else:
                return 400, "application/json", j({"error": "bad_request"}), {}, key
    except Exception as e:  # 에이전트 쪽 실패 → 기기는 예비로
        log(f"{device_id} brain fail {type(e).__name__}: {str(e)[:200]}")
        return 502, "application/json", j({"error": "brain", "fallback": True}), {}, key
    ms = int((time.monotonic() - t0) * 1000)
    if calls and state["rounds"] >= MAX_TOOL_ROUNDS:
        calls, text = [], (text + " 도구를 너무 여러 번 불러서 멈췄어요.").strip()
    log(f"{device_id} {req.get('type')} → {'tool_calls' if calls else 'final'} {ms}ms out={len(text)}자")
    if LOG_TEXT:
        log(f"Q={str(req.get('text', ''))[:200]!r} A={text[:200]!r}")
    if calls:
        TURNS.put(tid, device_id, state)
        out = {"type": "tool_calls", "turn": tid, "text": text, "calls": calls}
    else:
        out = {"type": "final", "turn": tid, "text": text}
    return 200, "application/json", j(out), {"X-Timing": f"llm={ms}"}, key


MEMO_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
INBOX_LOCK = threading.Lock()


def handle_memory(device_id, req):
    """「기억해 줘」 메모 → 승인 대기열 파일에 한 줄. 에이전트를 부르지 않는다. 본문은 로그에 남기지 않는다."""
    bad = (400, "application/json", j({"error": "bad_request"}), {})
    note_id, text = str(req.get("id", "")), str(req.get("text", "")).strip()
    if req.get("type") != "memory_note" or not MEMO_ID.match(note_id) or not text or len(text) > MEMO_MAX_CHARS \
            or str(req.get("device_id", device_id)) != device_id:  # 본문 기기와 서명한 기기가 달라선 안 된다
        log(f"{device_id} memory_note reject")
        return bad
    with INBOX_LOCK:
        seen = set()
        try:
            with MEMORY_INBOX.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        seen.add(json.loads(line).get("id"))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        status = "duplicate" if note_id in seen else "queued"
        if status == "queued":
            MEMORY_INBOX.parent.mkdir(parents=True, exist_ok=True)
            row = {"id": note_id, "ts": str(req.get("ts", ""))[:40], "device_id": device_id, "text": text,
                   "status": "pending", "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            fd = os.open(MEMORY_INBOX, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log(f"{device_id} memory_note {status} id={note_id[:8]} {len(text)}자")
    return 200, "application/json", j({"type": "memory_ack", "id": note_id, "status": status}), {}


def handle_audio(device_id, wav, brain):
    """StickS3 /v1/ask — STT·TTS 는 voice_bridge home 것(whisper.cpp·say), 두뇌는 커넥터 brain.
    vb.P["llm"](집 모드용, VOICE_AGENT) 을 쓰지 않는다 — 바깥 모드는 제한 에이전트(OAKLE_AGENT)로 돌아야 한다."""
    import tempfile
    sys.path.insert(0, str(HERE.parent))
    os.environ.setdefault("LLM", "cli")
    import voice_bridge as vb
    t, stage = {}, "stt"
    try:
        with tempfile.TemporaryDirectory() as td:
            wav_in, wav_out = os.path.join(td, "in.wav"), os.path.join(td, "out.wav")
            Path(wav_in).write_bytes(wav)
            t0 = time.monotonic(); text = vb.P["stt"](wav_in); t["stt"] = int((time.monotonic() - t0) * 1000)
            if not text:
                return 204, "text/plain", b"", {}
            stage = "llm"
            with DEVICE_LOCKS[device_id]:
                t0 = time.monotonic()
                answer, _ = brain.start(device_id, text[:4000], {}, [], {"rounds": 0})  # 기기 도구 없음 → 호출 0
                t["llm"] = int((time.monotonic() - t0) * 1000)
            emotion, answer = vb.pick_emotion(answer)
            spoken = vb.to_speakable(answer) or "답이 비어 있어요."
            stage = "tts"
            t0 = time.monotonic(); vb.P["tts"](spoken, wav_out); t["tts"] = int((time.monotonic() - t0) * 1000)
            out = Path(wav_out).read_bytes()
    except Exception as e:
        log(f"{device_id} audio fail stage={stage} {type(e).__name__}")
        return 502, "application/json", j({"error": stage, "fallback": True}), {}
    q = lambda s: urllib.parse.quote(s[:120])
    return 200, "audio/wav", out, {"X-Transcript": q(text), "X-Answer": q(spoken), "X-Oakle-Emotion": emotion,
                                    "X-Timing": ";".join(f"{k}={v}" for k, v in t.items())}



def serve_frame(ws, frame, brain):
    status, ctype, body, headers, key = handle(frame, brain)
    sig = relaysig.sign_response(key, frame.get("nonce", ""), status, body) if key else ""
    code = ""
    if status != 200 and ctype == "application/json":
        code = json.loads(body).get("error", "")  # 중계 기록용 오류 코드(본문 아님)
    try:
        ws.send_text(json.dumps({"t": "resp", "id": frame.get("id"), "status": status, "ctype": ctype,
                                 "headers": headers, "sig": sig, "code": code,
                                 "body_b64": base64.b64encode(body).decode()}))
    except (OSError, wsmin.Closed):
        log("reply lost: link down")


def run():
    token = read_secret("RELAY_CONNECTOR_TOKEN", "oakle_relay_connector_token")
    if not token:
        sys.exit(f"커넥터 토큰이 없어요: {CRED_DIR}/oakle_relay_connector_token (new-connector-token 으로 만든다)")
    brain = BRAINS[BRAIN]()
    backoff = 1
    log(f"connector brain={BRAIN} relay={RELAY_URL}")
    while True:
        try:
            ws = wsmin.connect(RELAY_URL, {"Authorization": f"Bearer {token}", "User-Agent": "oakle-connector/0.1"})
            # 중계는 20초마다 ping 을 보낸다. 60초(3번) 동안 아무것도 안 오면 반쯤 죽은 소켓으로 보고 다시 붙는다 —
            # 라우터 재기동 뒤 recv() 가 시한 없이 막혀 몇 시간씩 재접속이 없던 일이 있었다(SIGSTOP 으로 재현).
            # 3.9 에선 socket.timeout(OSError 하위)이라 아래 except 가 받고 로그엔 「link down: timeout: timed out」.
            ws.sock.settimeout(60)
            log("linked")
            backoff = 1
            ws.send_text(json.dumps({"t": "hello", "v": 1, "brain": BRAIN}))
            while True:
                raw = ws.recv()
                try:
                    frame = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if frame.get("t") == "req":
                    threading.Thread(target=serve_frame, args=(ws, frame, brain), daemon=True).start()
                # "cancel" 은 지금은 기록만: CLI 턴은 끝까지 돌고 답은 버려진다
        except (OSError, wsmin.Closed) as e:
            log(f"link down: {type(e).__name__}: {str(e)[:120]} — {backoff}s 뒤 재접속")
        time.sleep(backoff + secrets.randbelow(1000) / 1000)
        backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------- 관리 명령

def add_device(device_id):
    """기기 비밀을 만들어 한 번만 출력한다. 이 PC 엔 서명 키만, 중계 서버엔 auth 해시만 등록한다."""
    secret = secrets.token_urlsafe(32)
    p = CRED_DIR / KEYS_FILE
    keys = {}
    if p.exists():
        keys = json.loads(p.read_text())
    keys[device_id] = relaysig.derive_sign_key(secret).hex()
    p.write_text(json.dumps(keys, indent=1))
    os.chmod(p, 0o600)
    print(f"기기 비밀(기기에만 넣고 어디에도 적지 마세요): {secret}")
    print(f"중계 서버에서 실행: python3 relay_server.py add-device {device_id} "
          f"{relaysig.sha256hex(relaysig.derive_auth(secret).encode())}")


def new_connector_token():
    """커넥터 토큰을 만들어 이 PC 크리덴셜에 쓰고, 중계 서버에 둘 sha256 만 출력한다."""
    tok = secrets.token_urlsafe(32)
    p = CRED_DIR / "oakle_relay_connector_token"
    p.write_text(tok)
    os.chmod(p, 0o600)
    print(f"중계 서버의 CRED_DIR/oakle_relay_connector_token.sha256 에 넣을 값: {relaysig.sha256hex(tok.encode())}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "add-device":
        add_device(sys.argv[2]); sys.exit()
    if len(sys.argv) == 3 and sys.argv[1] == "remove-device":
        p = CRED_DIR / KEYS_FILE
        keys = json.loads(p.read_text()); keys.pop(sys.argv[2], None); p.write_text(json.dumps(keys, indent=1))
        print("removed", sys.argv[2]); sys.exit()
    if len(sys.argv) == 2 and sys.argv[1] == "new-connector-token":
        new_connector_token(); sys.exit()
    run()
