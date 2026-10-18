# 중계 규약 (바깥 모드)

집 밖(휴대폰 핫스팟 등)에서 기기가 집 PC 의 에이전트에 닿는 길이다. 집 공유기 포트는 열지 않는다.

```
기기 ──HTTPS POST /v1/ask (WAV) · /v1/turn (JSON) + 서명──▶ 중계 서버(relay_server.py, nginx 뒤)
                                                              ▲ WebSocket /v1/connect (집 PC 가 밖으로 연다)
                                          집 PC connector.py ─┘ 서명 확인 → STT → 에이전트 → TTS → 응답 서명
```

- 중계 서버는 인터넷에 있는 작은 VM 하나면 된다(표준 라이브러리 Python 3.6+). 기기 auth 값의 **해시**, 커넥터 토큰의 **해시**, 한도 계수, 본문 없는 접근 기록만 갖는다.
- 커넥터는 집 PC 에서 돈다. 기기 서명 키와 OpenClaw 를 갖는다.

## 1. 기기 비밀과 두 값

기기 비밀 S 는 `connector.py add-device <기기>` 가 한 번만 출력한다. 기기(`config.h` 의 `RELAY_SECRET`)에만 넣는다.

| 값 | 계산 | 누가 아나 |
|---|---|---|
| auth | hex(HMAC-SHA256(S, `"oakle-relay-auth"`)) | 기기. 중계 서버는 sha256(auth) 만 |
| sign_key | HMAC-SHA256(S, `"oakle-relay-sign"`) | 기기, 커넥터. 중계 서버는 모름 |

그래서 중계 서버가 털려도 ① 기기 행세로 에이전트에 질의를 넣을 수 없고 ② 가짜 답·가짜 도구 호출을 기기에 줄 수 없다. 막지 못하는 것: 본문 엿보기(TLS 는 nginx 에서 끝난다), 요청 버리기·지연.

## 2. 기기 → 중계 (HTTPS)

| 헤더 | 값 |
|---|---|
| `Authorization` | `Bearer <auth>` |
| `X-Device-Id` | `sticks3-1` 등 |
| `X-Oakle-Ts` | 유닉스 초. 중계는 ±300초, 커넥터는 ±120초까지 받는다(기기는 NTP 로 맞춘다) |
| `X-Oakle-Nonce` | 요청마다 새 난수(hex). 커넥터가 재전송을 거른다 |
| `X-Oakle-Sig` | hex(HMAC(sign_key, `"req\n{device}\n{ts}\n{nonce}\n{path}\n{sha256hex(body)}"`)) |

응답 헤더 `X-Oakle-Sig` = hex(HMAC(sign_key, `"resp\n{nonce}\n{status}\n{sha256hex(body)}"`)) — 요청 nonce 에 묶인다. 기기는 서명이 안 맞는 200 을 버린다. `X-Relay-Ms` 는 중계에서 잰 시간.

### `POST /v1/ask` (`audio/wav`, 16 kHz mono, 1 MB 이하)

응답은 집 모드 voice_bridge 와 같다: `200 audio/wav` + `X-Transcript`·`X-Answer`(URL 인코딩)·`X-Oakle-Emotion`·`X-Timing`. 말소리가 없으면 `204`.

### `POST /v1/turn` (JSON, 64 KB 이하 — 텍스트 클라이언트용)

```jsonc
{"type":"ask","text":"손전등 켜 줘","context":{"now":"…","memory":"…"},
 "tools":[{"name":"flashlight","description":"…","input_schema":{…}}]}       // 기기가 도구 목록의 정본
{"type":"tool_results","turn":"t-…","results":[{"id":"c-…","content":"손전등을 켰다","is_error":false}]}
// 응답
{"type":"final","turn":"t-…","text":"손전등 켰어요. [표정:ok]"}
{"type":"tool_calls","turn":"t-…","text":"","calls":[{"id":"c-…","name":"flashlight","input":{"on":true}}]}
```

- 한 턴의 도구 왕복은 최대 6번(`MAX_TOOL_ROUNDS`). 도구 왕복 중인 턴은 커넥터가 10분 들고 있다. 같은 `turn` 으로 결과를 두 번 보내면 `409 turn_unknown`.
- `{"type":"memory_note","id","text"(≤500자),"ts","device_id"}` 는 에이전트를 부르지 않고 커넥터가 `MEMORY_INBOX`(jsonl, 600)에 `status=pending` 한 줄을 덧붙인다. 하루 상한은 따로 센다(`MEMO_DAILY_LIMIT`). 대기열을 읽어 반영하는 일은 이 리포 밖에 둔다.

`GET /healthz` → `200 ok` + `X-Connector: up|down` (인증 없음).

## 3. 오류

본문은 `{"error": 코드, "fallback": bool}`. `fallback=true` 는 클라이언트가 자체 예비 경로로 넘어가도 된다는 뜻이다(StickS3 펌웨어엔 예비 경로가 없어 화면에 오류를 띄운다).

| 상태 | `error` | fallback | 누가 |
|---|---|---|---|
| 401 | `auth` | false | 중계 |
| 401 | `sig` | false | 커넥터(서명·시각·재전송) |
| 400 | `unsigned` / `bad_request` | false | 중계 / 커넥터 |
| 409 | `turn_unknown` | false | 커넥터 |
| 413 | `size` | false | 중계 |
| 429 | `rate`(분당) | false | 중계 |
| 429 | `limit`(하루) | true(메모는 false) | 중계 |
| 502 | `brain`(/v1/turn) · `stt`/`llm`/`tts`(/v1/ask) | true | 커넥터 |
| 503 | `connector_offline` / `connector_lost` | true | 중계 |
| 504 | `timeout` | true | 중계 |

## 4. 중계 ↔ 커넥터 (WebSocket 텍스트 프레임, JSON)

| 방향 | 프레임 |
|---|---|
| 커넥터 → 중계 | `{"t":"hello","v":1,"brain":"cli"}` |
| 중계 → 커넥터 | `{"t":"req","id":"17","path":"/v1/turn","device":"…","ts":…,"nonce":"…","sig":"…","ctype":"…","body_b64":"…"}` |
| 커넥터 → 중계 | `{"t":"resp","id":"17","status":200,"ctype":"…","headers":{…},"sig":"…","code":"","body_b64":"…"}` |
| 중계 → 커넥터 | `{"t":"cancel","id":"17"}` (중계가 기다리다 포기함. 지금은 기록만) |

- 커넥터는 하나만 붙는다. 새 커넥터가 인증하고 붙으면 옛 연결을 닫는다.
- 중계는 20초마다 ping 을 보낸다. 양쪽 모두 60초 동안 아무것도 안 오면 반쯤 죽은 소켓으로 보고 끊는다(커넥터는 1→60초 백오프로 다시 붙는다).

## 5. 등록 절차

```bash
# 집 PC (커넥터)
python3 server/relay/connector.py new-connector-token      # → 중계 서버에 둘 sha256 출력
python3 server/relay/connector.py add-device sticks3-1     # → 기기 비밀(한 번만) + 중계 서버에서 칠 명령 출력
# 중계 서버
echo <sha256> > $CRED_DIR/oakle_relay_connector_token.sha256
python3 relay_server.py add-device sticks3-1 <auth_sha256>
```

기기 비밀은 `config.h` 의 `RELAY_SECRET` 에만 넣는다. 어디에도 적어 두지 않는다.

## 6. 시험

`python3 server/relay/test_local.py` — 한 PC 에서 중계(127.0.0.1)·커넥터(`BRAIN=echo`)·가짜 기기를 띄워 31개 시나리오(서명·변조·재전송·한도·타임아웃·커넥터 사망과 재접속·로그에 본문 없음 등)를 돈다. 에이전트 호출 0회, 약 70초(분당 한도 창을 기다린다). `--real` 을 붙이면 실제 `openclaw agent` 로 도구 왕복을 한 번 더 한다.
