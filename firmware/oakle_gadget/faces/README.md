# faces/ — 표정 그림 자리

이 폴더의 그림 데이터는 리포에 없다(`.gitignore`). 없어도 빌드된다. 그때는 펌웨어가 도형 얼굴을 그린다.

## 파일

- `faces_data.h` 하나. `#include "faces/faces_data.h"` 를 `__has_include` 로 찾는다.
- 안에 있는 것: `FACE_W`·`FACE_H`(120), 그림마다 `static const uint16_t face_NN_이름[120*120]`(RGB565, 왼쪽 위부터 가로줄 순서),
  표 `FACE_TABLE[] = {{"이름", 배열}, …}` 와 `FACE_COUNT`.
- 값은 **보통 순서 RGB565**(`0xRRRR_RGGG_GGGB_BBBB`, 빨강이 위 비트). 펌웨어가 `M5.Display.setSwapBytes(true)` 를 켜고 그린다.
  LGFX 기본(`false`)은 `uint16_t*` 를 바이트가 뒤집힌 swap565 로 읽어 그림이 연두·무지개 잡음으로 깨진다(실기에서 겪었다).
  데이터가 의심되면 헤더를 PNG 로 되살려 본다(값을 그대로 풀면 멀쩡, 바이트를 뒤집어 풀면 실기와 같은 깨짐).

## 원본 그림 규칙

- 정사각 PNG, 투명 배경 권장(검정 위에 합성된다). 크기는 상관없다(360×360 을 썼다).
- 파일 이름 `NN_이름.png`, 20장: `01_joy 02_sad 03_angry 04_love 05_surprised 06_shy 07_gloomy 08_lol 09_ok 10_no
  11_thanks 12_sorry 13_congrats 14_fighting 15_hello 16_eat 17_sleepy 18_curious 19_yes 20_mindblown`.
  이름은 서버 `X-Oakle-Emotion` 값과 같다(`server/voice_bridge.py` 의 `EMOTIONS`).

## 만드는 법

```
pip install pillow        # 가상환경 권장
python3 tools/make_face_assets.py <원본 폴더>            # → firmware/oakle_gadget/faces/faces_data.h
python3 tools/make_face_assets.py <원본 폴더> --compare  # PNG 로 했을 때 크기도 찍어 본다
```

## 크기와 포맷을 RGB565 로 고른 이유

| 포맷 | 20장 플래시 | 그리기 |
|---|---|---|
| RGB565 배열 (채택) | 562 KB (576,000 B) | `pushImage` 로 바로 보냄. 120×120×2 B 를 SPI 40 MHz 로 약 6 ms [추론] |
| PNG 256색 (`drawPng`) | 약 136 KB | 매번 풀어야 함. 수십 ms + 해제용 RAM [추론] |

StickS3 는 플래시 8 MB 라 562 KB 는 여유가 있다. 앱 영역이 모자라면 PNG 로 바꾼다.
헤더 파일 자체는 약 2 MB 의 텍스트라 컴파일이 조금 느리다.
