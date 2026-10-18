#!/usr/bin/env python3
"""가짜 기기 — 중계 규약을 기기와 똑같이 부른다(서명·응답 서명 검증 포함). 시험용.

  DEVICE_SECRET=… python3 fake_device.py http://127.0.0.1:8766 text-test "안녕"
  DEVICE_SECRET=… python3 fake_device.py https://relay.example.com wav sticks3-1 q.wav   # StickS3 바깥 모드 음성 흉내(16k mono WAV)
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relaysig  # noqa: E402


class RelayError(Exception):
    def __init__(self, status, body, fallback):
        super().__init__(f"{status} {body}")
        self.status, self.body, self.fallback = status, body, fallback


class Device:
    def __init__(self, base, device_id, secret):
        self.base, self.device_id = base.rstrip("/"), device_id
        self.auth = relaysig.derive_auth(secret)
        self.key = relaysig.derive_sign_key(secret)

    def post(self, path, body: bytes, ctype="application/json", nonce=None, tamper=None, auth=None):
        ts, nonce = int(time.time()), nonce or secrets.token_hex(12)
        sig = relaysig.sign_request(self.key, self.device_id, ts, nonce, path, body)
        if tamper:
            body = tamper(body)
        req = urllib.request.Request(self.base + path, data=body, method="POST", headers={
            "Authorization": f"Bearer {auth or self.auth}", "X-Device-Id": self.device_id,
            "Content-Type": ctype, "X-Oakle-Ts": str(ts), "X-Oakle-Nonce": nonce, "X-Oakle-Sig": sig})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                status, out, hdr = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, out, hdr = e.code, e.read(), e.headers
        ms = int((time.monotonic() - t0) * 1000)
        signed = relaysig.equal(relaysig.sign_response(self.key, nonce, status, out), hdr.get("X-Oakle-Sig", ""))
        return status, out, hdr, ms, signed

    def turn(self, payload, **kw):
        """200 이고 서명이 맞으면 dict. 아니면 RelayError(fallback 여부 포함)."""
        status, out, hdr, ms, signed = self.post("/v1/turn", json.dumps(payload, ensure_ascii=False).encode(), **kw)
        if status == 200 and not signed:
            raise RelayError(status, "bad_response_sig", True)  # 중계가 답을 바꿨다 → 믿지 않는다
        if status != 200:
            try:
                err = json.loads(out)
            except ValueError:
                err = {"error": out[:80].decode(errors="replace"), "fallback": status >= 500}
            raise RelayError(status, err.get("error"), bool(err.get("fallback")) or status >= 500)
        d = json.loads(out)
        d["_ms"], d["_relay_ms"] = ms, hdr.get("X-Relay-Ms")
        return d

    def memo(self, note_id, text, ts="2026-01-01T00:00:00+00:00", device_id=None, **kw):
        """「기억해 줘」 메모(/v1/turn type=memory_note). (status, body dict, 응답 서명 일치)"""
        body = {"type": "memory_note", "id": note_id, "text": text, "ts": ts, "device_id": device_id or self.device_id}
        status, out, hdr, ms, signed = self.post("/v1/turn", json.dumps(body, ensure_ascii=False).encode(), **kw)
        try:
            d = json.loads(out)
        except ValueError:
            d = {"error": out[:80].decode(errors="replace")}
        return status, d, signed


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[2] == "memo":   # fake_device.py <base> memo <기기> <id> <내용>
        base, _, dev, nid, text = sys.argv[1:6]
        print(Device(base, dev, os.environ["DEVICE_SECRET"]).memo(nid, text))
        sys.exit()
    if len(sys.argv) == 5 and sys.argv[2] == "wav":    # fake_device.py <base> wav <기기> <in.wav> — StickS3 바깥 모드 흉내(/v1/ask — API 경로 버전, 기기 판 아님)
        base, _, dev, path = sys.argv[1:5]
        status, out, hdr, ms, signed = Device(base, dev, os.environ["DEVICE_SECRET"]).post(
            "/v1/ask", Path(path).read_bytes(), ctype="audio/wav")
        from urllib.parse import unquote
        print(json.dumps({"status": status, "signed": signed, "ms": ms, "relay_ms": hdr.get("X-Relay-Ms"),
                          "timing": hdr.get("X-Timing"), "emotion": hdr.get("X-Oakle-Emotion"),
                          "heard": unquote(hdr.get("X-Transcript", "")), "answer": unquote(hdr.get("X-Answer", "")),
                          "bytes": len(out), "ctype": hdr.get("Content-Type")}, ensure_ascii=False))
        sys.exit()
    base, dev, text = sys.argv[1:4]
    d = Device(base, dev, os.environ["DEVICE_SECRET"])
    print(json.dumps(d.turn({"type": "ask", "text": text, "tools": []}), ensure_ascii=False))
