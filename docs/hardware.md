# M5Stack StickS3 (K150) 하드웨어

핀은 **회로도 `K150_Stick_S3_PRJ_V0.6`(2025-11-11) 의 신호 이름**으로 확인했다. 펌웨어 기본값이나 다른 보드 설정을 배선 근거로 쓰지 않는다.

등급: **[근거]** 출처에 적혀 있음 · **[추론]** 출처끼리 맞춰 본 것 · **미확인**

## 출처

| 약칭 | 내용 |
|---|---|
| S | 회로도 V0.6 PDF — https://docs.m5stack.com/en/core/StickS3 의 Schematic 링크 |
| D | https://docs.m5stack.com/en/core/StickS3 (PinMap 표, IR 수신 주의) |
| U | https://github.com/m5stack/M5Unified (`board_M5StickS3` 분기, `M5PM1_Class`) |
| I | M5 공식 StickS3 IR NEC 예제 https://docs.m5stack.com/en/arduino/m5sticks3/ir_nec |
| M | Meta Muse Gadget SDK `boards/board_m5stack_sticks3.c` (참고로만 읽음, 코드 옮기지 않음) |

## 가장 중요한 함정

1. **스피커 앰프(AW8737) enable 이 ESP32 GPIO 에 없다.** PMIC(M5PM1, I2C 0x6E) G3(`PYG3_SPK_Pulse`)에 붙어 있다. LCD·오디오(코덱) 전원 L3B 도 PMIC G2(`PYG2_L3B_EN`). 그래서 I2C 로 PMIC 를 써야 소리가 난다. M5Unified `M5.begin()` 이 처리한다 [근거: S, U].
2. **PMIC 는 ESP 가 리셋돼도 자기 GPIO 상태를 유지한다.** 다른 펌웨어가 앰프를 켜 둔 기기는 우리 초기화가 틀려도 소리가 난다. 앰프 시험은 전원 버튼 두 번(완전 끄기) 뒤에 한다 [근거: M].
3. **IR LED·수신기 전원은 EXT_5V(PMIC 0x06 bit3)** 이고 기본은 꺼짐이다. 켜고 100 ms 기다린 뒤 쏜다 [근거: D, I].

## 핀맵 (회로도 신호 이름)

| 기능 | ESP32-S3 | 회로도 신호 | 등급 |
|---|---|---|---|
| SoC | ESP32-S3-PICO-1-N8R8 (Flash 8 MB, PSRAM 8 MB Octal) | — | [근거: D] |
| I2S MCLK / BCLK / LRCK | G18 / G17 / G15 | `G18_I2S_MCLK` `G17_I2S_BCLK` `G15_I2S_LRCK` | [근거: S] |
| I2S DOUT(→ 코덱 DAC) / DIN(← 코덱 ADC) | G14 / G16 | `G14_I2S_DDAC` `G16_I2S_DADC` | [근거: S] |
| 코덱 | ES8311, I2C 0x18 | `ES8311` | [근거: S, U] |
| 내부 I2C SDA / SCL | G47 / G48 (ES8311·BMI270·M5PM1 공유) | `G47_SDA` `G48_SCL` | [근거: S] |
| 버튼 A(정면) / B(옆) | G11 / G12, active-low, 딥슬립 깨우기 가능 | `G11_KEY1` `G12_KEY2` | [근거: S] |
| IR 송신 / 수신 | G46 / G42 | `G46_IR_TX` `G42_IR_RX` | [근거: S] |
| LCD (ST7789, 135×240) | MOSI G39 · SCK G40 · CS G41 · DC G45 · RST G21 | `G39_MOSI` `G40_SCK` `G41_LCD_CS` `G45_LCD_RS` `G21_LCD_RST` | [근거: S] |
| 백라이트 | G38 (PWM) | `G38_LCD_BL` | [근거: S] |
| 스피커 앰프 enable | PMIC G3 | `PYG3_SPK_Pulse` | [근거: S] |
| LCD·오디오 전원 | PMIC G2 | `PYG2_L3B_EN` | [근거: S] |
| IMU | BMI270 @0x68, INT → PMIC G4 | `PYG4_IMU_INT` | [근거: S, D] |
| 충전 상태 | PMIC G0 | `PYG0_CHG_STAT` | [근거: S] |
| 전원 버튼 | PMIC 가 처리(한 번 = 켜기/리셋, 두 번 = 끄기, 길게 = 다운로드). ESP 에서 읽을 GPIO 없음 | `PWR_BTN` | 동작 [근거: D] |
| Grove PORT.A | SDA G9 / SCL G10 | `GROVE_G9` `GROVE_G10` | [근거: S] |

## 펌웨어가 직접 쓰는 PMIC(M5PM1, 0x6E) 레지스터

| 레지스터 | 비트 | 뜻 | 펌웨어 |
|---|---|---|---|
| 0x06 | bit3 | EXT_5V 출력(Grove·IR) | IR 쏠 때·학습할 때만 켬 |
| 0x06 | bit4 | 녹색 LED(기본 켜짐, 1 mA 남짓) | 부팅 때 끔(`PMIC_LED_OFF`) |
| 0x11 | bit2 / bit3 | L3B(LCD·코덱 전원) / 앰프 | 딥슬립 전 끔, IR 학습 중 앰프 끔 |
| 0x24·0x25 | 16bit LE mV | USB 입력 전압 | 4000 mV 넘으면 USB 로 보고 볼륨·잠들기 결정 |

PMIC 는 작은 MCU 라 I2C 를 가끔 놓친다 → 세 번까지 다시 쓴다 [근거: M 주석].

## 알아 둘 것

- **마이크와 스피커는 같은 I2S 핀을 번갈아 쓴다**(M5Unified 구조). 한쪽을 `end()` 한 뒤 다른 쪽을 `begin()` 한다. 버튼 눌러 말하기라 반이중으로 충분하다.
- **배터리 볼륨**: 배터리일 때 75%(≈191) 넘게 키우면 순간 전류로 재부팅될 수 있다 [근거: D]. 펌웨어 기본 USB 255 / 배터리 185.
- **IR 수신 주의**: 수신은 RMT 로만, 받는 동안 스피커 앰프 끄기, 리모컨과 30 cm 이상 [근거: D].
- **첫 굽기**: 출하 UiFlow2 펌웨어는 USB 시리얼이 안 보일 수 있다. 전원 버튼 길게 = 다운로드 모드 [근거: D]. 굽기 전에 8 MB 전체를 백업해 두길 권한다: `esptool.py read_flash 0 0x800000 backup.bin`.
- **G13**: 용도를 회로도 텍스트로는 확정하지 못했다(미확인). 펌웨어는 쓰지 않는다.
