#!/usr/bin/env python3
"""oakle gadget 음성 서버 — 기기가 보낸 WAV 를 받아 STT → LLM → TTS 로 답 WAV 를 돌려준다.

한 파일로 두 프로필을 돈다.
  PROFILE=home    : 집 모드. 이 PC 의 LAN IP 에만 바인드. 로컬 STT(whisper.cpp) → OpenClaw 에이전트(VOICE_AGENT, 기본 main) → macOS say.
  PROFILE=outdoor : 실험용 클라우드 프로필. nginx 뒤 127.0.0.1 에 바인드. OpenAI 호환 STT → Claude Haiku → Google Cloud TTS.
                    (기기의 바깥 모드는 이 프로필이 아니라 server/relay/ 중계를 쓴다)

기기 ↔ 서버 규약 (두 프로필 공통):
  POST /v1/ask
    Authorization: Bearer <기기 토큰>
    Content-Type: audio/wav          (16 kHz, 16-bit, mono PCM WAV)
    X-Device-Id: sticks3-1
  → 200 audio/wav (16 kHz mono) + 헤더 X-Transcript / X-Answer (URL 인코딩, LCD 표시용)
    + X-Oakle-Emotion: joy        (대답 표정. 아래 EMOTIONS 20종 중 하나, 모르면 ok)
    + X-Timing: "stt=…;llm=…;tts=…" (ms). 기기가 자기 쪽 시각과 합쳐 단계별 지연을 한 줄로 남긴다.
    + X-Oakle-Action: {"ir":"lg_off"}  (있을 때만. 기기가 할 동작. ir = lg_power|lg_on|lg_off|circ_power, 그 밖은 서버가 버린다)
  → 401 토큰 불일치 / 413 너무 김 / 429 하루 한도 / 502 하위 단계 실패 (본문 = 단계 이름)
  GET /healthz → 200 "ok"

비밀값은 코드에 적지 않는다. 환경변수 → 없으면 CRED_DIR(기본 ~/.openclaw/credentials/) 파일.
의존성: 표준 라이브러리만. STT/TTS 는 외부 명령 또는 HTTP 로 부른다.
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROFILE = os.environ.get("PROFILE", "home")
CRED_DIR = Path(os.environ.get("CRED_DIR", str(Path.home() / ".openclaw" / "credentials")))
STATE_DIR = Path(os.environ.get("STATE_DIR", str(Path.home() / ".local" / "share" / "oakle-gadget")))
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", str(16000 * 2 * 15 + 44)))  # 15초
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "50" if PROFILE == "outdoor" else "500"))
LOG_TEXT = os.environ.get("LOG_TEXT", "0") == "1"  # 기본은 질의 원문을 로그에 남기지 않는다
BENCH_DIR = os.environ.get("BENCH_DIR", "")  # 벤치마크 때만: 받은 WAV 를 여기 남긴다(음질·인식률 재채점용). 평소엔 비워 둔다
# 집 모드가 부를 OpenClaw 에이전트. 집 LAN 안에서만 열리므로 기본은 main(모든 도구).
# 변수 이름을 커넥터의 OAKLE_AGENT 와 갈라 둔다 — 바깥 모드용 제한 에이전트 설정이 여기로 새지 않게.
AGENT = os.environ.get("VOICE_AGENT", "main")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def read_secret(env_name, cred_file):
    """환경변수 → 크리덴셜 파일. 못 읽으면 None (호출자가 그 기능만 건너뛴다)."""
    v = os.environ.get(env_name)
    if v:
        return v.strip()
    p = CRED_DIR / cred_file
    try:
        return p.read_text().strip()
    except OSError:
        log(f"secret missing: {env_name} / {p.name}")
        return None


# ---------------------------------------------------------------- 기기 토큰 / 한도

class DeviceRegistry:
    """기기 토큰은 sha256 해시만 저장한다. 폐기 = revoked=1."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices(
              device_id TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage(
              device_id TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(device_id, day));
            CREATE TABLE IF NOT EXISTS requests(
              ts TEXT NOT NULL, device_id TEXT, status INTEGER, stage TEXT,
              audio_ms INTEGER, stt_ms INTEGER, llm_ms INTEGER, tts_ms INTEGER,
              in_chars INTEGER, out_chars INTEGER, est_cost_usd REAL);
            """
        )

    def check(self, device_id, token):
        h = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.db.execute(
                "SELECT token_sha256, revoked FROM devices WHERE device_id=?", (device_id,)
            ).fetchone()
        return bool(row) and row[1] == 0 and row[0] == h

    def consume(self, device_id):
        """성공한 질의만 센다 — 실패한 요청이 한도를 깎지 않도록 응답 직전에 호출."""
        day = time.strftime("%Y-%m-%d")
        with self.lock:
            self.db.execute(
                "INSERT INTO usage(device_id, day, count) VALUES(?,?,1) "
                "ON CONFLICT(device_id, day) DO UPDATE SET count=count+1",
                (device_id, day),
            )
            self.db.commit()

    def used_today(self, device_id):
        day = time.strftime("%Y-%m-%d")
        with self.lock:
            row = self.db.execute(
                "SELECT count FROM usage WHERE device_id=? AND day=?", (device_id, day)
            ).fetchone()
        return row[0] if row else 0

    def record(self, **kw):
        cols = ",".join(kw)
        with self.lock:
            self.db.execute(
                f"INSERT INTO requests(ts,{cols}) VALUES(?{',?' * len(kw)})",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), *kw.values()),
            )
            self.db.commit()


# ---------------------------------------------------------------- 단계: STT

def stt_local_whisper_cpp(wav_path):
    """whisper.cpp (Metal). 모델 경로는 WHISPER_MODEL. 출력은 -otxt 파일."""
    model = os.environ.get("WHISPER_MODEL", str(Path.home() / "models" / "ggml-large-v3-turbo.bin"))
    out = wav_path + ".out"
    subprocess.run(
        ["whisper-cli", "-m", model, "-l", "ko", "-nt", "-otxt", "-of", out, "-f", wav_path],
        check=True, capture_output=True, timeout=60,
    )
    return Path(out + ".txt").read_text().strip()


def stt_local_mlx(wav_path):
    """mlx-whisper (별도 venv). 모델은 MLX_WHISPER_MODEL."""
    import mlx_whisper  # noqa: 설치된 venv 에서만
    model = os.environ.get("MLX_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
    return mlx_whisper.transcribe(wav_path, path_or_hf_repo=model, language="ko")["text"].strip()


def stt_openai_compatible(wav_path):
    """OpenAI 호환 /audio/transcriptions (OpenAI 또는 Groq). 실험용 outdoor 프로필."""
    base = os.environ.get("STT_BASE_URL", "https://api.groq.com/openai/v1")
    model = os.environ.get("STT_MODEL", "whisper-large-v3-turbo")
    key = read_secret("STT_API_KEY", "stt_api_key")
    if not key:
        raise RuntimeError("stt key missing")
    boundary = "----oakle" + hashlib.md5(str(time.time()).encode()).hexdigest()
    audio = Path(wav_path).read_bytes()
    parts = []
    for k, v in (("model", model), ("language", "ko"), ("response_format", "json")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="q.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode() + audio + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        base + "/audio/transcriptions", data=b"".join(parts), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return json.loads(http_json(req))["text"].strip()


# ---------------------------------------------------------------- 단계: LLM

# ---------------------------------------------------------------- 대답 표정
# 표정 20종(faces/README.md 의 번호_이름 중 이름). 이 밖의 값은 기본값으로.
EMOTIONS = ("joy", "sad", "angry", "love", "surprised", "shy", "gloomy", "lol", "ok", "no",
            "thanks", "sorry", "congrats", "fighting", "hello", "eat", "sleepy", "curious", "yes", "mindblown")
DEFAULT_EMOTION = "ok"
EMO_TAG = re.compile(r"\[\s*(?:emo|emotion|표정)\s*[:=]\s*([A-Za-z_]+)\s*\]", re.I)
# 표지가 없을 때의 간단 규칙: 위에서부터 처음 맞는 것
EMO_RULES = [
    ("sorry", r"죄송|미안"),
    ("thanks", r"고마워|고맙|감사"),
    ("congrats", r"축하"),
    ("hello", r"안녕|반가워|좋은 아침"),
    ("sleepy", r"잘 자|주무세요|졸려|피곤"),
    ("eat", r"식사|밥|점심|저녁 드|맛있"),
    ("fighting", r"화이팅|파이팅|힘내|응원"),
    ("surprised", r"실패|오류|에러|문제가 생|안 돼요|못 했"),
    ("no", r"아니요|아뇨|없어요|않아요"),
    ("curious", r"모르겠|확인해 볼게|글쎄|\?$"),
    ("lol", r"ㅋㅋ|하하|웃기"),
    ("love", r"사랑|좋아해"),
    ("yes", r"^(네|넵|예|알겠|그럴게|시작했)"),
    ("joy", r"좋아요|좋네요|다행|잘 됐|잘됐|완료|끝났"),
]


def pick_emotion(answer):
    """오클 답에서 표정 이름과 표지를 뗀 답을 돌려준다. 표지 → 규칙 → 기본값."""
    tag = None
    m = EMO_TAG.search(answer)
    if m:
        tag = m.group(1).lower()
        answer = (answer[:m.start()] + answer[m.end():]).strip()
    if tag in EMOTIONS:
        return tag, answer
    for name, pat in EMO_RULES:
        if re.search(pat, answer.strip()):
            return name, answer
    return DEFAULT_EMOTION, answer


EMO_ASK = (
    "답 맨 앞에 지금 기분에 맞는 표정 표지 하나를 [표정:이름] 꼴로 붙이세요. 이름은 "
    + "·".join(EMOTIONS) + " 중 하나. 표지는 읽히지 않고 기기 화면 얼굴로만 쓰입니다."
)

# ---------------------------------------------------------------- 기기 동작 (집 모드: 말한 기기에서 IR 을 쏜다)
# 「티비 꺼 줘/켜 줘」 → 에이전트가 [리모컨:lg_off] 표지를 쓰면 여기서 떼어 응답 헤더
# X-Oakle-Action: {"ir":"lg_off"} 로 싣고, StickS3 가 IR(NEC 38 kHz)을 쏜다. 기기는 TV 상태를 모른다.
# 서버가 실행하지 않는다 — 신호는 말한 기기 머리에서 나가야 한다.
# 허용 목록 밖 이름은 버린다(헤더에 안 싣는다). 표지 자체는 이름이 무엇이든 답에서 지운다(스피커로 읽히지 않게).
# circ_power = 선풍기·서큘레이터 전원 토글(켜기·끄기 구분 없는 리모컨). 코드는 펌웨어 CIRC_POWER.
IR_ACTIONS = ("lg_power", "lg_on", "lg_off", "circ_power")
ACT_TAG = re.compile(r"\[\s*리모컨\s*[:=]([^\]]*)\]")
DEVICE_ACTIONS = os.environ.get("DEVICE_ACTIONS", "1" if PROFILE == "home" else "0") == "1"


def pick_action(answer):
    """오클 답에서 [리모컨:이름] 표지를 모두 떼고, 허용 목록에 든 첫 이름으로 동작을 만든다. 없으면 None."""
    action = None
    for m in ACT_TAG.finditer(answer):
        name = m.group(1).strip().lower()
        if action is None and name in IR_ACTIONS:
            action = {"ir": name}
        elif name not in IR_ACTIONS:
            log(f"action dropped: {name[:20]!r}")
    return action, ACT_TAG.sub("", answer).strip()


ACT_ASK = (
    " 이 기기는 LG TV 와 서큘레이터의 적외선 리모컨 역할도 합니다. TV 나 서큘레이터 전원을 켜거나 끄라는 말이면 "
    "다른 도구(HA 등)를 쓰지 말고 답에 표지 하나를 붙이세요. TV: 켜기 [리모컨:lg_on], 끄기 [리모컨:lg_off], "
    "켜기·끄기가 분명하지 않으면 [리모컨:lg_power]. 서큘레이터(선풍기): 켜기·끄기 모두 [리모컨:circ_power](전원 토글 하나뿐). "
    "이 넷 말고는 쓰지 마세요. "
    "기기가 신호를 쏠 뿐 TV·서큘레이터 상태는 모르니 「TV 끄는 신호를 보냈어요」처럼 말하세요."
)

VOICE_PREFIX = (
    "[음성 리모컨에서 온 질문입니다. 답은 스피커로 읽힙니다. "
    "마크다운·표·코드·링크 없이 한국어 두세 문장으로 답하세요. "
    "오래 걸리는 일이면 시작했다고만 짧게 말하세요. " + EMO_ASK + (ACT_ASK if DEVICE_ACTIONS else "") + "]\n"
)


def llm_openclaw_http(text, device_id):
    """OpenClaw Gateway /v1/chat/completions (loopback). gateway.http.endpoints.chatCompletions 가 켜져 있어야 한다.
    `user` 를 고정하면 Gateway 가 같은 세션 키를 파생 → 기기별 대화가 이어진다 (docs/gateway/openai-http-api.md)."""
    token = read_secret("OPENCLAW_GATEWAY_TOKEN", "openclaw_gateway_token")
    if not token:
        raise RuntimeError("gateway token missing")
    body = {
        "model": f"openclaw/{AGENT}",
        "user": f"voice:{device_id}",
        "messages": [{"role": "user", "content": VOICE_PREFIX + text}],
    }
    req = urllib.request.Request(
        os.environ.get("OPENCLAW_URL", "http://127.0.0.1:18789") + "/v1/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    data = json.loads(http_json(req, timeout=float(os.environ.get("LLM_TIMEOUT", "120"))))
    return data["choices"][0]["message"]["content"] or ""


def llm_openclaw_cli(text, device_id):
    """설정 변경 없이 쓸 수 있는 대안: `openclaw agent` CLI. 노드 기동 비용만큼 느리다 [추론]."""
    r = subprocess.run(
        [os.environ.get("OPENCLAW_BIN", "openclaw"), "agent", "--agent", AGENT, "--session-key", f"agent:{AGENT}:voice-{device_id}",
         "--message", VOICE_PREFIX + text, "--json"],
        check=True, capture_output=True, text=True, timeout=180,
    )
    data = json.loads(r.stdout)
    if data.get("status") != "ok":
        raise RuntimeError(f"openclaw agent status {data.get('status')}")
    # openclaw 2026.9.x 출력: result.payloads[].text, result.meta.finalAssistantVisibleText
    res = data.get("result") or {}
    text = (res.get("meta") or {}).get("finalAssistantVisibleText")
    return text or "\n".join(p.get("text") or "" for p in res.get("payloads") or []).strip()


def llm_claude_haiku(text, device_id):
    """실험용 outdoor 프로필: Anthropic Messages API 직접 호출. 키는 서버에만 있다."""
    key = read_secret("ANTHROPIC_API_KEY", "anthropic_voice_api_key")
    if not key:
        raise RuntimeError("anthropic key missing")
    body = {
        "model": os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5"),
        "max_tokens": int(os.environ.get("CLAUDE_MAX_TOKENS", "200")),
        "system": "당신은 손바닥만 한 음성 기기의 비서입니다. 한국어 한두 문장, 마크다운 없이 답하세요. " + EMO_ASK,
        "messages": [{"role": "user", "content": text}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    data = json.loads(http_json(req, timeout=30))
    return "".join(b.get("text", "") for b in data.get("content", []))


# ---------------------------------------------------------------- 단계: TTS

def tts_macos_say(text, out_wav):
    """macOS say → AIFF → afconvert 로 16 kHz mono WAV. 둘 다 OS 기본 명령."""
    aiff = out_wav + ".aiff"
    subprocess.run(["say", "-v", os.environ.get("SAY_VOICE", "Yuna"), "-o", aiff, text],
                   check=True, timeout=60)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, out_wav],
                   check=True, timeout=30)


def normalize_wav(path, peak_dbfs=None):
    """16-bit PCM WAV 를 피크 기준으로 키운다(기본 -1 dBFS). say 출력은 피크가 낮아 StickS3 작은 스피커에서 잘 안 들렸다.
    표준 라이브러리(wave·array)만 쓴다. 16-bit 가 아니거나 거의 무음이면 그대로 둔다. 반환 = 곱한 배율(1.0 = 안 바꿈)."""
    import array
    import wave
    if peak_dbfs is None:
        peak_dbfs = float(os.environ.get("TTS_PEAK_DBFS", "-1"))
    with wave.open(path, "rb") as w:
        params = w.getparams()
        frames = w.readframes(params.nframes)
    if params.sampwidth != 2 or not frames:
        return 1.0
    pcm = array.array("h", frames)
    if sys.byteorder == "big":
        pcm.byteswap()
    peak = max(max(pcm), -min(pcm))
    if peak < 300:                                   # 거의 무음: 잡음만 키우게 된다
        return 1.0
    gain = min(32767 * 10 ** (peak_dbfs / 20) / peak, 20.0)   # 최대 +26 dB
    if abs(gain - 1.0) < 0.01:
        return 1.0
    pcm = array.array("h", (max(-32768, min(32767, int(round(v * gain)))) for v in pcm))
    if sys.byteorder == "big":
        pcm.byteswap()
    with wave.open(path, "wb") as w:
        w.setparams(params)
        w.writeframes(pcm.tobytes())
    return gain


def tts_google(text, out_wav):
    """실험용 outdoor 프로필: Google Cloud TTS REST (LINEAR16 16 kHz → WAV 헤더 포함으로 온다)."""
    import base64
    key = read_secret("GOOGLE_TTS_API_KEY", "google_tts_api_key")
    if not key:
        raise RuntimeError("tts key missing")
    body = {
        "input": {"text": text},
        "voice": {"languageCode": "ko-KR", "name": os.environ.get("TTS_VOICE", "ko-KR-Neural2-A")},
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 16000},
    }
    req = urllib.request.Request(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        data=json.dumps(body).encode(), method="POST",
        headers={"X-Goog-Api-Key": key, "Content-Type": "application/json"},
    )
    Path(out_wav).write_bytes(base64.b64decode(json.loads(http_json(req, timeout=30))["audioContent"]))


# ---------------------------------------------------------------- 공통

def http_json(req, timeout=60):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read()
    if "json" not in ctype:  # 죽은 API 가 200 + HTML 을 주는 경우를 고장으로 드러낸다
        raise RuntimeError(f"non-json response: {ctype}")
    return body


def to_speakable(text):
    text = re.sub(r"```.*?```", " 코드는 화면에서 확인하세요. ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", " 링크 ", text)
    text = re.sub(r"[*_`#>|]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: int(os.environ.get("MAX_SPOKEN_CHARS", "400"))]


PIPELINES = {
    "home": dict(
        stt={"whisper_cpp": stt_local_whisper_cpp, "mlx": stt_local_mlx}[os.environ.get("STT", "whisper_cpp")],
        llm={"http": llm_openclaw_http, "cli": llm_openclaw_cli}[os.environ.get("LLM", "http")],
        tts=tts_macos_say,
    ),
    "outdoor": dict(stt=stt_openai_compatible, llm=llm_claude_haiku, tts=tts_google),
}
P = PIPELINES[PROFILE]
REG = None  # __main__ 에서 연다. relay/connector.py 가 파이프라인만 빌려 쓸 때 DB 를 만들지 않게


def open_registry():
    global REG
    REG = DeviceRegistry(STATE_DIR / f"{PROFILE}.sqlite3")
    return REG


class Handler(BaseHTTPRequestHandler):
    server_version = "oakle-gadget/0.1"

    def log_message(self, fmt, *args):  # 기본 접근 로그는 끄고 record() 로 남긴다
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        # 기기가 받는 중에 끊으면(말하는 중 A 끼어들기·슬립) 한 줄만 남긴다(Traceback 대신)
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as e:
            self.close_connection = True
            log(f"client gone status={code} bytes={len(body)} {type(e).__name__}")

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok")
        self._send(404)

    def do_POST(self):
        if self.path != "/v1/ask":
            return self._send(404)
        device_id = self.headers.get("X-Device-Id", "")
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not device_id or not REG.check(device_id, token):
            REG.record(device_id=device_id[:32], status=401, stage="auth")
            return self._send(401, b"auth")
        if REG.used_today(device_id) >= DAILY_LIMIT:
            REG.record(device_id=device_id, status=429, stage="limit")
            return self._send(429, b"limit")
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 44 or n > MAX_AUDIO_BYTES:
            return self._send(413, b"size")
        audio = self.rfile.read(n)
        t = {"audio_ms": int((n - 44) / 32)}  # 16 kHz * 2 B = 32 B/ms
        stage = "stt"
        with tempfile.TemporaryDirectory() as td:
            wav_in, wav_out = os.path.join(td, "in.wav"), os.path.join(td, "out.wav")
            Path(wav_in).write_bytes(audio)
            if BENCH_DIR:
                Path(BENCH_DIR).mkdir(parents=True, exist_ok=True)
                Path(BENCH_DIR, f"{device_id}-{time.strftime('%Y%m%d-%H%M%S')}.wav").write_bytes(audio)
            try:
                t0 = time.monotonic(); text = P["stt"](wav_in); t["stt_ms"] = int((time.monotonic() - t0) * 1000)
                if not text:
                    return self._send(204)
                stage = "llm"
                t0 = time.monotonic(); answer = P["llm"](text, device_id); t["llm_ms"] = int((time.monotonic() - t0) * 1000)
                action, answer = pick_action(answer)
                if not DEVICE_ACTIONS:
                    action = None
                emotion, answer = pick_emotion(answer)
                spoken = to_speakable(answer) or "답이 비어 있어요."
                stage = "tts"
                t0 = time.monotonic(); P["tts"](spoken, wav_out); normalize_wav(wav_out); t["tts_ms"] = int((time.monotonic() - t0) * 1000)
                out = Path(wav_out).read_bytes()
            except Exception as e:  # 어느 단계에서 죽었는지 기기 LCD 에 그대로 보인다
                log(f"fail stage={stage} device={device_id} err={type(e).__name__}: {e}")
                REG.record(device_id=device_id, status=502, stage=stage, **t)
                return self._send(502, stage.encode())
        REG.consume(device_id)
        REG.record(device_id=device_id, status=200, stage="done", in_chars=len(text), out_chars=len(spoken), **t)
        if LOG_TEXT:
            log(f"Q={text!r} A={spoken!r}")
        q = lambda s: urllib.parse.quote(s[:120])
        timing = ";".join(f"{k[:-3]}={t[k]}" for k in ("stt_ms", "llm_ms", "tts_ms") if k in t)
        hdr = {"X-Transcript": q(text), "X-Answer": q(spoken), "X-Timing": timing, "X-Oakle-Emotion": emotion}
        if action:
            hdr["X-Oakle-Action"] = json.dumps(action, separators=(",", ":"))
            log(f"action device={device_id} {hdr['X-Oakle-Action']}")
        self._send(200, out, "audio/wav", hdr)


def add_device(device_id):
    """새 기기 토큰 발급: python3 voice_bridge.py add-device sticks3-1  (토큰은 이때 한 번만 출력)."""
    import secrets as pysecrets
    tok = pysecrets.token_urlsafe(32)
    REG.db.execute(
        "INSERT OR REPLACE INTO devices(device_id, token_sha256, revoked, created_at) VALUES(?,?,0,?)",
        (device_id, hashlib.sha256(tok.encode()).hexdigest(), time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    REG.db.commit()
    print(tok)


def revoke_device(device_id):
    REG.db.execute("UPDATE devices SET revoked=1 WHERE device_id=?", (device_id,))
    REG.db.commit()
    print("revoked", device_id)


if __name__ == "__main__":
    open_registry()
    if len(sys.argv) == 3 and sys.argv[1] == "add-device":
        add_device(sys.argv[2]); sys.exit()
    if len(sys.argv) == 3 and sys.argv[1] == "revoke-device":
        revoke_device(sys.argv[2]); sys.exit()
    # home: 이 PC 의 LAN IP 에만. outdoor: 127.0.0.1 (nginx 가 TLS 종단).
    host = os.environ.get("BIND", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    log(f"profile={PROFILE} bind={host}:{port} limit/day={DAILY_LIMIT}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
