# IR 리모컨 (집 모드)

「티비 꺼 줘」라고 말하면 기기 머리의 IR LED(G46)로 TV 전원 신호를 쏜다. 서버가 아니라 **말한 기기에서** 신호가 나간다.

```
에이전트 답 「[리모컨:lg_off] TV 끄는 신호를 보냈어요」
  → voice_bridge.pick_action 이 표지를 떼어 응답 헤더 X-Oakle-Action: {"ir":"lg_off"}
  → 펌웨어 do_action → NEC 38 kHz 송신 → 화면 「TV 끄기(기본) 신호 보냄 — 결과는 기기를 봐 주세요」
```

- 허용 목록: `lg_power`·`lg_on`·`lg_off`·`circ_power`. 그 밖의 이름은 서버가 버리고(로그 `action dropped:`), 표지는 이름이 무엇이든 읽히는 답에서 지운다.
- 기기는 TV 상태를 모른다. 그래서 「껐어요」가 아니라 「끄는 신호를 보냈어요」라고 말하게 지시문(`ACT_ASK`)에 적어 두었다.
- `DEVICE_ACTIONS=0` 이면 끈다(서버). 바깥 모드(중계)에는 이 기능이 없다.

## 코드

| 이름 | NEC 32bit(MSB 표기) | 비고 |
|---|---|---|
| `lg_power` | `0x20DF10EF` | LG TV 전원 토글. 널리 공개된 코드 |
| `lg_on` / `lg_off` | `0x20DF23DC` / `0x20DFA35C` | LG 전용 켜기·끄기. 모델에 따라 안 먹을 수 있어 **기본은 둘 다 토글**(`IR_LG_DISCRETE=0`) |
| `circ_power` | `0x00FF00FF` | 선풍기·서큘레이터 전원 토글 **예시**(주소 0x00·명령 0x00, 값싼 범용 리모컨에 흔함). 자기 기기 코드로 바꿔 쓴다 |

자기 기기 코드를 알아내려면 아래 학습 모드의 시리얼 출력(`ir learn: proto=NEC value=0x…`)을 읽어 `oakle_gadget.ino` 의 상수를 바꾼다. 다른 기기를 더하려면 서버 `IR_ACTIONS`·`ACT_ASK`, 펌웨어 `do_action` 세 곳을 함께 고친다.

## 학습 모드 (버튼 B 3초)

- G42 RMT 수신(1 µs 틱, 4블록, 15 ms 조용하면 프레임 끝)으로 15초 기다린다. 받는 동안 스피커 앰프를 끄고 EXT_5V 를 켠다.
- 리모컨을 30 cm 떨어져 기기 쪽으로 겨누고 버튼을 누른다. 원시 mark/space(µs)를 NVS `ir/tv_power` 에 저장한다.
- 앞에 붙은 짧은 잡음은 떼고(`ir_trim`), 헤더 9 ms(NEC)·4.5 ms(NEC45) 둘 다 해석한다.
- `IR_USE_LEARNED=1` 이면 TV 동작에 학습 신호를 기본 코드 대신 쏜다. 학습 신호엔 잡음·끊긴 헤더가 섞일 수 있어 기본은 0 이다.

## 송신이 하나도 안 먹을 때

코드 값보다 **송신 경로**를 먼저 본다. 모든 코드가 같은 송신 함수를 지나므로, 값을 바꿔 보는 건 아무것도 시험하지 못한다.

- 반송파 극성: 코어 3.3.x 의 `rmtSetCarrier(pin, en, carrier_level, freq, duty)` 셋째 인자는 실제로 `polarity_active_low` 로 넘어간다. **false** 여야 반송파가 mark 에 실린다(M5 공식 StickS3 NEC 예제와 같음). true 로 두면 TV 가 거의 반응하지 않았다.
- EXT_5V 를 켠 뒤 100 ms 기다린다. 시리얼 `ir: nec 0x… ext5v=ok sent` 로 전원 켜기 성공을 확인한다.
- 휴대폰 카메라로 IR LED 가 깜박이는지 본다.
