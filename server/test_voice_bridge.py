#!/usr/bin/env python3
"""voice_bridge 흉내 시험 — STT·LLM·TTS 를 갈아 끼우고 진짜 HTTP 로 /v1/ask 를 부른다.

  python3 server/test_voice_bridge.py

LLM·whisper·say 호출 0회. 임시 STATE_DIR 를 쓰고 실제 기기 DB·크리덴셜은 건드리지 않는다.
보는 것: 「[리모컨:…]」 표지 → X-Oakle-Action 헤더, 허용 목록 밖 표지는 버려짐, 표지가 읽히는 답에서 빠짐.
"""
import json
import os
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

TD = Path(tempfile.mkdtemp(prefix="oakle-vb-test-"))
os.environ.update(PROFILE="home", STATE_DIR=str(TD), LLM="cli", DAILY_LIMIT="100")
os.environ.pop("DEVICE_ACTIONS", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_bridge as vb  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


ANSWER = {"text": ""}
SEEN = []
vb.P["stt"] = lambda wav: "티비 꺼 줘"
vb.P["llm"] = lambda text, dev: (SEEN.append(text), ANSWER["text"])[1]
vb.P["tts"] = lambda text, out: Path(out).write_bytes(b"RIFF" + b"\0" * 40 + text.encode())
vb.normalize_wav = lambda path, peak_dbfs=None: 1.0
vb.open_registry()
TOKEN = "test-token-123"
vb.REG.db.execute("INSERT INTO devices VALUES(?,?,0,'t')", ("sticks3-t", vb.hashlib.sha256(TOKEN.encode()).hexdigest()))
vb.REG.db.commit()
srv = ThreadingHTTPServer(("127.0.0.1", 0), vb.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}/v1/ask"


def ask(answer):
    ANSWER["text"] = answer
    wav = b"RIFF" + b"\0" * 40 + b"\1\0" * 8000
    req = urllib.request.Request(URL, data=wav, method="POST", headers={
        "Authorization": f"Bearer {TOKEN}", "X-Device-Id": "sticks3-t", "Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, dict(r.headers), r.read()


def act(h):
    v = h.get("X-Oakle-Action")
    return json.loads(v) if v else None


def said(h):
    return urllib.parse.unquote(h["X-Answer"])


st, h, body = ask("[표정:ok] [리모컨:lg_off] TV 끄는 신호를 보냈어요.")
check("끄기 표지 → action lg_off", st == 200 and act(h) == {"ir": "lg_off"}, h.get("X-Oakle-Action"))
check("표지는 읽히는 답·TTS 에서 빠짐", "리모컨" not in said(h) and "리모컨".encode() not in body and said(h) == "TV 끄는 신호를 보냈어요.", said(h))
check("표정 표지는 그대로 동작", h["X-Oakle-Emotion"] == "ok", h["X-Oakle-Emotion"])
check("요청 머리말(VOICE_PREFIX)에 기기 도구 안내", all(f"[리모컨:{a}]" in vb.VOICE_PREFIX for a in vb.IR_ACTIONS))

st, h, _ = ask("[리모컨:lg_on] TV 켜는 신호를 보냈어요.")
check("켜기 표지 → lg_on", act(h) == {"ir": "lg_on"}, h.get("X-Oakle-Action"))
st, h, _ = ask("[리모컨: LG_POWER ] 신호 보냈어요.")
check("토글·대소문자·공백 허용 → lg_power", act(h) == {"ir": "lg_power"}, h.get("X-Oakle-Action"))
st, h, _ = ask("[리모컨:circ_power] 서큘레이터 켜는 신호를 보냈어요.")
check("서큘레이터 → circ_power", act(h) == {"ir": "circ_power"} and said(h) == "서큘레이터 켜는 신호를 보냈어요.", h.get("X-Oakle-Action"))
st, h, _ = ask("[리모컨:circ_on] 켰어요.")
check("없는 circ_on 은 버림(토글 하나뿐)", act(h) is None, h.get("X-Oakle-Action"))

st, h, _ = ask("[리모컨:lg_volume_up] [리모컨:rm -rf] 소리 키웠어요.")
check("허용 목록 밖 표지 → action 없음", st == 200 and act(h) is None, h.get("X-Oakle-Action"))
check("버린 표지도 답에서 지움", "리모컨" not in said(h) and "lg_" not in said(h), said(h))

st, h, _ = ask("[리모컨:evil] [리모컨:lg_off] 껐어요.")
check("섞이면 허용된 것만", act(h) == {"ir": "lg_off"}, h.get("X-Oakle-Action"))
st, h, _ = ask("[리모컨:lg_off] [리모컨:lg_on] 둘 다.")
check("둘 이상이면 첫 것 하나만", act(h) == {"ir": "lg_off"}, h.get("X-Oakle-Action"))

st, h, _ = ask("[표정:joy] 오늘 날씨는 맑아요.")
check("표지 없으면 헤더 없음", "X-Oakle-Action" not in h, h.get("X-Oakle-Action"))

vb.DEVICE_ACTIONS = False
st, h, _ = ask("[리모컨:lg_off] 껐어요.")
check("DEVICE_ACTIONS=0 이면 표지는 지우고 action 없음", act(h) is None and "리모컨" not in said(h), said(h))
vb.DEVICE_ACTIONS = True

check("pick_action 단위: 표지 없음", vb.pick_action("그냥 답") == (None, "그냥 답"))
srv.shutdown()
print(f"\n{sum(results)}/{len(results)} 통과 — 상태 {TD}")
sys.exit(0 if all(results) else 1)
