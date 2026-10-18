# server/

| 파일 | 어디서 | 하는 일 |
|---|---|---|
| `voice_bridge.py` | 집 PC(macOS) | 집 모드 서버. WAV → whisper.cpp → OpenClaw 에이전트 → `say` → WAV |
| `relay/relay_server.py` | 인터넷의 작은 VM(nginx 뒤) | 바깥 모드 중계. 기기 요청을 커넥터에 넘긴다 |
| `relay/connector.py` | 집 PC | 중계에 밖으로 WebSocket 을 열고, 서명 확인 뒤 voice_bridge 의 STT·TTS 와 에이전트로 답한다 |
| `relay/relaysig.py`·`relay/wsmin.py` | 공용 | 서명 계산, 최소 WebSocket |
| `relay/fake_device.py` | 어디서나 | 기기 흉내(서명 포함) — 시험용 |

모두 **표준 라이브러리만** 쓴다. voice_bridge·커넥터는 Python 3.9+, 중계 서버는 3.6+.

## 집 모드 실행

필요한 것: macOS(`say`·`afconvert`), [whisper.cpp](https://github.com/ggml-org/whisper.cpp)(`brew install whisper-cpp`)와 모델 파일, [OpenClaw](https://github.com/openclaw/openclaw)(`openclaw agent` CLI).

```bash
cp server/.env.example server/.env        # BIND 를 이 PC 의 LAN IP 로
set -a; . server/.env; set +a
python3 server/voice_bridge.py add-device sticks3-1      # 기기 토큰 1회 출력 → config.h 의 DEVICE_TOKEN
python3 server/voice_bridge.py                           # 서버 시작
```

손으로 시험(기기 없이):

```bash
say -v Yuna -o /tmp/q.aiff "오늘 날씨 어때" && afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/q.aiff /tmp/q.wav
curl -s -o /tmp/a.wav -D - -H "Authorization: Bearer <토큰>" -H "X-Device-Id: sticks3-1" \
     -H "Content-Type: audio/wav" --data-binary @/tmp/q.wav http://<BIND>:8765/v1/ask
afplay /tmp/a.wav
```

계속 띄워 두려면 macOS LaunchAgent 로 등록한다(`relay/ai.oakle.relay-connector.plist.example` 과 같은 꼴). LaunchAgent 는 PATH 가 짧으니 `OPENCLAW_BIN` 을 절대경로로 준다.

## 기기 ↔ 서버 규약 (`POST /v1/ask`)

```
요청  Authorization: Bearer <기기 토큰>, X-Device-Id: sticks3-1, Content-Type: audio/wav (16 kHz 16-bit mono)
응답  200 audio/wav (16 kHz mono)
      X-Transcript / X-Answer  URL 인코딩(화면 표시용)
      X-Oakle-Emotion          대답 표정 20종 중 하나(faces/README.md)
      X-Timing                 "stt=…;llm=…;tts=…" (ms)
      X-Oakle-Action           {"ir":"lg_off"} 등, 있을 때만(docs/ir.md)
      204 말소리 없음 · 401 토큰 · 413 길이 · 429 하루 한도 · 502 단계 실패(본문 = stt|llm|tts)
GET /healthz → 200 ok
```

- 표정: 에이전트에게 답 앞에 `[표정:이름]` 표지를 붙이라고 지시하고, 서버가 떼어 헤더로 보낸다. 표지가 없으면 답 문장의 간단한 규칙으로 고른다.
- 기기 토큰은 sha256 해시만 sqlite(`STATE_DIR/home.sqlite3`)에 둔다. `revoke-device <id>` 로 폐기.
- 질의 원문은 로그에 남기지 않는다(`LOG_TEXT=1` 일 때만). 단계별 ms 는 sqlite `requests` 표.

## 비밀값

코드에 적지 않는다. `read_secret(환경변수, 파일명)` 은 환경변수를 먼저 보고, 없으면 `CRED_DIR/<파일명>`(기본 `~/.openclaw/credentials/`)을 읽는다. 못 읽으면 로그에 이름만 남기고 그 단계에서 502.

| 용도 | 환경변수 | 파일 | 언제 |
|---|---|---|---|
| OpenClaw Gateway | `OPENCLAW_GATEWAY_TOKEN` | `openclaw_gateway_token` | `LLM=http` 일 때만 |
| 커넥터 토큰 | `RELAY_CONNECTOR_TOKEN` | `oakle_relay_connector_token` | 커넥터 |
| 커넥터 토큰 해시 | `RELAY_CONNECTOR_SHA256` | `oakle_relay_connector_token.sha256` | 중계 서버 |
| 기기 서명 키 | — | `oakle_relay_device_keys.json` | 커넥터(`add-device` 가 씀) |
| (실험) Anthropic·STT·Google TTS | `ANTHROPIC_API_KEY`·`STT_API_KEY`·`GOOGLE_TTS_API_KEY` | `anthropic_voice_api_key`·`stt_api_key`·`google_tts_api_key` | `PROFILE=outdoor` 일 때만 |

## 바깥 모드 (중계)

규약·등록 절차는 [docs/relay-protocol.md](../docs/relay-protocol.md). 배포 예시: `relay/nginx-relay.conf.example`, `relay/oakle-relay.service.example`(systemd), `relay/ai.oakle.relay-connector.plist.example`(커넥터 LaunchAgent).

**권장: 바깥 모드에는 도구를 제한한 에이전트를 따로 둔다.** 커넥터의 `OAKLE_AGENT` 기본값은 `device` 다. OpenClaw 에서 `device` 에이전트를 만들고 도구를 최소(예: 웹 검색만)로 허용해 두면, 기기를 잃어버리거나 기기 비밀이 새도 집 PC 의 파일·셸 도구까지 닿지 않는다. 집 모드(`VOICE_AGENT`, 기본 `main`)는 LAN 안에서만 열리는 것을 전제로 한다.

## 시험

```bash
python3 server/test_voice_bridge.py      # 집 모드 HTTP 흉내(LLM·whisper·say 호출 0회)
python3 server/relay/test_local.py       # 중계·커넥터·가짜 기기 왕복(에이전트 호출 0회, 약 70초)
```
