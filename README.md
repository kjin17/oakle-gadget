# oakle gadget

M5Stack **StickS3** 를 손바닥 음성 리모컨으로 만드는 펌웨어와 서버. 버튼을 누르고 말하면 집 PC 의 AI 에이전트([OpenClaw](https://github.com/openclaw/openclaw))가 받아 적고 답하고, 답을 기기 스피커로 읽어 준다. 화면엔 표정과 자막이 뜨고, 「티비 꺼 줘」 같은 말엔 기기 머리의 IR 로 TV 전원을 쏜다.

> **English summary** — Firmware (Arduino + M5Unified) and a small Python server that turn an M5Stack StickS3 into a push-to-talk voice remote for a home AI agent (OpenClaw). Hold button A, speak, release: the device POSTs a 16 kHz WAV, the server transcribes it (whisper.cpp), asks the agent, synthesizes the answer (macOS `say`) and returns a WAV plus a facial-expression hint and an optional IR action (e.g. LG TV power). At home it talks to the server on the LAN; on a phone hotspot it goes through an optional internet relay with per-device HMAC request/response signing, so the relay can neither forge questions nor answers. Wi-Fi is set up from a phone via a captive portal. Standard-library Python only. Docs are in Korean. MIT licensed.

## 무엇이 되나

| 기능 | 설명 | 문서 |
|---|---|---|
| 눌러서 말하기 | A 를 누르는 동안 녹음(16 kHz, 최대 10초) → 떼면 서버로 → 답 음성 재생. 재생 중 A 를 다시 누르면 끊고 새로 말한다 | [server/README.md](server/README.md) |
| 표정·자막 | 세로 화면 위 120×120 표정 20종, 아래 알아들은 말과 답. B 짧게 = 자막 한 줄 내리기 | [faces/README.md](firmware/oakle_gadget/faces/README.md) |
| 집 / 바깥 자동 전환 | 「집」 표시 Wi-Fi 면 LAN 서버로, 아니면(휴대폰 핫스팟 등) 인터넷 중계로. 화면 맨 아래 「집/바깥/연결 없음」 | [docs/relay-protocol.md](docs/relay-protocol.md) |
| Wi-Fi 설정 모드 | 처음 켜거나 A+B 5초 → `Oakle-Gadget-XXXX` Wi-Fi + 설정 페이지에서 휴대폰으로 망 저장(최대 5개) | [docs/wifi-setup.md](docs/wifi-setup.md) |
| IR 리모컨 | 「티비 꺼 줘」 → LG TV NEC 코드 송신. B 3초 = 리모컨 신호 학습 | [docs/ir.md](docs/ir.md) |
| 흔들기 | 대기 중 빠르게 흔들면 어지러운 표정 + 「띠요요용」(네트워크 없음) | [docs/shake.md](docs/shake.md) |
| 절전 | 20초 뒤 화면 어둡게, 배터리일 때 5분 쉬면 딥슬립(A·B 로 깨움) | — |

## 구성

```
집 모드 (LAN)
 StickS3 ──HTTP POST wav──▶ 집 PC:8765 voice_bridge.py
                              ├ whisper.cpp (STT, 한국어)
                              ├ openclaw agent (LLM)
                              └ say → afconvert 16 kHz wav ──▶ StickS3 스피커

바깥 모드 (인터넷, 선택)
 StickS3 ──HTTPS POST wav + 서명──▶ 중계 서버(VM, nginx) relay_server.py
                                        ▲ WebSocket (집 PC 가 밖으로 연다 — 집 포트 안 엶)
                    집 PC connector.py ─┘ 서명 확인 → STT → 제한 에이전트 → TTS → 응답 서명
```

## 하드웨어

- **M5Stack StickS3 (K150)** — ESP32-S3-PICO-1-N8R8, 1.14″ 135×240 LCD, ES8311 코덱 + MEMS 마이크 + 스피커, BMI270 IMU, IR 송·수신, 250 mAh 배터리.
- 핀은 직접 다루지 않는다. `M5.begin()`(M5Unified)이 PMIC·앰프·LCD 전원을 잡는다. 펌웨어가 직접 쓰는 건 IR(TX G46 / RX G42), 버튼(G11 / G12), PMIC 레지스터 몇 개뿐이다.
- 회로도 V0.6 신호 이름 기준 핀맵과 함정(앰프 enable 이 PMIC 에 있음 등): [docs/hardware.md](docs/hardware.md).

## 펌웨어 빌드·굽기

필요한 것: [arduino-cli](https://arduino.github.io/arduino-cli/), M5Stack 보드 패키지(`m5stack:esp32` 3.3.x), 라이브러리 M5Unified·M5GFX.

```bash
arduino-cli config add board_manager.additional_urls https://static-cdn.m5stack.com/resource/arduino/package_m5stack_index.json
arduino-cli core update-index && arduino-cli core install m5stack:esp32
arduino-cli lib install M5Unified M5GFX

cp firmware/oakle_gadget/config.h.example firmware/oakle_gadget/config.h   # 값 채우기(아래)
arduino-cli compile --fqbn m5stack:esp32:m5stack_sticks3 firmware/oakle_gadget
arduino-cli upload  --fqbn m5stack:esp32:m5stack_sticks3 -p <포트> firmware/oakle_gadget
```

- 포트가 안 보이면 전원 버튼을 길게 눌러 다운로드 모드로 들어간다. 출하 펌웨어를 남겨 두려면 굽기 전에 8 MB 를 백업한다([docs/hardware.md](docs/hardware.md)).
- 표정 그림은 리포에 없다. 없으면 도형 얼굴로 그린다. 자기 그림을 넣는 법: [faces/README.md](firmware/oakle_gadget/faces/README.md).
- 시리얼(115200)에 부팅 한 줄 `boot: wifi=ok route=home …`, 질의마다 `timing ms: …` 가 찍힌다.

### 설정 (`config.h`)

| 값 | 뜻 |
|---|---|
| `WIFI_SSID`·`WIFI_PASS` | 집 Wi-Fi 첫 시드(비워 두면 설정 모드에서 휴대폰으로 넣는다) |
| `DEVICE_ID`·`DEVICE_TOKEN` | `voice_bridge.py add-device <id>` 가 준 토큰 |
| `SERVER_URL` | 집 PC 의 `http://<LAN IP>:8765/v1/ask` |
| `RELAY_SECRET`·`RELAY_URL` | (선택) 바깥 모드. 빼면 집 전용 펌웨어 |
| `IR_LG_DISCRETE`·`IR_USE_LEARNED` | IR 동작([docs/ir.md](docs/ir.md)) |

전체 목록과 설명은 [config.h.example](firmware/oakle_gadget/config.h.example). `config.h` 는 `.gitignore` 대상이다.

## 서버 실행

```bash
cp server/.env.example server/.env          # BIND = 이 PC 의 LAN IP
set -a; . server/.env; set +a
python3 server/voice_bridge.py add-device sticks3-1    # 토큰 1회 출력 → config.h DEVICE_TOKEN
python3 server/voice_bridge.py
```

필요한 것: macOS(`say`), whisper.cpp + 모델, OpenClaw. 자세한 규약·비밀값·바깥 모드 중계 배포는 [server/README.md](server/README.md).

## 시험

```bash
python3 server/test_voice_bridge.py                     # 집 모드 HTTP 흉내 15개
python3 server/relay/test_local.py                      # 중계·커넥터·가짜 기기 왕복 31개(약 70초)
c++ -std=c++17 -Wall -I firmware/oakle_gadget tools/test_wifi_list.cpp -o /tmp/t1 && /tmp/t1    # 53개
c++ -std=c++17 -Wall -I firmware/oakle_gadget tools/test_shake.cpp     -o /tmp/t2 && /tmp/t2    # 37개
```

## 폴더

```
firmware/oakle_gadget/   Arduino 스케치 (oakle_gadget.ino, wifi_list.h, shake_detect.h, relay_ca.h, config.h.example)
server/                  voice_bridge.py(집 모드), relay/(바깥 모드 중계·커넥터), 시험
tools/                   표정 데이터 생성기, 펌웨어 헤더 호스트 시험
docs/                    하드웨어·Wi-Fi 설정·IR·흔들기·중계 규약
```

## 보안 메모

- 집 모드는 LAN 평문 HTTP 다. `BIND` 를 LAN IP 로 두고 인터넷에 열지 않는다. 집 안에서는 버튼을 누른 누구나 같은 권한으로 에이전트를 부른다.
- 바깥 모드는 도구를 제한한 별도 에이전트를 권한다(`OAKLE_AGENT`, [server/README.md](server/README.md)).
- Wi-Fi 비밀번호·기기 토큰은 기기 NVS·바이너리에 평문으로 있다. 기기를 잃어버리면 서버에서 `revoke-device` 로 폐기한다.

## 라이선스

[MIT](LICENSE). M5Unified·M5GFX·Arduino-ESP32 등 의존 라이브러리는 각자의 라이선스를 따른다. `relay_ca.h` 의 ISRG 루트 인증서는 공개 인증서다.
