// 흔들기 감지 — Arduino 없이도 컴파일된다(tools/test_shake.cpp 가 PC 에서 시험)
// 입력은 가속도(g, 중력 포함)와 시각(ms)뿐. IMU 읽기·반응은 oakle_gadget.ino 쪽.
//
// 방식: 느린 저역 통과로 중력을 빼고 남은 「움직임 가속도」 크기가 thr_g 를 넘었다가 thr_g/2 아래로 내려오면 봉우리 하나.
//   (문턱 위에서 방향이 확 뒤집혀도 끝 — ±8 g 포화 땐 0 근처 샘플이 없다) 봉우리마다 그 구간의 최대 벡터를 방향으로 쓴다. 직전 봉우리와 방향이 반대(cos < reverse_cos)이고 간격이
//   min_gap_ms~max_gap_ms 이면 사슬을 잇고, 아니면 이번 봉우리부터 새 사슬. 사슬이 need 개(window_ms 안)가 되면 흔들기.
//   - 걷기·주머니: 움직임이 문턱보다 작거나, 발 디딤 충격은 한쪽 방향이고 간격(≈500 ms)이 max_gap_ms 보다 길다
//   - 버튼 누름·톡 치기·내려놓기: 봉우리 1~2개 → need 에 못 미침. 같은 방향으로 여러 번 톡톡 → 방향 반전이 아님
//   - 샘플 사이가 max_dt_ms 보다 벌어지면(loop 가 녹음·재생·Wi-Fi 등으로 멈췄다) 처음부터
//   - 한 번 터지면 cooldown_ms 동안 안 센다
#pragma once
#include <stdint.h>
#include <math.h>

namespace shk {

struct Config {
  float thr_g = 1.8f;           // 봉우리 문턱(중력 뺀 크기, g)
  int need = 5;                 // 방향이 번갈아 바뀐 봉우리 수(5 = 왕복 두 번 반)
  uint32_t window_ms = 1500;    // 사슬 첫 봉우리부터 이 안에 need 개
  uint32_t min_gap_ms = 60;     // 봉우리 사이 최소(한 충격의 떨림을 여러 번 세지 않게)
  uint32_t max_gap_ms = 400;    // 봉우리 사이 최대(이보다 느리면 흔들기가 아니다)
  uint32_t cooldown_ms = 5000;  // 터진 뒤 쉬는 시간
  float reverse_cos = -0.5f;    // 이보다 작으면 「반대 방향」(120° 넘게 꺾임)
  float tau_ms = 250.0f;        // 중력 추정 저역 통과 시정수
  uint32_t max_dt_ms = 200;     // 샘플 사이가 이보다 길면 처음부터
};

struct State {
  bool init = false;
  uint32_t last_t = 0;
  float g[3] = {0, 0, 0};       // 중력 추정
  bool in_peak = false;
  float pk[3] = {0, 0, 0};      // 지금 봉우리의 최대 벡터
  float pk_mag = 0;
  uint32_t pk_t = 0;
  float prev[3] = {0, 0, 0};    // 직전 봉우리 방향
  uint32_t prev_t = 0;
  int n = 0;                    // 사슬 길이
  uint32_t first_t = 0;         // 사슬 첫 봉우리 시각
  float chain_max = 0;          // 사슬 안 최대 크기(로그용)
  bool cooling = false;
  uint32_t cool_until = 0;
  // 마지막으로 터진 흔들기(로그용)
  int last_peaks = 0;
  float last_max_g = 0;
  uint32_t last_dur_ms = 0;
};

// 녹음·재생·버튼 등으로 쉬었다가 다시 볼 때. 쿨다운은 남긴다
inline void reset(State& s) {
  bool c = s.cooling; uint32_t cu = s.cool_until;
  s = State();
  s.cooling = c; s.cool_until = cu;
}

inline void end_peak(State& s, const Config& c) {
  s.in_peak = false;
  float m = s.pk_mag;
  if (m <= 0) return;
  float d[3] = {s.pk[0] / m, s.pk[1] / m, s.pk[2] / m};
  bool linked = false;
  if (s.n > 0) {
    uint32_t gap = s.pk_t - s.prev_t;
    float cs = d[0] * s.prev[0] + d[1] * s.prev[1] + d[2] * s.prev[2];
    if (gap < c.min_gap_ms) {                      // 같은 충격의 떨림: 센 쪽만 남기고 넘어간다
      return;
    }
    linked = gap <= c.max_gap_ms && cs < c.reverse_cos && s.pk_t - s.first_t <= c.window_ms;
  }
  if (linked) {
    s.n++;
    if (m > s.chain_max) s.chain_max = m;
  } else {
    s.n = 1; s.first_t = s.pk_t; s.chain_max = m;
  }
  for (int i = 0; i < 3; i++) s.prev[i] = d[i];
  s.prev_t = s.pk_t;
}

// 샘플 하나. 흔들기가 막 끝까지 세어졌으면 true(그 뒤 cooldown_ms 동안은 false)
inline bool feed(State& s, const Config& c, uint32_t t, float ax, float ay, float az) {
  float a[3] = {ax, ay, az};
  if (!s.init || t - s.last_t > c.max_dt_ms) {
    reset(s);
    s.init = true;
    for (int i = 0; i < 3; i++) s.g[i] = a[i];
    s.last_t = t;
    return false;
  }
  float dt = (float)(t - s.last_t);
  s.last_t = t;
  float k = dt / (c.tau_ms + dt);
  float d[3];
  for (int i = 0; i < 3; i++) {
    s.g[i] += k * (a[i] - s.g[i]);
    d[i] = a[i] - s.g[i];
  }
  float m = sqrtf(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
  if (s.cooling) {
    if ((int32_t)(t - s.cool_until) < 0) return false;
    s.cooling = false;
    s.n = 0; s.in_peak = false;
  }
  if (!s.in_peak) {
    if (m > c.thr_g) {
      s.in_peak = true; s.pk_mag = m; s.pk_t = t;
      for (int i = 0; i < 3; i++) s.pk[i] = d[i];
    }
    return false;
  }
  // 세게 흔들면 ±8 g 에 포화돼 0 근처를 지나는 샘플이 없을 수 있다 → 문턱 위에서 방향이 뒤집혀도 봉우리를 끊고 새로 연다
  bool flipped = false;
  if (m > c.thr_g) {
    float cs = (d[0] * s.pk[0] + d[1] * s.pk[1] + d[2] * s.pk[2]) / (m * s.pk_mag);
    flipped = cs < c.reverse_cos;
  }
  if (!flipped) {
    if (m > s.pk_mag) {
      s.pk_mag = m; s.pk_t = t;
      for (int i = 0; i < 3; i++) s.pk[i] = d[i];
    }
    if (m >= c.thr_g * 0.5f) return false;
  }
  end_peak(s, c);
  if (flipped) {
    s.in_peak = true; s.pk_mag = m; s.pk_t = t;
    for (int i = 0; i < 3; i++) s.pk[i] = d[i];
  }
  if (s.n >= c.need) {
    s.last_peaks = s.n; s.last_max_g = s.chain_max; s.last_dur_ms = s.prev_t - s.first_t;
    s.n = 0;
    s.cooling = true; s.cool_until = t + c.cooldown_ms;
    return true;
  }
  return false;
}

}  // namespace shk
