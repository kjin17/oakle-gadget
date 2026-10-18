// oakle gadget 펌웨어 — M5Stack StickS3 음성 리모컨. 붙은 Wi-Fi 로 집 모드·바깥 모드를 고른다.
// Arduino-ESP32(m5stack 코어 3.3.x) + M5Unified.
//
// Wi-Fi: 망은 NVS 「wifi」에 최대 5개, 항목마다 「집」 표시(wifi_list.h). config.h 의 WIFI_SSID(집)·HOTSPOT_SSID(바깥)는
//   첫 시드로만 들어가고 그 뒤엔 NVS 가 우선. 저장 망이 없거나 부팅 때 전부 실패하면 설정 모드: SoftAP 「Oakle-Gadget-XXXX」 +
//   캡티브 포털(192.168.4.1)에서 주변 망을 골라 저장 → 재부팅. 다시 들어가기 = A+B 함께 5초, 또는 B 를 누른 채 켜기(2초).
// 경로: 부팅·재연결 때 스캔해서 보이는 저장 망 중 「집」 표시 망이 있으면 집, 아니면 바깥(집 우선).
//   집 모드  : SERVER_URL(같은 LAN 의 server/voice_bridge.py, 평문 HTTP) + DEVICE_TOKEN
//   바깥 모드: RELAY_URL(인터넷의 중계 서버 https, ISRG 루트 relay_ca.h) + 중계 서명 규약(docs/relay-protocol.md).
//          RELAY_SECRET 에서 auth = HMAC(S,"oakle-relay-auth")·sign_key = HMAC(S,"oakle-relay-sign"), 요청·응답 서명, 시각은 NTP.
//          응답 서명이 안 맞으면 답을 버린다(중계 서버가 답을 바꿨을 수 있다).
//   화면 맨 아래 줄에 배터리와 「집」/「바깥」/「연결 없음」.
//
// 동작: A 버튼을 누르는 동안 녹음(16 kHz mono, 최대 MAX_SEC초, PSRAM)
//       → 떼면 WAV 를 POST /v1/ask → 받은 WAV 재생. 화면엔 상태, 받아 적은 말, 답 앞부분.
//   ※ /v1/ask 의 v1 은 서버 API 경로 버전이다 — 바꾸면 서명·중계가 깨진다.
//       재생 중 A 를 다시 누르면 재생을 끊고 바로 새로 녹음한다.
// 핀은 직접 다루지 않는다. M5.begin() 이 StickS3 의 PMIC(M5PM1)·앰프·LCD 전원을 잡는다 [근거: M5Unified board_M5StickS3 분기].
// 예외: 절전·IR 전원용으로 PMIC 레지스터 몇 개를 직접 쓴다(pmic_bit). 값은 docs/hardware.md 표.
// 마이크와 스피커는 같은 I2S 핀을 번갈아 쓴다 → 반드시 한쪽을 end() 한 뒤 다른 쪽 begin() [근거: M5Unified 구조].
//
// 시리얼에는 질의마다 한 줄씩 단계별 지연을 남긴다:
//   timing ms: route=home|outdoor relay=… held=… post=+… hdr=+… body=+… play=+… | server stt=…;llm=…;tts=…
//   (+값은 버튼을 뗀 순간 기준)
//
// 설정: config.h.example 을 config.h 로 복사해 채운다 (config.h 는 git 제외). Wi-Fi 칸은 비워 둬도 된다(설정 모드에서 넣는다).
// 버튼: A 누르는 동안 = 녹음 / B 짧게 = 자막 넘기기 / B 3초 = IR 학습 / A+B 5초 = Wi-Fi 설정 모드 / B 누른 채 켜기(2초) = 설정 모드
//       설정 모드 안: A = 나가기, B 5초 = 저장 망 모두 지우기
// 흔들기: 대기 중 빠르게 왕복 두세 번 흔들면 어지러운 표정 + 「띠요요용」, 5초 쉼(SHAKE_* 상수, shake_detect.h)
//
// 표정: 세로 화면(135×240) 위쪽 120×120 에 표정, 아래에 상태·글자. 그림 데이터 faces/faces_data.h 는 리포에 없다
//       (tools/make_face_assets.py 로 만든다, faces/README.md). 없으면 도형 얼굴로 대신 그린다.
//       표정 이름은 server/voice_bridge.py 의 EMOTIONS 20종(joy … mindblown)과 같다.

#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <WebServer.h>
#include <DNSServer.h>
#include "esp_sleep.h"
#include <Preferences.h>
#include <time.h>
#include "mbedtls/md.h"
#include "config.h"   // WIFI_SSID, WIFI_PASS, SERVER_URL, DEVICE_ID, DEVICE_TOKEN, (HOTSPOT_SSID, HOTSPOT_PASS, RELAY_SECRET, RELAY_URL)
#include "relay_ca.h" // RELAY_ROOT_CA — ISRG Root X1·X2 (공개 인증서)
#include "wifi_list.h" // 저장 망 목록·붙는 순서·설정 페이지(호스트 시험 tools/test_wifi_list.cpp)
#include "shake_detect.h" // 흔들기 감지(호스트 시험 tools/test_shake.cpp)
#if __has_include("faces/faces_data.h")
#include "faces/faces_data.h"  // FACE_TABLE[FACE_COUNT] = {이름, RGB565 120×120}
#define HAVE_FACES 1
#else
#define HAVE_FACES 0
#endif

// ---- 조절값 (config.h 에서 덮어쓸 수 있다) ----
#ifndef MAX_SEC
#define MAX_SEC 10
#endif
#ifndef TAIL_TRIM_MS
#define TAIL_TRIM_MS 120         // 끝부분을 버린다: 버튼 떼는 딸깍 소리가 STT 에 「.」나 잡음 단어로 들어가지 않게
#endif
#ifndef MIN_HOLD_MS
#define MIN_HOLD_MS 400          // 이보다 짧게 누른 건 말이 아니라 실수로 본다
#endif
#ifndef DIM_AFTER_S
#define DIM_AFTER_S 20           // 손 안 대면 화면을 어둡게
#endif
#ifndef SLEEP_AFTER_S
#define SLEEP_AFTER_S 300        // 배터리일 때 이만큼 놀면 딥슬립. 0 = 끔. 버튼 A/B 로 깬다
#endif
#ifndef MIC_PGA_DB
#define MIC_PGA_DB -1            // -1 = M5Unified 기본(아날로그 PGA 최소 + 디지털 볼륨 최대). 0~30(3 dB 단위)이면 아날로그로 키우고 디지털은 0 dB.
                                 //  Muse 기본은 아날로그 30 dB [근거: Muse SDK muse_settings.c]. 어느 쪽이 나은지는 실기 비교로 정한다
#endif
#ifndef SCREEN_ROT
#define SCREEN_ROT 0             // 세로. 0 = 버튼 A 가 화면 아래 [추론: StickCPlus2 와 같은 패널 설정, 실기 확인], 거꾸로면 2
#endif
#ifndef VOL_USB
#define VOL_USB 255              // USB 전원일 때 스피커 볼륨(0~255)
#endif
#ifndef VOL_BATT
#define VOL_BATT 185             // 배터리일 때. 75%(≈191) 넘으면 순간 전류로 재부팅될 수 있다 [근거: M5 StickS3 문서]
#endif
#ifndef IR_TX_PIN
#define IR_TX_PIN 46             // StickS3 IR_TX = G46 [근거: docs.m5stack.com/en/core/StickS3 PinMap]
#endif
#ifndef IR_LG_DISCRETE
#define IR_LG_DISCRETE 0         // 0 = 켜기·끄기·전원 모두 토글 0x20DF10EF(대부분의 LG TV 가 받는다), 1 = 전용 켜기·끄기 코드
#endif
#ifndef IR_USE_LEARNED
#define IR_USE_LEARNED 0         // 1 이면 NVS ir/tv_power 학습 신호를 기본 코드보다 먼저 쏜다. 학습 신호엔 앞 잡음·끊긴 헤더가 섞일 수 있어 기본은 끈다
#endif
#ifndef IR_CARRIER_LEVEL
#define IR_CARRIER_LEVEL false   // rmtSetCarrier 셋째 인자(= polarity_active_low). false = 반송파를 mark 에. M5 공식 StickS3 예제와 같음 — 아래 ir_tx_init 주석
#endif
#ifndef IR_RX_PIN
#define IR_RX_PIN 42             // StickS3 IR_RX = G42 [근거: docs.m5stack.com/en/core/StickS3 PinMap]
#endif
#ifndef IR_LEARN_S
#define IR_LEARN_S 15            // 학습 모드에서 리모컨 신호를 기다리는 시간
#endif
#ifndef IR_REPEAT
#define IR_REPEAT 2              // 학습 신호 재생 횟수(1~3). NEC 면 2회째부터는 반복 코드(누르고 있을 때와 같다), 아니면 프레임을 통째로 다시
#endif
#if defined(HOTSPOT_SSID) && !defined(RELAY_SECRET)
#error "HOTSPOT_SSID 를 넣었으면 RELAY_SECRET(connector add-device 로 발급한 기기 비밀)도 넣어야 해요"
#endif
// 바깥 모드는 기기 비밀이 있어야 쓸 수 있다. 핫스팟 이름은 포털로도 넣으므로 RELAY_SECRET 으로 판정
#ifdef RELAY_SECRET
#define HAVE_OUTDOOR 1
#ifndef RELAY_URL
#error "RELAY_SECRET 을 넣었으면 RELAY_URL(중계 서버 https://<도메인>/v1/ask)도 넣어야 해요"
#endif
#else
#define HAVE_OUTDOOR 0
#ifndef RELAY_URL
#define RELAY_URL ""
#endif
#endif
// Wi-Fi 시드: 없어도 빌드된다 — 포털에서 넣는다
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif
#if defined(HOTSPOT_SSID) && !defined(HOTSPOT_PASS)
#define HOTSPOT_PASS ""
#endif
#ifndef PORTAL_TIMEOUT_S
#define PORTAL_TIMEOUT_S 180     // 설정 모드에서 휴대폰이 안 붙은 채 이만큼 지나면 나와서 평소대로(저장 망이 하나도 없으면 안 나온다)
#endif
#ifndef PORTAL_COMBO_MS
#define PORTAL_COMBO_MS 5000     // A+B 를 함께 이만큼 누르면 설정 모드
#endif
#ifndef PORTAL_BOOT_HOLD_MS
#define PORTAL_BOOT_HOLD_MS 2000 // B 를 누른 채 켜서 이만큼 계속 누르면 설정 모드
#endif
#ifndef PORTAL_WIPE_MS
#define PORTAL_WIPE_MS 5000      // 설정 모드에서 B 를 이만큼 누르면 저장 망 모두 지우기
#endif
// PORTAL_PASS: 설정 Wi-Fi 비밀번호(8자 이상). 기본은 열린 망 — 설정 모드는 첫 부팅·실패·버튼 때만 잠깐 열린다
#ifndef PREFER_OUTDOOR
#define PREFER_OUTDOOR 0         // 1 = 핫스팟이 보이면 집보다 먼저(집에서 바깥 경로 시험용)
#endif
#ifndef WIFI_WAIT_MS
#define WIFI_WAIT_MS 8000        // 한 SSID 에 붙기를 기다리는 시간
#endif
#ifndef PMIC_LED_OFF
#define PMIC_LED_OFF 1           // PMIC 녹색 LED 는 기본으로 늘 켜져 1 mA 남짓 먹는다 [근거: Muse SDK 보드 코드 주석]
#endif
// 흔들기: 대기 중 빠르게 흔들면 오클이 어지러워한다. IMU = BMI270(내부 I2C 0x68, ±8 g·100 Hz, M5Unified 기본)
#ifndef SHAKE_ENABLE
#define SHAKE_ENABLE 1           // 0 = 끔
#endif
#ifndef SHAKE_G
#define SHAKE_G 1.8f             // 봉우리 문턱: 중력 뺀 가속도 크기(g). 오작동하면 올리고, 잘 안 먹으면 내린다(걷기 손 0.3~0.6 g)
#endif
#ifndef SHAKE_PEAKS
#define SHAKE_PEAKS 5            // 방향이 번갈아 바뀐 봉우리 수. 5 = 왕복 두 번 반(「흔들흔들흔」)
#endif
#ifndef SHAKE_WINDOW_MS
#define SHAKE_WINDOW_MS 1500     // 그 봉우리들이 이 안에
#endif
#ifndef SHAKE_GAP_MS
#define SHAKE_GAP_MS 400         // 봉우리 사이 최대. 이보다 느리게(1 Hz 남짓) 흔들면 안 센다 — 걷기 디딤(≈500 ms)을 거른다
#endif
#ifndef SHAKE_COOLDOWN_S
#define SHAKE_COOLDOWN_S 5       // 한 번 반응한 뒤 쉬는 시간
#endif
#ifndef FACE_DIZZY
#define FACE_DIZZY "mindblown"   // 어지러운 표정(20종 중 @_@ 에 가장 가까운 것. 도형 얼굴은 X 눈)
#endif

static const uint32_t RATE = 16000;
static const size_t CHUNK = 1600;                 // 100 ms
static int16_t* pcm = nullptr;                    // PSRAM
static size_t pcm_len = 0;                        // samples
static uint32_t t_press, t_release;               // millis
static uint32_t last_touch;                       // 마지막으로 사람이 손댄 시각
static bool dimmed = false;
static uint32_t wifi_next_try = 0, wifi_backoff = 1000;

// ---- 경로: 붙은 SSID 로 정한다 ----
enum Route { R_NONE, R_HOME, R_OUT };
static Route route = R_NONE;
static const char* route_name(Route r) { return r == R_HOME ? "home" : r == R_OUT ? "outdoor" : "none"; }
static const char* route_label(Route r) { return r == R_HOME ? "집" : r == R_OUT ? "바깥" : "연결 없음"; }
// 저장 망 목록(NVS 「wifi」). 집/바깥은 항목의 「집」 표시로 정한다 — config.h 시드는 WIFI_SSID 가 집, HOTSPOT_SSID 가 바깥으로 들어간다
static wl::List nets;
static Route route_of_ssid(const String& s) {
  int r = wl::route_of(nets, s.c_str(), HAVE_OUTDOOR);
  return r == 1 ? R_HOME : r == 2 ? R_OUT : R_NONE;
}

// ---- 중계 서명 (server/relay/relaysig.py 와 같은 계산) ----
static String relay_auth;                          // hex(HMAC(S, "oakle-relay-auth")) → Authorization: Bearer
static uint8_t relay_key[32];                      // HMAC(S, "oakle-relay-sign") — 요청·응답 서명 키

static String hexs(const uint8_t* b, size_t n) {
  static const char* H = "0123456789abcdef";
  String o; o.reserve(n * 2);
  for (size_t i = 0; i < n; i++) { o += H[b[i] >> 4]; o += H[b[i] & 15]; }
  return o;
}
static void hmac256(const uint8_t* k, size_t kn, const uint8_t* m, size_t mn, uint8_t out[32]) {
  mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), k, kn, m, mn, out);
}
static String sha256hex(const uint8_t* d, size_t n) {
  uint8_t h[32];
  mbedtls_md(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), d, n, h);
  return hexs(h, 32);
}
static String relay_sig(const String& msg) {
  uint8_t h[32];
  hmac256(relay_key, 32, (const uint8_t*)msg.c_str(), msg.length(), h);
  return hexs(h, 32);
}
static void relay_init() {
#if HAVE_OUTDOOR
  const char* s = RELAY_SECRET;
  uint8_t h[32];
  hmac256((const uint8_t*)s, strlen(s), (const uint8_t*)"oakle-relay-auth", 16, h);
  relay_auth = hexs(h, 32);
  hmac256((const uint8_t*)s, strlen(s), (const uint8_t*)"oakle-relay-sign", 16, relay_key);
#endif
}

// 서명은 시각에 묶인다(중계 ±300초, 커넥터 ±120초). 부팅 때 configTime 으로 NTP 를 켜 두고, 쓰기 전에 맞았는지 본다
static bool clock_ok(uint32_t wait_ms) {
  uint32_t t0 = millis();
  while (time(nullptr) < 1700000000 && millis() - t0 < wait_ms) delay(100);
  return time(nullptr) >= 1700000000;
}

// ---- PMIC(M5PM1, 0x6E) 직접 쓰기: 절전에만 쓴다 ----
// PMIC 는 작은 MCU 라 I2C 를 가끔 놓친다 → 세 번까지 다시 한다 [근거: Muse SDK board_m5stack_sticks3.c 주석] / 실기 미확인
static const uint8_t PM1 = 0x6E;
static const uint8_t PM1_PWR_CFG = 0x06;          // bit3 = 5V 출력, bit4 = 녹색 LED
static const uint8_t PM1_GPIO_OUT = 0x11;         // bit2 = L3B(LCD+오디오 전원), bit3 = 앰프
static const uint8_t PM1_VIN_MV_L = 0x24;         // USB 입력 전압, 16bit LE mV

static bool pmic_bit(uint8_t reg, uint8_t mask, bool on) {
  for (int i = 0; i < 3; i++) {
    bool ok = on ? M5.In_I2C.bitOn(PM1, reg, mask, 100000) : M5.In_I2C.bitOff(PM1, reg, mask, 100000);
    if (ok) return true;
    delay(2);
  }
  return false;
}

static int vin_mv() {                              // USB 입력 전압. 못 읽으면 -1
  uint8_t b[2] = {0, 0};
  if (!M5.In_I2C.readRegister(PM1, PM1_VIN_MV_L, b, 2, 100000)) return -1;
  return b[0] | (b[1] << 8);
}

static bool on_usb() {
  int mv = vin_mv();
  return mv < 0 || mv > 4000;                      // 못 읽으면 잠들지 않는 쪽으로
}

// 볼륨은 반대로 못 읽으면 배터리로 본다(크게 틀었다 재부팅되는 쪽이 더 나쁘다)
static uint8_t speaker_volume() {
  int mv = vin_mv();
  return mv > 4000 ? VOL_USB : VOL_BATT;
}

// ---- 적외선(IR) 송신: LG TV 전원. NEC 38 kHz, ESP32-S3 RMT ----
// IR LED 전원은 EXT_5V 에 달려 있고 M5Unified 기본값은 꺼짐 → 쏘는 동안만 setExtOutput(true) [근거: M5 StickS3 문서 EXT_5V_EN]
// 앰프 간섭: 문서는 「수신」 때만 앰프를 끄라 한다. 송신은 스피커를 begin 하기 전(녹음 뒤 end 된 상태)에 해서 겹치지 않는다
// LG 코드(32bit, 첫 비트부터 MSB 순, 주소 0x04): 전원 토글 0x20DF10EF, 켜기 0x20DF23DC, 끄기 0x20DFA35C — 널리 공개된 LG TV NEC 코드다.
// 모델에 따라 전용 켜기·끄기가 안 먹을 수 있어 기본은 토글이다(꺼진 TV 에 「끄기」를 보내면 켜진다). 실기에서 토글 0x20DF10EF 로 확인했다
static const uint32_t LG_POWER = 0x20DF10EF, LG_ON = 0x20DF23DC, LG_OFF = 0x20DFA35C;
// 선풍기·서큘레이터 전원 토글 예시 0x00FF00FF(NEC 주소 0x00·명령 0x00). 값싼 범용 IR 리모컨에 흔한 코드라 예시로 둔다 — 기기마다 다르니
// 자기 기기 코드는 IR 학습(B 3초) 시리얼 출력으로 읽어 바꾼다. 켜기·끄기 구분 없음.
// 반복 코드 없이 프레임 한 번만 쏜다(선풍기가 반복을 「길게 누름」으로 볼 수 있다 [추론])
static const uint32_t CIRC_POWER = 0x00FF00FF;
static bool ir_ready = false;

static bool ir_tx_init() {
  if (!ir_ready) {
    // 코어 3.3.9 esp32-hal-rmt.c 는 셋째 인자를 flags.polarity_active_low 로 넘긴다(헤더 주석과 반대) [근거: 코어 소스 214행].
    // M5 공식 StickS3 NEC 예제도 polarity_active_low=false·level 1=mark·duty 0.33 [근거: docs.m5stack.com/en/arduino/m5sticks3/ir_nec].
    // true 로 두면 반송파가 space 에 실려 TV 가 거의 반응하지 않는다(실기: 어떤 코드로도 대부분 무반응)
    ir_ready = rmtInit(IR_TX_PIN, RMT_TX_MODE, RMT_MEM_NUM_BLOCKS_1, 1000000)    // 1 tick = 1 us
               && rmtSetCarrier(IR_TX_PIN, true, IR_CARRIER_LEVEL, 38000, 0.33);
    if (!ir_ready) Serial.println("ir: rmt init failed");
  }
  return ir_ready;
}

// IR LED 전원(EXT_5V = PMIC 0x06 bit3). M5.Power.setExtOutput 은 결과를 안 돌려주고 PMIC 는 I2C 를 가끔 놓친다 → 다시 해 보고 결과를 남긴다
static bool ir_power(bool on) {
  bool ok = pmic_bit(PM1_PWR_CFG, 0x08, on);
  if (!ok) Serial.printf("ir: ext5v %s FAIL\n", on ? "on" : "off");
  if (on) delay(100);                                                            // 5V 승압이 올라올 때까지. M5 공식 예제가 setExtOutput 뒤 100 ms
  return ok;
}

static bool ir_send_nec(uint32_t code, bool repeat = true) {
  if (!ir_tx_init()) return false;
  rmt_data_t f[34], rep[2];
  f[0] = {{9000, 1, 4500, 0}};                                                   // 머리
  for (int i = 0; i < 32; i++) {
    bool one = code & (1UL << (31 - i));
    f[1 + i] = {{560, 1, (uint32_t)(one ? 1690 : 560), 0}};
  }
  f[33] = {{560, 1, 100, 0}};                                                    // 끝 표시
  rep[0] = {{9000, 1, 2250, 0}};                                                 // NEC 반복 코드(리모컨을 누르고 있을 때와 같다)
  rep[1] = {{560, 1, 100, 0}};
  bool pw = ir_power(true);
  bool ok = rmtWrite(IR_TX_PIN, f, 34, 200);
  if (repeat) {
    delay(40);                                                                   // 프레임 주기 108 ms 근처
    ok = rmtWrite(IR_TX_PIN, rep, 2, 100) && ok;
  }
  delay(5);
  ir_power(false);
  Serial.printf("ir: nec 0x%08lX ext5v=%s %s\n", (unsigned long)code, pw ? "ok" : "FAIL", ok ? "sent" : "FAIL");
  return ok;
}

// ---- 학습한 신호: 진짜 리모컨 전원 버튼을 G42 로 받아 NVS 「ir/tv_power」에 원시 타이밍으로 둔다 ----
// 형식: uint16 us 배열, 켜짐(mark)·꺼짐(space) 번갈아, mark 부터. 끝 space 는 0 일 수 있다. NEC 값은 저장하지 않고 부팅 때 원시에서 다시 푼다(앞 잡음은 ir_trim 이 뗀다)
static const size_t IR_RAW_MAX = 190;
static uint16_t ir_raw[IR_RAW_MAX];
static size_t ir_raw_n = 0;                        // 0 = 학습한 신호 없음 → 기본 LG 코드
static uint32_t ir_raw_nec = 0;                    // 0 = NEC 로 안 풀림
static bool ir_raw_hdr45 = false;                  // 헤더 4.5 ms(삼성형). 반복 코드가 없어 프레임을 통째로 다시 쏜다
static size_t ir_trim(uint16_t* d, size_t n);
static bool nec_decode(const uint16_t* d, size_t n, uint32_t* out, bool* hdr45);

static void ir_load_learned() {
  Preferences p;
  if (!p.begin("ir", true)) { ir_raw_n = 0; return; }   // 처음엔 이름공간이 없다
  size_t len = p.getBytesLength("tv_power");
  ir_raw_n = (len >= 2 * 4 && len <= sizeof ir_raw) ? p.getBytes("tv_power", ir_raw, len) / 2 : 0;
  p.end();
  ir_raw_n = ir_trim(ir_raw, ir_raw_n);           // 앞 잡음째 저장된 것도 여기서 떼고 다시 푼다
  if (!nec_decode(ir_raw, ir_raw_n, &ir_raw_nec, &ir_raw_hdr45)) { ir_raw_nec = 0; ir_raw_hdr45 = false; }
}

// 원시 재생. NEC 로 풀린 신호면 2회째부터 NEC 반복 코드, 아니면 프레임을 40 ms 쉬고 통째로 다시
// (NEC 프레임을 통째로 두 번 쏘면 토글 버튼이 두 번 눌린 걸로 보일 수 있다 [추론])
static bool ir_send_raw(int times) {
  if (!ir_tx_init()) return false;
  static rmt_data_t f[IR_RAW_MAX / 2 + 1];
  size_t ns = 0;
  for (size_t i = 0; i < ir_raw_n; i += 2) {
    uint32_t mk = min<uint32_t>(ir_raw[i], 32767);
    uint32_t sp = i + 1 < ir_raw_n ? min<uint32_t>(ir_raw[i + 1], 32767) : 0;
    f[ns++] = {{mk, 1, (uint32_t)(sp ? sp : 100), 0}};                           // 끝 space 0 은 RMT 끝 표시라 100 us 로
  }
  rmt_data_t rep[2] = {{{9000, 1, 2250, 0}}, {{560, 1, 100, 0}}};
  ir_power(true);
  bool ok = rmtWrite(IR_TX_PIN, f, ns, 300);
  for (int k = 1; k < times; k++) {
    if (ir_raw_nec && !ir_raw_hdr45) { delay(40); ok = rmtWrite(IR_TX_PIN, rep, 2, 100) && ok; }
    else            { delay(40); ok = rmtWrite(IR_TX_PIN, f, ns, 300) && ok; }
  }
  delay(5);
  ir_power(false);
  Serial.printf("ir: send learned proto=%s value=0x%08lX raw=%u x%d %s\n", !ir_raw_nec ? "RAW" : ir_raw_hdr45 ? "NEC45" : "NEC",
                (unsigned long)ir_raw_nec, (unsigned)ir_raw_n, times, ok ? "sent" : "FAIL");
  return ok;
}

// 서버 X-Oakle-Action({"ir":"lg_off"}) → 화면에 쓸 이름(「TV 끄기」). 없거나 모르는 이름이면 nullptr
// IR_USE_LEARNED=1 이고 학습한 신호가 있으면 TV 켜기·끄기·전원 모두 그 신호(리모컨 전원 버튼 = 토글)를 쏜다. 기본은 LG_POWER 토글
// 서큘레이터(circ_power)는 학습 신호와 무관하게 늘 기본 코드
static bool ir_used_learned = false;
static const char* do_action(const String& a) {
  if (a.indexOf("\"ir\"") < 0) return nullptr;
  if (a.indexOf("\"circ_power\"") >= 0) {
    ir_used_learned = false;
    return ir_send_nec(CIRC_POWER, false) ? "서큘레이터 전원" : "실패";
  }
  uint32_t code; const char* label;
  if (a.indexOf("\"lg_on\"") >= 0)       { code = IR_LG_DISCRETE ? LG_ON : LG_POWER;  label = "TV 켜기"; }
  else if (a.indexOf("\"lg_off\"") >= 0) { code = IR_LG_DISCRETE ? LG_OFF : LG_POWER; label = "TV 끄기"; }
  else if (a.indexOf("\"lg_power\"") >= 0) { code = LG_POWER; label = "TV 전원"; }
  else return nullptr;
  ir_used_learned = IR_USE_LEARNED && ir_raw_n > 0;
  bool ok = ir_used_learned ? ir_send_raw(constrain(IR_REPEAT, 1, 3)) : ir_send_nec(code);
  return ok ? label : "실패";
}

// ---- 화면(세로 135×240): 위 표정(FACE_Y..+120), 그 아래 상태 한 줄, 글자 칸, 맨 아래 배터리 ----
static const int SCR_W = 135, SCR_H = 240;
static const int FACE_S = 120, FACE_X = (SCR_W - FACE_S) / 2, FACE_Y = 2;
static const int TXT_X = 2, TW = SCR_W - 2 * TXT_X;
static const int STATUS_Y = FACE_Y + FACE_S + 3;   // 125, 글자 크기 2(16px)
static const int CAP_Y = STATUS_Y + 19;            // 144, 받아 적은 말·답
static const int FOOT_Y = SCR_H - 13;              // 227, 배터리·경로(「집」/「바깥」, 한글이라 efontKR 12px)
static const int CAP_MAX_Y = FOOT_Y - 1;
static const int BAR_Y = CAP_Y + 4;                // 녹음 막대

// 상태별 표정. 이름은 server/voice_bridge.py EMOTIONS 와 같다. 대답 표정은 서버가 X-Oakle-Emotion 으로 준다
#define FACE_BOOT   "hello"      // 켜짐·Wi-Fi 잡는 중
#define FACE_IDLE   "joy"        // 대기
#define FACE_LISTEN "yes"        // 듣는 중 (경례)
#define FACE_THINK  "curious"    // 보내고 기다리는 중
#define FACE_SPEAK  "ok"         // 대답 표정이 없거나 모르는 이름일 때
#define FACE_ERROR  "surprised"  // 서버·메모리 오류
#define FACE_NOWIFI "mindblown"  // 연결 없음
#define FACE_HUH    "curious"    // 너무 짧음·말소리 없음
#define FACE_SLEEPY "sleepy"     // 오래 쉼(화면 어둡게)

#if !HAVE_FACES
// 도형 얼굴: 표정 그림이 없을 때(공개 리포 그대로 빌드) 대신 그린다
static void shape_face(const char* f, int dx) {
  auto& d = M5.Display;
  const int cx = FACE_X + dx + FACE_S / 2, cy = FACE_Y + FACE_S / 2 + 6, r = 44;
  d.fillRect(FACE_X + dx, FACE_Y, FACE_S, FACE_S, TFT_BLACK);
  d.drawLine(cx, cy - r, cx, cy - r - 10, TFT_LIGHTGREY);                // 안테나
  d.fillCircle(cx, cy - r - 13, 4, TFT_ORANGE);
  d.fillCircle(cx, cy, r, 0xE71C);                                       // 헬멧(밝은 회색)
  d.fillCircle(cx, cy + 4, r - 10, 0xFF9B);                              // 얼굴(살구색)
  const int ex = 15, ey = cy - 2, my = cy + 18;
  auto is = [&](const char* n) { return strcmp(f, n) == 0; };
  if (is("sleepy") || is("joy") || is("lol") || is("thanks")) {          // 감은 눈 / 웃는 눈
    d.drawArc(cx - ex, ey + (is("sleepy") ? -3 : 3), 5, 4, is("sleepy") ? 20 : 200, is("sleepy") ? 160 : 340, TFT_BLACK);
    d.drawArc(cx + ex, ey + (is("sleepy") ? -3 : 3), 5, 4, is("sleepy") ? 20 : 200, is("sleepy") ? 160 : 340, TFT_BLACK);
  } else if (is("mindblown") || is("no")) {                              // X 눈
    for (int s : {-1, 1}) {
      d.drawLine(cx + s * ex - 4, ey - 4, cx + s * ex + 4, ey + 4, TFT_BLACK);
      d.drawLine(cx + s * ex - 4, ey + 4, cx + s * ex + 4, ey - 4, TFT_BLACK);
    }
  } else {
    int er = is("surprised") ? 6 : 4;
    d.fillCircle(cx - ex, ey, er, TFT_BLACK);
    d.fillCircle(cx + ex, ey, er, TFT_BLACK);
  }
  if (is("surprised") || is("curious") || is("yes")) d.fillCircle(cx, my, is("surprised") ? 6 : 3, TFT_BLACK);   // 동그란 입
  else if (is("sad") || is("gloomy") || is("sorry") || is("angry") || is("mindblown") || is("no"))
    d.drawArc(cx, my + 10, 9, 8, 220, 320, TFT_BLACK);                    // 처진 입
  else if (is("sleepy")) d.drawLine(cx - 4, my, cx + 4, my, TFT_BLACK);
  else d.drawArc(cx, my - 6, 10, 9, 30, 150, TFT_BLACK);                   // 웃는 입
  if (is("shy") || is("love") || is("joy")) {                             // 볼
    d.fillCircle(cx - 26, cy + 10, 5, 0xFB2C);
    d.fillCircle(cx + 26, cy + 10, 5, 0xFB2C);
  }
}
#endif

static const char* face_now = FACE_BOOT;          // 지금 화면의 표정(흔들기 반응 뒤 되돌릴 때)
// dx = 가로로 밀어 그리기(흔들기 반응용, ±FACE_X 안). 0 이 아니면 face_now 를 바꾸지 않는다
static void draw_face(const char* f, int dx = 0) {
  if (dx == 0) face_now = f;
#if HAVE_FACES
  for (size_t i = 0; i < FACE_COUNT; i++)
    if (strcmp(FACE_TABLE[i].name, f) == 0) {
      M5.Display.pushImage(FACE_X + dx, FACE_Y, FACE_W, FACE_H, FACE_TABLE[i].px);
      return;
    }
  for (size_t i = 0; i < FACE_COUNT; i++)                                // 모르는 이름이면 OK 얼굴
    if (strcmp(FACE_TABLE[i].name, FACE_SPEAK) == 0) { M5.Display.pushImage(FACE_X + dx, FACE_Y, FACE_W, FACE_H, FACE_TABLE[i].px); return; }
#else
  shape_face(f, dx);
#endif
}

static void touch() {
  last_touch = millis();
  if (dimmed) { M5.Display.setBrightness(128); dimmed = false; }
}

// 맨 아래 줄 왼쪽: 「85% 집」. 오른쪽 40px 는 자막 꼬리표(「3/7」) 자리라 건드리지 않는다
static void footer() {
  auto& d = M5.Display;
  d.fillRect(0, FOOT_Y, SCR_W - 40, SCR_H - FOOT_Y, TFT_BLACK);
  d.setFont(&fonts::Font0);
  d.setTextSize(1);
  d.setTextColor(TFT_WHITE, TFT_BLACK);
  d.setCursor(TXT_X, FOOT_Y + 3);
  d.printf("%d%%", M5.Power.getBatteryLevel());
  int x = d.getCursorX() + 4;
  d.setFont(&fonts::efontKR_12);
  d.setTextColor(route == R_HOME ? TFT_GREEN : route == R_OUT ? TFT_CYAN : TFT_RED, TFT_BLACK);
  d.setCursor(x, FOOT_Y);
  d.print(route_label(route));
  d.setFont(&fonts::Font0);
}

static void status(const char* s, uint16_t color = TFT_WHITE, const char* face = FACE_IDLE) {
  touch();
  M5.Display.fillScreen(TFT_BLACK);
  draw_face(face);
  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextColor(color);
  M5.Display.setTextSize(2);
  M5.Display.setTextDatum(top_center);
  M5.Display.drawString(s, SCR_W / 2, STATUS_Y);
  M5.Display.setTextDatum(top_left);
  footer();
}

// 아래 글자 칸에 한글을 글자 단위로 줄바꿈해 찍는다(라이브러리 줄바꿈은 화면 왼쪽 끝으로 돌아간다 [추론]).
// efontKR 은 M5GFX(LovyanGFX)에 들어 있는 한글 글꼴이다 [추론: 실기에서 글꼴 이름 확인 필요]. 넘치는 줄은 버린다.
static int caption(int y, const String& s, uint16_t color, int max_y = CAP_MAX_Y) {
  auto& d = M5.Display;
  d.setFont(&fonts::efontKR_12);
  d.setTextSize(1);
  d.setTextColor(color, TFT_BLACK);
  d.setTextWrap(false);
  const int lh = d.fontHeight() + 1;
  int x = TXT_X;
  for (size_t i = 0; i < s.length() && y + lh <= max_y;) {
    uint8_t c = s[i];
    size_t n = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : 4;   // UTF-8 한 글자
    String ch = s.substring(i, i + n);
    i += n;
    if (ch == "\n") { x = TXT_X; y += lh; continue; }
    int w = d.textWidth(ch);
    if (x + w > TXT_X + TW) { x = TXT_X; y += lh; if (y + lh > max_y) break; if (ch == " ") continue; }
    d.setCursor(x, y);
    d.print(ch);
    x += w;
  }
  d.setFont(&fonts::Font0);
  return y + lh;
}

// ---- 답 자막 스크롤: 긴 답은 세로 화면에서 끝이 잘린다 ----
// 답 화면을 줄 단위로 미리 접어 두고, B 짧게 누를 때마다 한 줄씩 내린다. 끝 다음은 처음으로. 새 답이 오면 맨 위
struct CapLine { String text; uint16_t color; };
static const int CAP_LINES_MAX = 64;
static CapLine cap_lines[CAP_LINES_MAX];
static int cap_n = 0, cap_off = 0;

static void cap_wrap(const String& s, uint16_t color, int max_lines = CAP_LINES_MAX) {   // caption() 과 같은 규칙으로 접는다
  auto& d = M5.Display;
  d.setFont(&fonts::efontKR_12);
  d.setTextSize(1);
  int start = cap_n, x = TXT_X;
  String cur;
  for (size_t i = 0; i < s.length();) {
    uint8_t c = s[i];
    size_t n = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : 4;
    String ch = s.substring(i, i + n);
    i += n;
    bool brk = ch == "\n";
    int w = brk ? 0 : d.textWidth(ch);
    if (brk || x + w > TXT_X + TW) {
      if (cap_n - start >= max_lines || cap_n >= CAP_LINES_MAX) { cur = ""; break; }
      cap_lines[cap_n++] = {cur, color};
      cur = ""; x = TXT_X;
      if (brk || ch == " ") continue;
    }
    cur += ch; x += w;
  }
  if (cur.length() && cap_n - start < max_lines && cap_n < CAP_LINES_MAX) cap_lines[cap_n++] = {cur, color};
  d.setFont(&fonts::Font0);
}

static int cap_visible() {
  M5.Display.setFont(&fonts::efontKR_12);
  int lh = M5.Display.fontHeight() + 1;
  M5.Display.setFont(&fonts::Font0);
  return (CAP_MAX_Y - CAP_Y) / lh;
}

static void cap_draw() {                           // 글자 칸과 꼬리표(「3/7」 = 맨 위 줄/전체 줄)만 다시 그린다
  auto& d = M5.Display;
  d.fillRect(0, CAP_Y, SCR_W, CAP_MAX_Y - CAP_Y, TFT_BLACK);
  d.setFont(&fonts::efontKR_12);
  d.setTextSize(1);
  const int lh = d.fontHeight() + 1;
  int y = CAP_Y;
  for (int i = cap_off; i < cap_n && y + lh <= CAP_MAX_Y; i++, y += lh) {
    d.setTextColor(cap_lines[i].color, TFT_BLACK);
    d.setCursor(TXT_X, y);
    d.print(cap_lines[i].text);
  }
  d.setFont(&fonts::Font0);
  d.setTextSize(1);
  d.fillRect(SCR_W - 40, FOOT_Y, 40, SCR_H - FOOT_Y, TFT_BLACK);
  if (cap_n > cap_visible()) {
    d.setTextColor(TFT_DARKGREY, TFT_BLACK);
    d.setTextDatum(top_right);
    d.drawString(String(cap_off + 1) + "/" + String(cap_n), SCR_W - TXT_X, FOOT_Y + 3);
    d.setTextDatum(top_left);
  }
}

static void cap_scroll() {                         // B 짧게: 한 줄 아래로, 끝이면 처음으로
  touch();
  if (cap_n == 0) { status("READY"); return; }
  int last = max(0, cap_n - cap_visible());
  cap_off = cap_off >= last ? 0 : cap_off + 1;
  Serial.printf("cap: scroll %d/%d\n", cap_off + 1, cap_n);
  cap_draw();
}

// ---- IR 학습 모드(버튼 B 3초): 리모컨 전원 버튼 신호를 받아 저장 ----
// 문서 주의 그대로 [근거: docs.m5stack.com/en/core/StickS3 「Infrared Reception Notes」]:
//  ① 수신은 RMT 로만 ② 받는 동안 스피커 앰프 끔 ③ 리모컨과 30 cm 이상. 수신기도 EXT_5V 에서 전원을 받는다
// 수신기 출력은 신호가 있을 때 LOW(복조됨) [추론: 일반 IR 수신 모듈] → RMT level 0 = mark
// 시리얼: 「ir learn: proto=NEC value=0x20DF10EF addr=0x20 cmd=0x10 raw=67」 + 「ir learn: us=…」 + 「ir learn: saved key=ir/tv_power」
static bool in_range(uint32_t v, uint32_t lo, uint32_t hi) { return v >= lo && v <= hi; }

// 앞에 붙은 잡음을 뗀다: 첫 긴 mark(≥3 ms, 헤더)부터 남긴다. 헤더가 없으면 그대로
// (실기 첫 학습에서 헤더 앞에 짧은 잡음 4칸이 붙어 해석이 실패한 적이 있다)
static size_t ir_trim(uint16_t* d, size_t n) {
  for (size_t i = 0; i < n; i += 2)                // 짝수 칸 = mark
    if (d[i] >= 3000) {
      if (i) { memmove(d, d + i, (n - i) * 2); Serial.printf("ir: trim %u leading durations\n", (unsigned)i); }
      return n - i;
    }
  return n;
}

// 원시(mark부터) → NEC 32bit(첫 비트가 MSB, LG 표기 0x20DF10EF 와 같은 순서). 아니면 false
// 헤더 mark 9 ms(NEC) 와 4.5 ms(삼성형, 같은 비트 부호) 둘 다 받는다 → *hdr45
static bool nec_decode(const uint16_t* d, size_t n, uint32_t* out, bool* hdr45) {
  if (n < 2 + 64 + 1 || !in_range(d[0], 3500, 11000) || !in_range(d[1], 3500, 5500)) return false;
  *hdr45 = d[0] < 7000;
  uint32_t v = 0;
  for (int i = 0; i < 32; i++) {
    uint16_t mk = d[2 + 2 * i], sp = d[3 + 2 * i];
    if (!in_range(mk, 300, 900)) return false;
    if (in_range(sp, 300, 900)) v <<= 1;
    else if (in_range(sp, 1300, 2100)) v = (v << 1) | 1;
    else return false;
  }
  *out = v;
  return true;
}

static void ir_learn() {
  cap_n = 0; cap_off = 0;                          // 옛 답 자막은 버린다(B 짧게가 덮어 그리지 않게)
  static rmt_data_t rx[192];
  M5.Speaker.end();                                  // 앰프 끔(M5Unified 콜백이 PMIC 0x11 bit3 을 내린다)
  pmic_bit(PM1_GPIO_OUT, 0x08, false);               // 혹시 몰라 한 번 더. 다음 Speaker.begin() 이 다시 켠다
  M5.Power.setExtOutput(true);                       // 수신기 전원
  status("IR LEARN", TFT_YELLOW, FACE_LISTEN);
  caption(CAP_Y, "리모컨을 기기 쪽으로 30cm 떨어져 겨누고 전원 버튼을 눌러 주세요", TFT_WHITE, CAP_Y + 4 * 13);
  Serial.printf("ir learn: start pin=%d timeout=%ds\n", IR_RX_PIN, IR_LEARN_S);
  bool ok = rmtInit(IR_RX_PIN, RMT_RX_MODE, RMT_MEM_NUM_BLOCKS_4, 1000000)   // 1 tick = 1 us, RX 메모리 4블록(192 심볼)
            && rmtSetRxMinThreshold(IR_RX_PIN, 2)                           // 2 us 보다 짧은 잡음 무시(S3 상한 ≈3 us)
            && rmtSetRxMaxThreshold(IR_RX_PIN, 15000);                      // 15 ms 조용하면 한 프레임 끝(NEC 프레임 안 최대 9 ms, 반복 간격 ≈40 ms)
  if (!ok) {
    Serial.println("ir learn: FAIL rmt rx init");
    status("IR FAIL", TFT_RED, FACE_ERROR);
    caption(CAP_Y, "수신기 초기화 실패", TFT_RED);
    M5.Power.setExtOutput(false);
    return;
  }
  delay(50);                                         // 수신기 전원 안정 [추론]
  static uint16_t got[IR_RAW_MAX];
  size_t gn = 0;
  size_t n = 192;
  rmtReadAsync(IR_RX_PIN, rx, &n);
  uint32_t t0 = millis(), shown = 0;
  bool cancel = false;
  while (millis() - t0 < IR_LEARN_S * 1000UL) {
    M5.update();
    if (M5.BtnA.wasPressed()) { cancel = true; break; }
    uint32_t left = IR_LEARN_S - (millis() - t0) / 1000;
    if (left != shown) {                             // 남은 초
      shown = left;
      M5.Display.fillRect(TXT_X, FOOT_Y - 12, TW, 10, TFT_BLACK);
      M5.Display.setTextSize(1); M5.Display.setCursor(TXT_X, FOOT_Y - 12);
      M5.Display.printf("%lus  (A = cancel)", (unsigned long)left);
    }
    if (rmtReceiveCompleted(IR_RX_PIN)) {
      // 심볼 → mark/space 목록. 앞에 붙은 space(대기 HIGH)는 버린다
      gn = 0;
      for (size_t i = 0; i < n && gn < IR_RAW_MAX; i++) {
        const uint32_t dur[2] = {rx[i].duration0, rx[i].duration1}, lvl[2] = {rx[i].level0, rx[i].level1};
        for (int h = 0; h < 2 && gn < IR_RAW_MAX; h++) {
          if (dur[h] == 0) continue;
          bool mark = lvl[h] == 0;
          if (gn == 0 && !mark) continue;
          if ((gn % 2 == 0) != mark) { if (gn) got[gn - 1] += dur[h]; continue; }   // 같은 레벨이 이어지면 합친다
          got[gn++] = dur[h];
        }
      }
      Serial.printf("ir learn: frame symbols=%u durations=%u\n", (unsigned)n, (unsigned)gn);
      if (gn >= 16) break;                           // 반복 코드(2~4칸)·잡음은 버리고 다시 기다린다
      gn = 0; n = 192;
      rmtReadAsync(IR_RX_PIN, rx, &n);
    }
    delay(10);
  }
  rmtDeinit(IR_RX_PIN);
  M5.Power.setExtOutput(false);
  if (cancel || gn == 0) {
    Serial.printf("ir learn: %s\n", cancel ? "cancel" : "timeout");
    status(cancel ? "CANCEL" : "TIMEOUT", TFT_RED, FACE_HUH);
    caption(CAP_Y, cancel ? "학습 취소. 저장한 신호는 그대로예요" : "신호를 못 받았어요. B 를 3초 눌러 다시 해 주세요", TFT_RED);
    return;
  }
  gn = ir_trim(got, gn);
  uint32_t v = 0;
  bool h45 = false;
  bool nec = nec_decode(got, gn, &v, &h45);
  String us;
  for (size_t i = 0; i < gn; i++) { if (i) us += ','; us += got[i]; }
  if (nec) Serial.printf("ir learn: proto=%s value=0x%08lX addr=0x%02X cmd=0x%02X check=%s raw=%u\n", h45 ? "NEC45" : "NEC", (unsigned long)v,
                         (unsigned)(v >> 24), (unsigned)((v >> 8) & 0xFF),
                         ((((v >> 24) ^ (v >> 16)) & 0xFF) == 0xFF && (((v >> 8) ^ v) & 0xFF) == 0xFF) ? "ok" : "bad",
                         (unsigned)gn);
  else Serial.printf("ir learn: proto=RAW value=0x00000000 raw=%u\n", (unsigned)gn);
  Serial.printf("ir learn: us=%s\n", us.c_str());
  Preferences p;
  bool saved = p.begin("ir", false) && p.putBytes("tv_power", got, gn * 2) == gn * 2;
  p.end();
  if (!saved) {
    Serial.println("ir learn: FAIL nvs save");
    status("IR FAIL", TFT_RED, FACE_ERROR);
    caption(CAP_Y, "받았는데 저장 실패(NVS)", TFT_RED);
    return;
  }
  memcpy(ir_raw, got, gn * 2); ir_raw_n = gn; ir_raw_nec = nec ? v : 0; ir_raw_hdr45 = nec && h45;
  Serial.println("ir learn: saved key=ir/tv_power");
  char line[64];
  if (nec) snprintf(line, sizeof line, "저장됨: %s 0x%08lX / 원시 %u펄스", h45 ? "NEC45" : "NEC", (unsigned long)v, (unsigned)gn);
  else snprintf(line, sizeof line, "저장됨: 원시 %u펄스 (NEC 아님)", (unsigned)gn);
  status("SAVED", TFT_GREEN, FACE_IDLE);
  caption(CAP_Y, line, TFT_GREEN);
}

static String url_decode(const String& in) {
  String out; out.reserve(in.length());
  for (size_t i = 0; i < in.length(); i++) {
    char c = in[i];
    if (c == '%' && i + 2 < in.length()) {
      out += (char)strtol(in.substring(i + 1, i + 3).c_str(), nullptr, 16); i += 2;
    } else out += (c == '+') ? ' ' : c;
  }
  return out;
}

// 녹음 중 입력 크기 막대(0~1). 말이 들어가고 있는지 눈으로 확인하는 용도.
static void level_bar(const int16_t* p, size_t n) {
  int peak = 0;
  for (size_t i = 0; i < n; i++) { int v = abs(p[i]); if (v > peak) peak = v; }
  int w = (int)((long)TW * peak / 32768);
  M5.Display.fillRect(TXT_X, BAR_Y, TW, 10, TFT_DARKGREY);
  M5.Display.fillRect(TXT_X, BAR_Y, w, 10, peak > 30000 ? TFT_RED : TFT_GREEN);   // 빨강 = 잘림
}

// ---- WAV ----
static void put_le(uint8_t* p, uint32_t v, int n) { for (int i = 0; i < n; i++) p[i] = (v >> (8 * i)) & 0xFF; }

static void wav_header(uint8_t* h, uint32_t data_bytes) {
  memcpy(h, "RIFF", 4); put_le(h + 4, 36 + data_bytes, 4); memcpy(h + 8, "WAVEfmt ", 8);
  put_le(h + 16, 16, 4); put_le(h + 20, 1, 2); put_le(h + 22, 1, 2); put_le(h + 24, RATE, 4);
  put_le(h + 28, RATE * 2, 4); put_le(h + 32, 2, 2); put_le(h + 34, 16, 2);
  memcpy(h + 36, "data", 4); put_le(h + 40, data_bytes, 4);
}

// ---- 녹음 ----
// 녹음 중 B 를 함께 누르면 녹음을 버리고 rec_combo = true (A+B 5초 = Wi-Fi 설정 모드, loop 가 이어서 센다)
static bool rec_combo = false;
static void record_while_held() {
  cap_n = 0; cap_off = 0;                          // 옛 답 자막은 버린다(B 짧게가 덮어 그리지 않게)
  rec_combo = false;
  t_press = millis();
  M5.Speaker.end();
  M5.Mic.begin();
  pcm_len = 0;
#if MIC_PGA_DB >= 0
  // ES8311(0x18) reg 0x14: bit4 = MIC1 선택, bit3..0 = PGA 3 dB 단위 / reg 0x17: ADC 디지털 볼륨, 0xBF = 0 dB.
  // M5Unified 의 마이크 콜백이 begin 때 0x14=0x10, 0x17=0xFF 를 쓰므로 그 뒤에 덮어쓴다 [근거: M5Unified.inl _microphone_enabled_cb_sticks3]
  // 콜백이 첫 record 때 다시 불리면 덮어쓴 값이 사라진다 → 실기에서 레지스터를 읽어 확인할 것 [추론]
  M5.In_I2C.writeRegister8(0x18, 0x14, 0x10 | (MIC_PGA_DB / 3), 100000);
  M5.In_I2C.writeRegister8(0x18, 0x17, 0xBF, 100000);
#endif
  status("LISTEN", TFT_GREEN, FACE_LISTEN);
  const size_t max_samples = RATE * MAX_SEC;
  while (M5.BtnA.isPressed() && pcm_len + CHUNK <= max_samples) {
    if (M5.BtnB.isPressed()) { rec_combo = true; break; }
    if (M5.Mic.record(pcm + pcm_len, CHUNK, RATE)) {
      if (pcm_len >= CHUNK) level_bar(pcm + pcm_len - CHUNK, CHUNK);   // 직전 조각은 채워져 있다 [추론: record 는 비동기]
      pcm_len += CHUNK;
    }
    M5.update();
  }
  t_release = millis();
  while (M5.Mic.isRecording()) delay(1);
  M5.Mic.end();
  size_t trim = RATE * TAIL_TRIM_MS / 1000;
  pcm_len = pcm_len > trim ? pcm_len - trim : 0;
}

// ---- 질의와 재생. 재생 중 A 를 다시 누르면 true (바로 새 녹음) ----
static bool ask_and_play() {
  if (t_release - t_press < MIN_HOLD_MS || pcm_len < RATE / 4) { status("short", TFT_WHITE, FACE_HUH); return false; }
  if (!WiFi.isConnected()) { status("NOWIFI", TFT_RED, FACE_NOWIFI); return false; }
  status("SEND", TFT_YELLOW, FACE_THINK);
  uint32_t data_bytes = pcm_len * 2;
  uint8_t* body = (uint8_t*)ps_malloc(44 + data_bytes);
  if (!body) { status("NOMEM", TFT_RED, FACE_ERROR); return false; }
  wav_header(body, data_bytes);
  memcpy(body + 44, pcm, data_bytes);

  const Route via = route;                       // 보내는 순간의 경로(답을 받는 동안 바뀌어도 서명 확인은 이걸로)
  String nonce;
  HTTPClient http;
  WiFiClientSecure tls;
  if (via == R_OUT) {
    if (!clock_ok(5000)) { free(body); status("CLOCK", TFT_RED, FACE_ERROR); caption(CAP_Y, "시계를 못 맞췄어요(NTP). 잠시 뒤 다시", TFT_RED); return false; }
    tls.setCACert(RELAY_ROOT_CA);
    http.begin(tls, RELAY_URL);
    uint8_t nb[12];
    esp_fill_random(nb, sizeof nb);
    nonce = hexs(nb, sizeof nb);
    String ts = String((unsigned long)time(nullptr));
    http.addHeader("Authorization", "Bearer " + relay_auth);
    http.addHeader("X-Oakle-Ts", ts);
    http.addHeader("X-Oakle-Nonce", nonce);
    http.addHeader("X-Oakle-Sig", relay_sig("req\n" DEVICE_ID "\n" + ts + "\n" + nonce + "\n/v1/ask\n" + sha256hex(body, 44 + data_bytes)));
  } else {
    http.begin(SERVER_URL);
    http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
  }
  // HTTPClient::setTimeout 은 uint16_t(ms)라 65535 가 끝이다 — 예전 120000 은 54464 ms 로 잘려 있었다(코어 3.3.9 HTTPClient.h)
  http.setTimeout(65000);
  http.setConnectTimeout(10000);
  http.addHeader("X-Device-Id", DEVICE_ID);
  http.addHeader("Content-Type", "audio/wav");
  const char* want[] = {"X-Transcript", "X-Answer", "X-Timing", "X-Oakle-Emotion", "X-Oakle-Action", "X-Oakle-Sig", "X-Relay-Ms"};
  http.collectHeaders(want, 7);
  uint32_t t_post = millis();
  status("THINK", TFT_YELLOW, FACE_THINK);
  int code = http.POST(body, 44 + data_bytes);   // 서버가 STT·LLM·TTS 를 다 끝내야 돌아온다
  uint32_t t_hdr = millis();
  free(body);

  if (code == 204) { status("no speech", TFT_WHITE, FACE_HUH); http.end(); return false; }
  if (code != 200) {
    char msg[24]; snprintf(msg, sizeof msg, "ERR %d", code);
    status(msg, TFT_RED, code < 0 ? FACE_NOWIFI : FACE_ERROR);   // 502 본문 = 실패 단계(stt/llm/tts), 음수 = 연결 실패
    // 바깥 경로 오류 본문은 {"error":"connector_offline","fallback":true} 꼴(docs/relay-protocol.md)
    M5.Display.setCursor(TXT_X, CAP_Y); M5.Display.print(code < 0 ? http.errorToString(code) : http.getString().substring(0, via == R_OUT ? 40 : 20));
    Serial.printf("ask failed: route=%s code=%d after %lu ms\n", route_name(via), code, (unsigned long)(t_hdr - t_release));
    http.end(); return false;
  }
  String heard = url_decode(http.header("X-Transcript"));
  String answer = url_decode(http.header("X-Answer"));
  String srv = http.header("X-Timing");
  String emo = http.header("X-Oakle-Emotion");     // 표정 이름(joy …). 비었거나 모르는 이름이면 OK 얼굴
  emo.trim();
  if (emo.length() == 0) emo = FACE_SPEAK;
  String act = http.header("X-Oakle-Action");      // 기기 동작(있을 때만). 지금은 IR 하나
  int len = http.getSize();
  if (len <= 44 || len > 2 * 1024 * 1024) { status("BADLEN", TFT_RED, FACE_ERROR); http.end(); return false; }
  uint8_t* wav = (uint8_t*)ps_malloc(len);
  if (!wav) { status("NOMEM", TFT_RED, FACE_ERROR); http.end(); return false; }
  WiFiClient* s = http.getStreamPtr();
  int got = 0; uint32_t t0 = millis();
  while (got < len && millis() - t0 < 30000) {
    int n = s->readBytes(wav + got, len - got);
    if (n > 0) got += n;
  }
  uint32_t t_body = millis();
  String rsig = http.header("X-Oakle-Sig"), relay_ms = http.header("X-Relay-Ms");
  http.end();
  if (via == R_OUT) {                              // 응답 서명: 중계 서버가 답을 바꾸거나 지어냈으면 버린다(docs/relay-protocol.md)
    bool okSig = got == len && rsig.equalsIgnoreCase(relay_sig("resp\n" + nonce + "\n200\n" + sha256hex(wav, got)));
    if (!okSig) {
      free(wav);
      Serial.printf("ask failed: route=outdoor bad response sig (got=%d len=%d)\n", got, len);
      status("SIG ERR", TFT_RED, FACE_ERROR);
      caption(CAP_Y, "중계 답의 서명이 안 맞아 버렸어요", TFT_RED);
      return false;
    }
  }

  const char* ir = nullptr;
  if (act.length()) {                              // 말하기 전에 쏜다: 앰프가 아직 꺼져 있다
    status("IR", TFT_YELLOW, FACE_THINK);
    ir = do_action(act);
  }
  status("SPEAK", TFT_CYAN, emo.c_str());
  cap_n = 0; cap_off = 0;                          // 새 답은 맨 위부터
  if (ir) cap_wrap(strcmp(ir, "실패") == 0 ? String("IR 신호 못 보냄(IR 오류)")
                                           : String(ir) + (ir_used_learned ? "(학습)" : "(기본)") + " 신호 보냄 — 결과는 기기를 봐 주세요",
                   strcmp(ir, "실패") == 0 ? TFT_RED : TFT_YELLOW, 3);   // 기기는 TV·서큘레이터 상태를 모른다
  cap_wrap(heard.substring(0, 60), TFT_DARKGREY, 2);   // 무엇으로 알아들었는지 먼저(두 줄까지)
  cap_wrap(answer, TFT_WHITE);
  cap_draw();
  M5.Speaker.begin();
  M5.Speaker.setVolume(speaker_volume());
  M5.Speaker.playWav(wav, got);
  uint32_t t_play = millis();
  Serial.printf("timing ms: route=%s relay=%s held=%lu post=+%lu hdr=+%lu body=+%lu play=+%lu | server %s\n",
                route_name(via), relay_ms.length() ? relay_ms.c_str() : "-",
                (unsigned long)(t_release - t_press), (unsigned long)(t_post - t_release),
                (unsigned long)(t_hdr - t_release), (unsigned long)(t_body - t_release),
                (unsigned long)(t_play - t_release), srv.c_str());
  bool barge = false;
  while (M5.Speaker.isPlaying()) {
    M5.update();
    if (M5.BtnA.wasPressed()) { M5.Speaker.stop(); barge = true; break; }
    if (M5.BtnB.wasClicked()) cap_scroll();        // 말하는 중에도 내려 읽는다
    delay(10);
  }
  free(wav);
  if (!barge) { status("READY"); cap_draw(); }     // 다 말한 뒤에도 답을 남겨 두고 B 로 내려 읽게
  return barge;
}

// ---- 흔들기 — 감지는 shake_detect.h ----
// loop 에서만 본다 → 녹음·재생·Wi-Fi 설정 모드·IR 학습은 loop 를 막고 도니 그동안은 자연히 안 센다(샘플이 끊기면 처음부터).
// 버튼을 누르고 있거나 막 손댄 직후(0.4초)·스피커가 울리는 중에도 세지 않는다.
// 반응: 어지러운 표정이 좌우로 흔들리며 「띠요요용」(M5.Speaker.tone, 기기 안에서만 — 네트워크 없음) → 잠깐 멈춤 → 원래 표정
static bool imu_ok = false;
static shk::State shake_st;
static shk::Config shake_cfg;
static void shake_init() {
  shake_cfg.thr_g = SHAKE_G;
  shake_cfg.need = SHAKE_PEAKS;
  shake_cfg.window_ms = SHAKE_WINDOW_MS;
  shake_cfg.max_gap_ms = SHAKE_GAP_MS;
  shake_cfg.cooldown_ms = SHAKE_COOLDOWN_S * 1000UL;
  imu_ok = SHAKE_ENABLE && M5.Imu.isEnabled();
  Serial.printf("boot: imu=%s type=%d shake=%s thr=%.1fg peaks=%d gap=%lums cool=%ds\n", M5.Imu.isEnabled() ? "ok" : "none",
                (int)M5.Imu.getType(), imu_ok ? "on" : "off", (double)SHAKE_G, SHAKE_PEAKS, (unsigned long)SHAKE_GAP_MS, SHAKE_COOLDOWN_S);
}

static bool btn_raw_down() { return digitalRead(GPIO_NUM_11) == LOW || digitalRead(GPIO_NUM_12) == LOW; }

static void shake_react() {
  const char* back = face_now;
  bool was_dim = dimmed;
  if (was_dim || strcmp(back, FACE_SLEEPY) == 0) back = FACE_IDLE;   // 졸다가 흔들려 깼다
  Serial.printf("shake: peaks=%d max=%.1fg dur=%lums face=%s was_dim=%d\n", shake_st.last_peaks, (double)shake_st.last_max_g,
                (unsigned long)shake_st.last_dur_ms, FACE_DIZZY, was_dim);
  touch();                                         // 화면 밝게, 잠들기 시계 처음부터
  bool spk_was_on = M5.Speaker.isEnabled();
  if (!spk_was_on) M5.Speaker.begin();
  M5.Speaker.setVolume(speaker_volume());
  // 좌우로 흔들리는 얼굴 + 내려가며 떨리는 소리. 버튼이 눌리면 바로 그친다(눌림은 M5.update 를 안 불러 loop 몫으로 남긴다)
  static const int DX[] = {-FACE_X, FACE_X, -5, 5, -3, 3, -1, 1};
  static const uint16_t HZ[] = {1320, 990, 1180, 880, 1050, 780, 940, 660};
  auto& d = M5.Display;
  bool cut = false;
  for (size_t i = 0; i < sizeof(DX) / sizeof(DX[0]) && !cut; i++) {
    d.fillRect(0, FACE_Y, SCR_W, FACE_S, TFT_BLACK);
    draw_face(FACE_DIZZY, DX[i]);
    M5.Speaker.tone(HZ[i], 70);
    uint32_t t0 = millis();
    while (millis() - t0 < 75) { if (btn_raw_down()) { cut = true; break; } delay(5); }
  }
  d.fillRect(0, FACE_Y, SCR_W, FACE_S, TFT_BLACK);
  draw_face(FACE_DIZZY);
  uint32_t t0 = millis();
  while (!cut && millis() - t0 < 1000) { if (btn_raw_down()) cut = true; delay(10); }
  M5.Speaker.stop();
  if (!spk_was_on) M5.Speaker.end();
  draw_face(back);
  touch();
  shk::reset(shake_st);                            // 반응하는 동안 쌓인 건 버린다(쿨다운은 남는다)
}

static void check_shake() {
  if (!imu_ok) return;
  if (M5.BtnA.isPressed() || M5.BtnB.isPressed() || millis() - last_touch < 400 || M5.Speaker.isPlaying()) {
    shk::reset(shake_st);
    return;
  }
  float x, y, z;
  if (!M5.Imu.getAccel(&x, &y, &z)) return;
  if (shk::feed(shake_st, shake_cfg, millis(), x, y, z)) shake_react();
}

// ---- 전원 ----
static void deep_sleep() {
  Serial.println("idle: deep sleep (wake: A or B)");
  M5.Display.setBrightness(0);
  M5.Display.sleep();
  WiFi.disconnect(true); WiFi.mode(WIFI_OFF);
  pmic_bit(PM1_GPIO_OUT, 0x08 | 0x04, false);    // 앰프와 L3B(LCD·코덱 전원)를 끈다
  while (digitalRead(GPIO_NUM_11) == LOW || digitalRead(GPIO_NUM_12) == LOW) delay(20);   // 누른 채로 잠들면 바로 깬다
  delay(50);
  // KEY1/KEY2 는 active-low, 풀업은 딥슬립에도 살아 있는 쪽에 있다 [근거: Muse SDK 보드 코드 주석] / 실기 미확인
  esp_sleep_enable_ext1_wakeup((1ULL << GPIO_NUM_11) | (1ULL << GPIO_NUM_12), ESP_EXT1_WAKEUP_ANY_LOW);
  esp_deep_sleep_start();                        // 깨면 setup() 부터 다시 (Wi-Fi 재접속 2~3초 [추론])
}

// ---- 저장 망(NVS 「wifi」): n, s0..s4(SSID), p0..p4(비밀번호), h0..h4(집), seed(config.h 시드 지문) ----
static uint32_t nets_seed = 0;                   // NVS 에 적힌 시드 지문. 지우기(초기화)는 이것을 남겨 시드가 되살아나지 않게 한다

static void nets_load() {
  wl::clear(nets);
  Preferences p;
  if (!p.begin("wifi", true)) return;            // 처음엔 이름공간이 없다
  int n = constrain((int)p.getUChar("n", 0), 0, wl::MAX_NETS);
  for (int i = 0; i < n; i++) {
    char k[4];
    wl::Net& e = nets.n[nets.count];
    snprintf(k, sizeof k, "s%d", i);
    if (p.getString(k, e.ssid, sizeof e.ssid) == 0 || !e.ssid[0]) continue;
    snprintf(k, sizeof k, "p%d", i);
    if (p.getString(k, e.pass, sizeof e.pass) == 0) e.pass[0] = 0;
    snprintf(k, sizeof k, "h%d", i);
    e.home = p.getBool(k, false);
    nets.count++;
  }
  nets_seed = p.getUInt("seed", 0);
  p.end();
}

static bool nets_save() {
  Preferences p;
  if (!p.begin("wifi", false)) return false;
  bool ok = p.clear();
  ok = p.putUChar("n", nets.count) == 1 && ok;
  for (int i = 0; i < nets.count; i++) {
    char k[4];
    snprintf(k, sizeof k, "s%d", i); ok = p.putString(k, nets.n[i].ssid) > 0 && ok;
    snprintf(k, sizeof k, "p%d", i); p.putString(k, nets.n[i].pass);          // 열린 망은 0 바이트라 결과로 판정 안 함
    snprintf(k, sizeof k, "h%d", i); ok = p.putBool(k, nets.n[i].home) == 1 && ok;
  }
  ok = p.putUInt("seed", nets_seed) == 4 && ok;
  p.end();
  Serial.printf("wifi: saved nets=%d %s\n", nets.count, ok ? "ok" : "FAIL");
  return ok;
}

// config.h 의 WIFI_SSID(집)·HOTSPOT_SSID(바깥)를 시드로 합친다. 처음 한 번, 그리고 시드 값이 바뀐 펌웨어를 구웠을 때만
// (예시 config 의 "CHANGE_ME" 와 빈 값은 건너뛴다)
static void nets_seed_from_config() {
  wl::Net s[2];
  int n = 0;
  auto add = [&](const char* ssid, const char* pass, bool home) {
    if (!wl::ssid_ok(ssid) || strcmp(ssid, "CHANGE_ME") == 0 || strlen(pass) > 64) return;
    strcpy(s[n].ssid, ssid); strcpy(s[n].pass, strcmp(pass, "CHANGE_ME") ? pass : ""); s[n].home = home; n++;
  };
  add(WIFI_SSID, WIFI_PASS, true);
#ifdef HOTSPOT_SSID
  add(HOTSPOT_SSID, HOTSPOT_PASS, false);
#endif
  if (n == 0) return;
  uint32_t h = wl::seed_hash(s, n);
  if (h == nets_seed) return;
  int skipped;
  int added = wl::merge_seeds(nets, s, n, &skipped);
  nets_seed = h;
  Serial.printf("wifi: seed from config.h n=%d added=%d skipped=%d\n", n, added, skipped);
  nets_save();
}

// ---- Wi-Fi 고르기: 스캔해서 보이는 저장 망 중 집 → 바깥 순, 같은 쪽은 신호 센 것부터(wifi_list.h order) ----
// 저장 망이 하나도 안 보이면 부팅 때만(allow_blind) 전부 차례로 시도(숨은 SSID·스캔 실패 대비). 재연결 땐 스캔만 하고 넘어간다
// — 망이 5개면 다 시도하는 데 40초라, 그동안 버튼이 먹통이 된다
// SSID·비밀번호는 시리얼·화면에 찍지 않는다(경로 이름과 목록 번호만)
static bool wifi_try(const char* ssid, const char* pass) {
  WiFi.disconnect(false, false);
  delay(50);
  WiFi.begin(ssid, pass[0] ? pass : nullptr);
  uint32_t t0 = millis();
  while (!WiFi.isConnected() && millis() - t0 < WIFI_WAIT_MS) delay(100);
  return WiFi.isConnected();
}

static const int SCAN_MAX = 40;
static char scan_ssid[SCAN_MAX][33];
static wl::ScanItem scan_items[SCAN_MAX];
static int scan_n = 0;

static int wifi_scan() {                         // 결과를 scan_items 에 복사해 둔다(포털 목록에도 쓴다)
  int n = WiFi.scanNetworks();
  scan_n = 0;
  for (int i = 0; i < n && scan_n < SCAN_MAX; i++) {
    strlcpy(scan_ssid[scan_n], WiFi.SSID(i).c_str(), sizeof scan_ssid[0]);
    scan_items[scan_n] = {scan_ssid[scan_n], (int)WiFi.RSSI(i)};
    scan_n++;
  }
  WiFi.scanDelete();
  return n;
}

static bool wifi_connect(bool allow_blind) {
  WiFi.disconnect(false, false);
  int n = wifi_scan();
  int ord[wl::MAX_NETS];
  bool blind;
  int k = wl::order(nets, scan_items, scan_n, HAVE_OUTDOOR, PREFER_OUTDOOR, allow_blind || n <= 0, ord, &blind);
  Serial.printf("wifi: scan n=%d saved=%d try=%d%s\n", n, nets.count, k, blind ? " blind" : "");
  bool ok = false;
  for (int i = 0; i < k && !ok; i++) {
    const wl::Net& e = nets.n[ord[i]];
    ok = wifi_try(e.ssid, e.pass);
    Serial.printf("wifi: try #%d %s %s\n", ord[i], route_name(route_of_ssid(e.ssid)), ok ? "ok" : "fail");
  }
  if (!ok) WiFi.disconnect(false, false);        // 실패한 begin 이 뒤에서 계속 붙으려 하지 않게
  route = ok ? route_of_ssid(WiFi.SSID()) : R_NONE;
  return ok;
}

// ---- 설정 모드: SoftAP + DNS 전부 가로채기 + 설정 페이지. 저장하면 재부팅 ----
// 휴대폰은 붙자마자 연결 확인 주소(generate_204·hotspot-detect 등)를 묻는다 → 모두 http://192.168.4.1/ 로 돌려 설정 페이지를 띄운다
// A = 나가기, B 5초 = 저장 망 모두 지우기. 휴대폰이 안 붙은 채 PORTAL_TIMEOUT_S 지나면 나간다(저장 망이 있을 때만)
static bool b_long_done = false;                 // B 길게가 이미 처리됨(떼기 전까지 IR 학습 안 함)

// 글자 칸은 6줄(efontKR 12px, 한 줄 한글 10자 남짓) — 줄마다 한 줄에 들어가게 짧게 쓴다. 넘치면 아래 줄이 잘린다
static void portal_screen(const char* ap, const char* note, uint16_t color) {
  status("SETUP", TFT_YELLOW, FACE_BOOT);
  String s = String("Wi-Fi 설정 모드\n") + ap + "\n192.168.4.1\n";
  s += note && *note ? note : "폰으로 붙어 설정해요";
  s += "\nA 나가기\nB 5초 모두 지우기";
  caption(CAP_Y, s, color);
}

static void portal(const char* why) {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char ap[20];
  snprintf(ap, sizeof ap, "Oakle-Gadget-%02X%02X", mac[4], mac[5]);
  Serial.printf("portal: start why=%s ap=%s saved=%d\n", why, ap, nets.count);
  WiFi.disconnect(false, false);
  route = R_NONE;
  WiFi.mode(WIFI_AP_STA);
  wifi_scan();                                   // AP 를 띄우기 전에 한 번(띄운 뒤 스캔은 휴대폰 연결을 잠깐 흔든다)
  const IPAddress ip(192, 168, 4, 1);
  WiFi.softAPConfig(ip, ip, IPAddress(255, 255, 255, 0));
#ifdef PORTAL_PASS
  WiFi.softAP(ap, PORTAL_PASS);
#else
  WiFi.softAP(ap);
#endif
  DNSServer dns;
  dns.setErrorReplyCode(DNSReplyCode::NoError);
  dns.start(53, "*", ip);
  WebServer srv(80);
  uint32_t restart_at = 0, last_seen = millis();
  auto send_page = [&](const char* msg, bool err) {
    std::string h = wl::page(nets, scan_items, scan_n, HAVE_OUTDOOR, ap, msg, err);
    srv.sendHeader("Cache-Control", "no-store");
    srv.send(200, "text/html; charset=utf-8", h.c_str());
  };
  auto to_root = [&]() { srv.sendHeader("Location", "http://192.168.4.1/"); srv.send(302, "text/plain", ""); };
  srv.on("/", HTTP_GET, [&]() {
    if (srv.hostHeader() != "192.168.4.1") { to_root(); return; }   // 다른 이름으로 온 연결 확인 요청 → 설정 페이지로
    send_page("", false);
  });
  srv.on("/scan", HTTP_GET, [&]() { wifi_scan(); to_root(); });
  srv.on("/save", HTTP_POST, [&]() {
    String s = srv.arg("s"), p = srv.arg("p");
    bool home = HAVE_OUTDOOR ? srv.hasArg("h") : true;
    wl::Result r = wl::upsert(nets, s.c_str(), p.c_str(), home);
    if (r != wl::ADDED && r != wl::UPDATED) { send_page(wl::result_text(r), true); return; }
    if (!nets_save()) { nets_load(); send_page("NVS 저장 실패 — 다시 해 주세요", true); return; }
    String msg = String(wl::result_text(r)) + ". 기기가 다시 켜지며 붙어 볼게요. 이 설정 Wi-Fi 는 곧 끊겨요."
                 " 못 붙으면 설정 Wi-Fi 가 다시 떠요(비밀번호 확인).";
    send_page(msg.c_str(), false);
    portal_screen(ap, "저장함, 다시 켜요", TFT_GREEN);
    restart_at = millis() + 2000;                 // 응답이 휴대폰에 닿을 시간
  });
  srv.on("/forget", HTTP_POST, [&]() {
    bool ok = wl::remove(nets, srv.arg("s").c_str()) && nets_save();
    send_page(ok ? "지웠어요" : "못 지웠어요(이미 없거나 NVS 실패)", !ok);
  });
  srv.on("/wipe", HTTP_POST, [&]() {
    wl::clear(nets);
    bool ok = nets_save();
    send_page(ok ? "저장된 망을 모두 지웠어요" : "NVS 실패", !ok);
  });
  srv.onNotFound(to_root);
  srv.begin();
  portal_screen(ap, nullptr, TFT_WHITE);
  // pressedFor 는 누르기 시작한 때부터 잰다 → A+B 5초·B 누른 채 켜기로 들어오면 B 가 이미 눌려 있어 바로 「모두 지우기」가 된다.
  // 들어올 때 눌려 있던 B 는 한 번 뗀 뒤부터 센다
  bool b_wiped = M5.BtnB.isPressed();
  for (;;) {
    dns.processNextRequest();
    srv.handleClient();
    M5.update();
    if (restart_at && (int32_t)(millis() - restart_at) >= 0) { Serial.println("portal: saved, restart"); delay(100); ESP.restart(); }
    if (WiFi.softAPgetStationNum() > 0) { last_seen = millis(); touch(); }
    if (M5.BtnA.wasPressed()) { Serial.println("portal: exit (A)"); break; }
    if (M5.BtnB.isReleased()) b_wiped = false;
    else if (!b_wiped && M5.BtnB.pressedFor(PORTAL_WIPE_MS)) {
      b_wiped = true;
      wl::clear(nets);
      bool ok = nets_save();
      Serial.printf("portal: wipe by button %s\n", ok ? "ok" : "FAIL");
      portal_screen(ap, ok ? "모두 지웠어요" : "지우기 실패(NVS)", ok ? TFT_GREEN : TFT_RED);
    }
    uint32_t quiet = millis() - last_seen;
    if (nets.count > 0 && quiet > PORTAL_TIMEOUT_S * 1000UL) { Serial.println("portal: exit (timeout)"); break; }
    if (SLEEP_AFTER_S && quiet > SLEEP_AFTER_S * 1000UL && millis() - last_touch > SLEEP_AFTER_S * 1000UL && !on_usb()) {
      srv.stop(); dns.stop();
      deep_sleep();                              // 배터리에서 아무도 안 붙으면 평소처럼 잔다(깨면 다시 setup)
    }
    delay(2);
  }
  srv.stop();
  dns.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  b_long_done = true;                            // 나가며 누르고 있던 B 가 IR 학습으로 이어지지 않게
}

// A·B 를 함께 PORTAL_COMBO_MS 누르면 true. 하나라도 먼저 떼면 false(아무 일 없음)
static bool combo_hold() {
  uint32_t t0 = millis();
  int shown = -1;
  status("SETUP?", TFT_YELLOW, FACE_HUH);
  Serial.println("portal: combo A+B");
  while (M5.BtnA.isPressed() && M5.BtnB.isPressed()) {
    uint32_t el = millis() - t0;
    if (el >= PORTAL_COMBO_MS) return true;
    int left = (PORTAL_COMBO_MS - el + 999) / 1000;
    if (left != shown) {
      shown = left;
      M5.Display.fillRect(0, CAP_Y, SCR_W, CAP_MAX_Y - CAP_Y, TFT_BLACK);
      caption(CAP_Y, String("A·B 를 계속 누르면\n") + left + "초 뒤 Wi-Fi 설정 모드", TFT_YELLOW);
    }
    M5.update();
    delay(10);
  }
  b_long_done = true;
  status(WiFi.isConnected() ? "READY" : "NOWIFI", WiFi.isConnected() ? TFT_WHITE : TFT_RED, WiFi.isConnected() ? FACE_IDLE : FACE_NOWIFI);
  return false;
}

// 경로의 healthz 한 번: 집 = LAN voice_bridge, 바깥 = 중계 서버(TLS 사슬·커넥터 상태까지). 부팅·경로 바뀔 때 시리얼 판정용
static void probe_healthz() {
  if (route == R_NONE) return;
  HTTPClient h;
  WiFiClientSecure tls;
  String url = String(route == R_OUT ? RELAY_URL : SERVER_URL);
  url.replace("/v1/ask", "/healthz");
  if (route == R_OUT) { tls.setCACert(RELAY_ROOT_CA); h.begin(tls, url); }
  else h.begin(url);
  h.setTimeout(route == R_OUT ? 8000 : 3000);
  const char* want[] = {"X-Connector"};
  h.collectHeaders(want, 1);
  uint32_t t0 = millis();
  int code = h.GET();
  Serial.printf("healthz: route=%s code=%d connector=%s ms=%lu\n", route_name(route), code,
                h.header("X-Connector").length() ? h.header("X-Connector").c_str() : "-", (unsigned long)(millis() - t0));
  h.end();
}

static void keep_wifi() {
  static bool was_up = true;                     // 부팅 줄이 이미 찍었다
  if (WiFi.isConnected()) {
    Route r = route_of_ssid(WiFi.SSID());
    if (!was_up || r != route) {                 // 늦게 붙거나 다시 붙었을 때도 시리얼로 판정할 수 있게
      was_up = true;
      route = r;
      Serial.printf("wifi: up route=%s ip=%s rssi=%d\n", route_name(route), WiFi.localIP().toString().c_str(), WiFi.RSSI());
      footer();
      probe_healthz();
    }
    wifi_backoff = 1000; return;
  }
  if (was_up) { was_up = false; route = R_NONE; footer(); Serial.println("wifi: down"); }
  if ((int32_t)(millis() - wifi_next_try) < 0) return;
  // 다시 붙을 땐 스캔부터 — 집을 나서면 핫스팟으로, 돌아오면 집으로 바뀐다. 스캔+시도 동안 버튼은 안 받는다
  // (보이는 저장 망만 시도: 스캔 ≈2초 + 망당 최대 WIFI_WAIT_MS)
  wifi_connect(false);
  wifi_next_try = millis() + wifi_backoff;
  wifi_backoff = min<uint32_t>(wifi_backoff * 2, 30000);   // 1초 → 최대 30초
}

static void keep_wifi_now() {                    // 설정 모드에서 나온 직후: 기다리지 말고 바로 다시 붙어 본다
  wifi_next_try = millis();
  wifi_backoff = 1000;
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  Serial.begin(115200);
  M5.Display.setRotation(SCREEN_ROT);
  M5.Display.setSwapBytes(true);   // 표정 배열은 보통 순서 RGB565(0xRRRR_RGGG_GGGB_BBBB). LGFX 기본(false)은 바이트가 뒤집힌 swap565 로 읽어 깨진다
#if PMIC_LED_OFF
  pmic_bit(PM1_PWR_CFG, 0x10, false);
#endif
  pcm = (int16_t*)ps_malloc(RATE * MAX_SEC * 2);
  relay_init();
  status("WIFI..", TFT_WHITE, FACE_BOOT);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);                    // 같은 AP 로만 다시 붙는다 → 경로를 못 바꾼다. 재연결은 keep_wifi 가 스캔부터
  configTime(0, 0, "pool.ntp.org", "time.google.com");   // 바깥 서명용 유닉스 시각(SNTP 는 뒤에서 계속 맞춘다)
  nets_load();
  nets_seed_from_config();
  Serial.printf("boot: nets=%d\n", nets.count);
  // B 를 누른 채 켜면(PORTAL_BOOT_HOLD_MS 동안 계속) 설정 모드. 짧게 눌러 깨운 것은 그냥 지나간다
  M5.update();
  bool boot_setup = false;
  if (M5.BtnB.isPressed()) {
    b_long_done = true;                            // 이 누름이 loop 에서 IR 학습(B 3초)으로 이어지지 않게
    uint32_t t0 = millis();
    while (M5.BtnB.isPressed() && millis() - t0 < PORTAL_BOOT_HOLD_MS) { M5.update(); delay(10); }
    boot_setup = M5.BtnB.isPressed();
  }
  if (boot_setup) portal("button-boot");
  else if (nets.count == 0) portal("no-saved");
  else if (!wifi_connect(true)) portal("all-failed");
  if (WiFi.isConnected()) status("READY");
  else status("NOWIFI", TFT_RED, FACE_NOWIFI);
  // 부팅 한 줄: 시리얼만 보고도 Wi-Fi·경로·서버 도달을 판정한다
  Serial.printf("boot: wifi=%s route=%s outdoor=%s ip=%s rssi=%d vin=%dmV vol=%u rot=%d\n", WiFi.isConnected() ? "ok" : "FAIL",
                route_name(route), HAVE_OUTDOOR ? "configured" : "off", WiFi.localIP().toString().c_str(), WiFi.RSSI(),
                vin_mv(), speaker_volume(), SCREEN_ROT);
  ir_load_learned();
  if (ir_raw_n) Serial.printf("boot: ir=%s proto=%s value=0x%08lX raw=%u discrete=%d\n", IR_USE_LEARNED ? "learned" : "default(learned-stored)",
                              !ir_raw_nec ? "RAW" : ir_raw_hdr45 ? "NEC45" : "NEC", (unsigned long)ir_raw_nec, (unsigned)ir_raw_n, IR_LG_DISCRETE);
  else Serial.printf("boot: ir=default discrete=%d\n", IR_LG_DISCRETE);
  if (route == R_OUT) clock_ok(5000);              // 시각을 먼저 받아 둔다(첫 질의 지연 줄이기)
  probe_healthz();
  shake_init();
}

void loop() {
  M5.update();
  keep_wifi();
  if (M5.BtnA.wasPressed()) {
    touch();
    bool combo = M5.BtnB.isPressed();              // B 를 먼저 누르고 A → 녹음하지 않고 설정 모드 확인
    if (!combo) {
      do { record_while_held(); combo = rec_combo; } while (!combo && ask_and_play());   // 재생 끊고 다시 말하기
    }
    if (combo) {
      b_long_done = true;                          // 이 B 누름은 IR 학습으로 세지 않는다
      if (combo_hold()) { portal("button-combo"); keep_wifi_now(); }
    }
  }
  // B 짧게(0.5초 안에 뗌) = 답 자막 한 줄 내리기. M5Unified 의 wasClicked 는 hold(500ms) 넘긴 누름엔 안 뜬다 → 3초 학습과 안 섞인다
  if (M5.BtnB.wasClicked()) cap_scroll();
  check_shake();                                   // 흔들면 어지러운 표정 + 소리
  // B 3초 = IR 학습. 0.5~3초에 떼면 아무것도 안 한다. A 를 함께 누르고 있으면(설정 모드 조합) 학습 안 함
  if (M5.BtnB.isReleased()) b_long_done = false;
  else if (!b_long_done && M5.BtnB.pressedFor(3000) && !M5.BtnA.isPressed()) {
    b_long_done = true;
    ir_learn();
  }
  uint32_t idle = millis() - last_touch;
  if (!dimmed && idle > DIM_AFTER_S * 1000UL) {   // 오래 쉼: 졸린 얼굴로 바꾸고 어둡게
    draw_face(FACE_SLEEPY);
    M5.Display.setBrightness(16); dimmed = true;
  }
  if (SLEEP_AFTER_S && idle > SLEEP_AFTER_S * 1000UL && !on_usb()) deep_sleep();
  delay(10);
}
