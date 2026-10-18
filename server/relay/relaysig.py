"""기기 비밀 1개에서 두 값을 만든다 — 중계 서버가 위조할 수 없게.

기기 비밀 S (발급 때 한 번만 출력, 기기에만 저장)
  auth     = hex(HMAC-SHA256(S, "oakle-relay-auth"))   → Authorization: Bearer 로 보냄. 중계 서버는 sha256(auth) 만 안다
  sign_key = HMAC-SHA256(S, "oakle-relay-sign")        → 커넥터만 안다(크리덴셜 파일). 중계 서버는 모른다

요청 서명:  X-Oakle-Sig = hex(HMAC(sign_key, "req\\n{device}\\n{ts}\\n{nonce}\\n{path}\\n{sha256hex(body)}"))
응답 서명:  X-Oakle-Sig = hex(HMAC(sign_key, "resp\\n{nonce}\\n{status}\\n{sha256hex(body)}"))   ← 요청 nonce 에 묶인다

그래서 중계 서버가 털려도 (1) 기기 행세로 에이전트에 질의를 넣을 수 없고 (2) 가짜 답·가짜 도구 호출을 기기에 줄 수 없다.
막지 못하는 것: 본문 엿보기(TLS 는 nginx 에서 끝난다), 요청 버리기·지연.
펌웨어 쪽 같은 계산: firmware/oakle_gadget/oakle_gadget.ino relay_init·relay_sig
"""
import hashlib
import hmac

AUTH_LABEL = b"oakle-relay-auth"
SIGN_LABEL = b"oakle-relay-sign"


def derive_auth(secret: str) -> str:
    return hmac.new(secret.encode(), AUTH_LABEL, hashlib.sha256).hexdigest()


def derive_sign_key(secret: str) -> bytes:
    return hmac.new(secret.encode(), SIGN_LABEL, hashlib.sha256).digest()


def sha256hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sign_request(sign_key: bytes, device_id, ts, nonce, path, body: bytes) -> str:
    msg = f"req\n{device_id}\n{ts}\n{nonce}\n{path}\n{sha256hex(body)}".encode()
    return hmac.new(sign_key, msg, hashlib.sha256).hexdigest()


def sign_response(sign_key: bytes, nonce, status, body: bytes) -> str:
    msg = f"resp\n{nonce}\n{status}\n{sha256hex(body)}".encode()
    return hmac.new(sign_key, msg, hashlib.sha256).hexdigest()


def equal(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())
