#!/usr/bin/env python3
"""로컬 왕복 시험 — 한 PC 안에서 중계(127.0.0.1)·커넥터·가짜 기기를 띄워 시나리오를 돈다.

  python3 test_local.py            # BRAIN=echo 시나리오만 (에이전트 호출 0회)
  python3 test_local.py --real     # + 실제 에이전트(BRAIN=cli) 도구 왕복 1회 (에이전트 턴 2번)

임시 CRED_DIR·STATE_DIR 를 쓰고, 끝나면 띄운 프로세스를 모두 정리한다. 실제 크리덴셜 파일은 건드리지 않는다.
"""
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import relaysig  # noqa: E402
from fake_device import Device, RelayError  # noqa: E402

PORT = int(os.environ.get("TEST_PORT", "18766"))
BASE = f"http://127.0.0.1:{PORT}"
TOOLS = [
    {"name": "get_status", "description": "현재 시각과 배터리", "input_schema": {"type": "object", "properties": {}}},
    {"name": "flashlight", "description": "손전등을 켜거나 끈다.",
     "input_schema": {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}},
]
results, procs = [], []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def expect_err(name, fn, status, code=None, fallback=None):
    try:
        fn()
        check(name, False, "성공해 버림")
    except RelayError as e:
        ok = e.status == status and (code is None or e.body == code) and (fallback is None or e.fallback == fallback)
        check(name, ok, f"{e.status} {e.body} fallback={e.fallback}")


def health():
    with urllib.request.urlopen(BASE + "/healthz", timeout=5) as r:
        return r.read().decode(), r.headers.get("X-Connector")


def wait(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def main(real):
    td = Path(tempfile.mkdtemp(prefix="oakle-relay-test-"))
    cred, state = td / "cred", td / "state"
    cred.mkdir(); state.mkdir()
    env = dict(os.environ, CRED_DIR=str(cred), STATE_DIR=str(state), PORT=str(PORT),
               DAILY_LIMIT="50", RATE_PER_MIN="25", TURN_TIMEOUT="4", PING_EVERY="2",
               MEMO_DAILY_LIMIT="4", MEMORY_INBOX=str(state / "device_memory_inbox.jsonl"))
    # 커넥터 토큰: 커넥터엔 평문, 중계엔 해시만
    ctok = secrets.token_urlsafe(32)
    (cred / "oakle_relay_connector_token").write_text(ctok)
    (cred / "oakle_relay_connector_token.sha256").write_text(relaysig.sha256hex(ctok.encode()))
    # 기기 2대 발급 (connector add-device 와 같은 계산)
    devs = {}
    for dev_id in ("text-test", "sticks3-test"):
        out = subprocess.run([sys.executable, str(HERE / "connector.py"), "add-device", dev_id], env=env,
                             capture_output=True, text=True, check=True).stdout
        secret = out.split("어디에도 적지 마세요): ")[1].split()[0]
        vm_line = out.strip().splitlines()[-1].split("python3 relay_server.py ")[1].split()
        subprocess.run([sys.executable, str(HERE / "relay_server.py"), *vm_line], env=env, check=True,
                       capture_output=True)
        devs[dev_id] = secret
    keys = json.loads((cred / "oakle_relay_device_keys.json").read_text())
    check("발급: 커넥터엔 서명 키만, 중계엔 auth 해시만", all(len(v) == 64 for v in keys.values())
          and devs["text-test"] not in (cred / "oakle_relay_device_keys.json").read_text())

    relay = subprocess.Popen([sys.executable, str(HERE / "relay_server.py")], env=env,
                             stderr=open(td / "relay.log", "w"))
    procs.append(relay)
    assert wait(lambda: health()[0] == "ok"), "relay not up"
    txt = Device(BASE, "text-test", devs["text-test"])

    # 1) 커넥터 없음 → 503 예비 신호
    check("헬스체크: 커넥터 없을 때 down", health() == ("ok", "down"))
    expect_err("커넥터 없음 → 503 connector_offline(fallback)",
               lambda: txt.turn({"type": "ask", "text": "안녕", "tools": TOOLS}), 503, "connector_offline", True)

    def start_connector(brain, extra=None):
        p = subprocess.Popen([sys.executable, str(HERE / "connector.py")],
                             env=dict(env, BRAIN=brain, RELAY_URL=f"ws://127.0.0.1:{PORT}/v1/connect", **(extra or {})),
                             stderr=open(td / f"connector-{brain}.log", "a"))
        procs.append(p)
        assert wait(lambda: health()[1] == "up"), "connector not linked"
        return p

    conn = start_connector("echo", {"ECHO_SLOW": "6"})
    check("헬스체크: 커넥터 붙은 뒤 up", health() == ("ok", "up"))

    # 2) 텍스트 왕복(echo)
    lat = []
    for i in range(5):
        r = txt.turn({"type": "ask", "text": f"안녕 {i}", "tools": TOOLS})
        lat.append(r["_ms"])
    check("텍스트 왕복(echo) + 응답 서명 확인", r["type"] == "final" and "메아리" in r["text"],
          f"지연 ms {lat} (중앙 {sorted(lat)[2]})")

    # 3) 도구 왕복
    r1 = txt.turn({"type": "ask", "text": "도구:get_status", "tools": TOOLS})
    r2 = txt.turn({"type": "tool_results", "turn": r1.get("turn"),
                  "results": [{"id": r1["calls"][0]["id"], "content": "배터리 80%", "is_error": False}]})
    check("도구 왕복 ask→tool_calls→tool_results→final", r1["type"] == "tool_calls" and r2["type"] == "final"
          and "배터리 80%" in r2["text"], f"{r1['_ms']}+{r2['_ms']}ms")
    expect_err("같은 턴 결과 두 번 → 409", lambda: txt.turn(
        {"type": "tool_results", "turn": r1.get("turn"), "results": []}), 409, "turn_unknown")

    # 4) 인증
    expect_err("틀린 기기 토큰 → 401", lambda: txt.turn({"type": "ask", "text": "x"}, auth="0" * 64), 401, "auth")
    expect_err("등록 안 된 기기 → 401", lambda: Device(BASE, "text-ghost", "nope").turn({"type": "ask", "text": "x"}),
               401, "auth")
    # 중계를 통과해도 커넥터가 서명으로 거른다(중계 서버가 본문을 바꾼 경우)
    expect_err("본문 변조 → 커넥터가 401 sig", lambda: txt.turn(
        {"type": "ask", "text": "원래 질문"}, tamper=lambda b: b.replace("원래".encode(), "바뀐".encode())), 401, "sig")
    fixed = secrets.token_hex(12)
    txt.turn({"type": "ask", "text": "한 번", "tools": []}, nonce=fixed)
    expect_err("같은 nonce 재전송(replay) → 401 sig", lambda: txt.turn({"type": "ask", "text": "한 번", "tools": []},
                                                                    nonce=fixed), 401, "sig")
    # 응답 변조 감지: 서명이 맞지 않는 200 은 기기가 버린다
    good = relaysig.sign_response(txt.key, "n1", 200, b'{"type":"final","text":"a"}')
    check("응답 변조 감지(서명 대조)", not relaysig.equal(good, relaysig.sign_response(
        txt.key, "n1", 200, b'{"type":"final","text":"b"}')))

    # 5) 크기
    expect_err("본문 64KB 초과 → 413", lambda: txt.turn({"type": "ask", "text": "가" * 30000}), 413, "size")

    # 6) 하루 한도: 오늘 사용량을 한도까지 채운 뒤
    st = Device(BASE, "sticks3-test", devs["sticks3-test"])
    db = sqlite3.connect(str(state / "relay.sqlite3"))
    db.execute("INSERT OR REPLACE INTO usage(device_id, day, count) VALUES(?,?,?)",
               ("sticks3-test", time.strftime("%Y-%m-%d"), 50)); db.commit(); db.close()
    expect_err("하루 한도 초과 → 429 limit(fallback)", lambda: st.turn({"type": "ask", "text": "x"}), 429, "limit", True)
    check("한도는 기기별(txt 는 계속 됨)", txt.turn({"type": "ask", "text": "아직 돼요?", "tools": []})["type"] == "final")

    # 7) 분당 시도 한도(25)
    codes = []
    for _ in range(26):
        try:
            txt.turn({"type": "ask", "text": "가" * 30000})
        except RelayError as e:
            codes.append(e.body)
    check("분당 시도 한도 → 429 rate", "rate" in codes, f"{codes.count('size')}×size 뒤 {codes.count('rate')}×rate")
    time.sleep(61)  # 분당 창이 지나가길 기다린다

    # 7b) 「기억해 줘」 메모 → 승인 대기열(에이전트 호출 없음)
    inbox = state / "device_memory_inbox.jsonl"
    rows = lambda: [json.loads(x) for x in inbox.read_text().splitlines()] if inbox.exists() else []
    st1, d1, sg1 = txt.memo("note-test-0001", "커피는 라떼를 좋아함")
    st2, d2, sg2 = txt.memo("note-test-0001", "커피는 라떼를 좋아함")
    r = rows()
    check("memory_note → inbox 한 줄(pending) + 응답 서명", st1 == 200 and sg1 and d1.get("status") == "queued"
          and len(r) == 1 and r[0]["status"] == "pending" and r[0]["device_id"] == "text-test"
          and r[0]["text"] == "커피는 라떼를 좋아함", f"{st1} {d1}")
    check("같은 id 재전송 → duplicate, 줄 안 늘어남", st2 == 200 and d2.get("status") == "duplicate" and len(rows()) == 1)
    check("inbox 파일 권한 600", oct(inbox.stat().st_mode & 0o777) == "0o600", oct(inbox.stat().st_mode & 0o777))
    st, d, _ = txt.memo("note-test-0002", "가" * 501)
    check("501자 메모 → 400 bad_request", st == 400 and d.get("error") == "bad_request", f"{st} {d}")
    st, d, _ = txt.memo("note-test-0003", "남의 이름으로", device_id="sticks3-test")
    check("본문 device_id 가 서명 기기와 다름 → 400", st == 400, f"{st} {d}")
    st, d, _ = txt.memo("note-test-0004", "원래 메모", tamper=lambda b: b.replace("원래".encode(), "바뀐".encode()))
    check("메모 본문 변조 → 커넥터 401 sig", st == 401 and d.get("error") == "sig", f"{st} {d}")
    txt.memo("note-test-0005", "다섯째")   # 성공 3번째(duplicate 도 성공으로 센다) → 한도 4 중 3
    txt.memo("note-test-0006", "여섯째")   # 4번째
    st, d, _ = txt.memo("note-test-0007", "일곱째")
    check("하루 메모 상한 → 429 limit, fallback 아님", st == 429 and d.get("error") == "limit" and d.get("fallback") is False,
          f"{st} {d}")
    check("메모 상한은 /v1/turn 한도와 따로", txt.turn({"type": "ask", "text": "메모 뒤에도", "tools": []})["type"] == "final")
    check("거절된 메모는 inbox 에 안 들어감", [x["id"] for x in rows()] == ["note-test-0001", "note-test-0005", "note-test-0006"],
          str([x["id"] for x in rows()]))

    # 8) 타임아웃: 중계 TURN_TIMEOUT=4s, echo 가 6s 걸림
    expect_err("커넥터 응답 지연 → 504 timeout(fallback)",
               lambda: txt.turn({"type": "ask", "text": "느리게", "tools": []}), 504, "timeout", True)

    # 9) 요청 도중 커넥터 끊김 → 503 connector_lost, 그 뒤 connector_offline, 재접속 후 회복
    import threading
    box = {}

    def slow():
        try:
            txt.turn({"type": "ask", "text": "느리게", "tools": []})
        except RelayError as e:
            box["e"] = e
    th = threading.Thread(target=slow); th.start()
    time.sleep(1)
    conn.send_signal(signal.SIGKILL); conn.wait()
    th.join(10)
    e = box.get("e")
    check("요청 중 커넥터 사망 → 503 connector_lost(fallback)", bool(e) and e.status == 503
          and e.body == "connector_lost" and e.fallback, f"{e and e.status} {e and e.body}")
    wait(lambda: health()[1] == "down", 5)
    expect_err("사망 뒤 새 요청 → 503 connector_offline(fallback)",
               lambda: txt.turn({"type": "ask", "text": "안녕"}), 503, "connector_offline", True)
    t0 = time.monotonic()
    conn = start_connector("echo")
    check("커넥터 재기동 → 재접속·회복", txt.turn({"type": "ask", "text": "다시", "tools": []})["type"] == "final",
          f"재접속까지 {int((time.monotonic() - t0) * 1000)}ms")
    # 중계 쪽이 죽었다 살아나는 경우: 커넥터가 백오프로 다시 붙는다
    relay.send_signal(signal.SIGTERM); relay.wait()
    relay = subprocess.Popen([sys.executable, str(HERE / "relay_server.py")], env=env,
                             stderr=open(td / "relay.log", "a"))
    procs.append(relay)
    t0 = time.monotonic()
    ok = wait(lambda: health()[1] == "up", 20)
    check("중계 재시작 → 커넥터 자동 재접속", ok, f"{int((time.monotonic() - t0) * 1000)}ms")

    # 10) 로그에 본문이 없는지
    logs = "".join((td / f).read_text() for f in os.listdir(td) if f.endswith(".log"))
    check("로그에 질의·답·메모 본문 없음", "안녕" not in logs and "메아리" not in logs and "배터리 80%" not in logs
          and "라떼" not in logs and "다섯째" not in logs)

    # 11) 실제 에이전트(cli) — 도구 왕복 1회
    if real:
        conn.send_signal(signal.SIGTERM); conn.wait()
        wait(lambda: health()[1] == "down", 5)
        relay.send_signal(signal.SIGTERM); relay.wait()
        relay = subprocess.Popen([sys.executable, str(HERE / "relay_server.py")], env=dict(env, TURN_TIMEOUT="120"),
                                 stderr=open(td / "relay.log", "a"))
        procs.append(relay)
        wait(lambda: health()[0] == "ok")
        start_connector("cli")
        r1 = txt.turn({"type": "ask", "text": "손전등 켜 줘", "context": {"now": time.strftime("%Y-%m-%d %H:%M")},
                      "tools": TOOLS})
        detail = f"1차 {r1['type']} {r1['_ms']}ms"
        if r1["type"] == "tool_calls":
            c = r1["calls"][0]
            r2 = txt.turn({"type": "tool_results", "turn": r1["turn"],
                          "results": [{"id": c["id"], "content": "손전등을 켰다", "is_error": False}]})
            detail += f" ({c['name']} {json.dumps(c['input'])}) → 2차 {r2['type']} {r2['_ms']}ms: {r2['text'][:60]}"
            check("실제 에이전트 도구 왕복", c["name"] == "flashlight" and r2["type"] == "final", detail)
        else:
            check("실제 에이전트 도구 왕복", False, detail + f": {r1['text'][:80]}")

    print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} 통과 — 로그 {td}")
    return all(ok for _, ok, _ in results)


if __name__ == "__main__":
    try:
        ok = main("--real" in sys.argv)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
    sys.exit(0 if ok else 1)
