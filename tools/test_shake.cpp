// firmware/oakle_gadget/shake_detect.h 호스트 시험. 리포 루트에서:
//   c++ -std=c++17 -Wall -I firmware/oakle_gadget tools/test_shake.cpp -o /tmp/test_shake && /tmp/test_shake
// 샘플은 합성이다(실기 기록 아님). 100 Hz(BMI270 기본 ODR, loop 의 delay(10))에 ±2 ms 흔들림을 섞는다.
// 중력은 1 g, 단위는 M5.Imu.getAccel 과 같은 g. 기기를 세워 든 자세(중력 = -y)를 기본으로 한다.
#include "shake_detect.h"
#include <cstdio>
#include <cstdlib>
#include <functional>

static int fails = 0, total = 0;
#define CHECK(c) do { total++; if (!(c)) { fails++; std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); } } while (0)

struct V { float x, y, z; };
using Gen = std::function<V(float /*초*/)>;

static uint32_t rng = 12345;
static float noise(float amp) {                    // 결정적 의사 난수(-amp..amp)
  rng = rng * 1103515245u + 12345u;
  return amp * (((rng >> 8) & 0xFFFF) / 32767.5f - 1.0f);
}

// 시각 t0(ms)부터 secs 초 동안 넣고, 터진 횟수와 첫 시각을 돌려준다
struct Run { int hits; float first_s; };
static Run run(shk::State& s, const shk::Config& c, uint32_t& t, float secs, Gen g, float nz = 0.03f) {
  Run r{0, -1};
  uint32_t start = t, end = t + (uint32_t)(secs * 1000);
  while (t < end) {
    float sec = (t - start) / 1000.0f;
    V v = g(sec);
    if (shk::feed(s, c, t, v.x + noise(nz), v.y + noise(nz), v.z + noise(nz))) {
      if (!r.hits) r.first_s = sec;
      r.hits++;
    }
    t += 10 + (int)noise(2.5f);
  }
  return r;
}
static Run fresh(float secs, Gen g, const shk::Config& c = shk::Config(), float nz = 0.03f) {
  shk::State s; uint32_t t = 1000;
  return run(s, c, t, secs, g, nz);
}

static const float PI2 = 6.2831853f;
static V still(float) { return {0, -1, 0}; }
static Gen sine_axis(int axis, float amp, float hz) {      // 한 축으로 앞뒤 흔들기
  return [=](float s) { V v = still(s); float a = amp * sinf(PI2 * hz * s); (axis == 0 ? v.x : axis == 1 ? v.y : v.z) += a; return v; };
}

int main() {
  shk::Config c;

  // 1) 가만히(책상 위·손에 쥐고 가만히)
  CHECK(fresh(10, still).hits == 0);
  CHECK(fresh(10, still, c, 0.15f).hits == 0);               // 잡음 많은 쪽

  // 2) 흔들기 — 세게·보통·옆으로·앞뒤로·누운 자세
  { Run r = fresh(2, sine_axis(0, 3.0f, 4.0f)); CHECK(r.hits == 1); CHECK(r.first_s > 0 && r.first_s < 1.0f); std::printf("  세게 4Hz 3g: %.2fs\n", r.first_s); }
  { Run r = fresh(2, sine_axis(0, 2.2f, 3.0f)); CHECK(r.hits == 1); std::printf("  보통 3Hz 2.2g: %.2fs\n", r.first_s); }
  CHECK(fresh(2, sine_axis(2, 2.5f, 3.5f)).hits == 1);       // 화면 쪽 앞뒤
  CHECK(fresh(2, sine_axis(1, 2.5f, 3.5f)).hits == 1);       // 위아래(중력 축)
  CHECK(fresh(2, [](float s) { V v{0, 0, 1}; v.x += 2.6f * sinf(PI2 * 3.5f * s); return v; }).hits == 1);   // 책상에 눕힌 채 집어 흔들기
  CHECK(fresh(2, [](float s) { float a = 2.6f * sinf(PI2 * 4 * s); return V{a * 0.7f, -1 + a * 0.5f, a * 0.5f}; }).hits == 1);   // 비스듬히
  // 흔들기에 손목 비틀림(방향 30° 흔들림)이 섞여도
  CHECK(fresh(2, [](float s) { float a = 2.8f * sinf(PI2 * 4 * s), w = 0.5f * sinf(PI2 * 1.3f * s); return V{a * cosf(w), -1, a * sinf(w)}; }).hits == 1);

  // 3) 약한·느린 흔들기는 안 센다(문턱·간격이 기준이라는 확인)
  CHECK(fresh(3, sine_axis(0, 1.2f, 4.0f)).hits == 0);       // 문턱 아래
  CHECK(fresh(4, sine_axis(0, 2.5f, 1.0f)).hits == 0);       // 1 Hz: 봉우리 간격 500 ms > max_gap

  // 4) 걷기 — 손에 들고(위아래 0.35 g, 2 Hz + 좌우 0.15 g, 1 Hz)
  CHECK(fresh(20, [](float s) { V v = still(s); v.y += 0.35f * sinf(PI2 * 2 * s) + 0.1f * sinf(PI2 * 4 * s); v.x += 0.15f * sinf(PI2 * s); return v; }).hits == 0);
  // 걷기 — 바지 주머니: 발 디딜 때마다 아래로 2.5 g 충격(20 ms) + 반대 1.0 g 되튐(30 ms 뒤), 0.52 초마다 + 허벅지 흔들림 0.6 g
  auto pocket = [](float s) {
    V v{0, 0, -1};                                           // 주머니에 거꾸로·눕혀
    float ph = fmodf(s, 0.52f);
    if (ph < 0.02f) v.z -= 2.5f; else if (ph < 0.05f) v.z += 1.0f;
    v.x += 0.6f * sinf(PI2 * 0.96f * s);
    return v;
  };
  CHECK(fresh(20, pocket).hits == 0);
  // 주머니 2: 디딤 충격과 되튐이 둘 다 세서(±2.5 g, 80 ms 차) 반전 봉우리가 짝으로 온다 — 짝 사이 470 ms 가 max_gap 에 걸린다
  CHECK(fresh(20, [](float s) { V v{0, 0, -1}; float ph = fmodf(s, 0.55f); if (ph < 0.02f) v.z -= 2.5f; else if (ph >= 0.08f && ph < 0.10f) v.z += 2.5f; return v; }).hits == 0);
  // 뛰기(손에 들고): 위아래 1.2 g, 2.8 Hz
  CHECK(fresh(20, sine_axis(1, 1.2f, 2.8f)).hits == 0);

  // 5) 버튼 누름·톡 치기·내려놓기
  auto bump = [](float at, float amp) { return [=](float s) { V v = still(s); float d = s - at; if (d >= 0 && d < 0.02f) v.z += amp; else if (d >= 0.02f && d < 0.05f) v.z -= amp * 0.6f; return v; }; };
  CHECK(fresh(3, bump(1.0f, 3.0f)).hits == 0);               // 버튼 꾹(한 번)
  CHECK(fresh(3, bump(1.0f, 6.0f)).hits == 0);               // 책상에 탁 내려놓기
  // 탁 내려놓은 뒤 잔떨림(25 Hz, 줄어드는 ±3 g) — 20 ms 마다 반전 봉우리라 min_gap 이 없으면 센다
  CHECK(fresh(3, [](float s) { V v = still(s); float d = s - 1.0f; if (d >= 0 && d < 0.2f) v.x += 3.0f * expf(-d * 4) * sinf(PI2 * 25 * d + 1.5708f); return v; }).hits == 0);
  // 같은 방향으로 톡톡 5번(150 ms 간격) — 반전이 아니라서 안 센다(반전 검사가 빠지면 여기서 걸린다)
  CHECK(fresh(3, [](float s) { V v = still(s); float ph = fmodf(s, 0.15f); if (s > 0.5f && s < 1.25f && ph < 0.02f) v.z += 2.5f; return v; }).hits == 0);
  // 들어서 뒤집기(1초에 180°) — 중력 추정이 늦게 따라가도 문턱에 못 미친다
  CHECK(fresh(4, [](float s) { float a = s < 1 ? 0 : s < 2 ? (s - 1) * 3.14159f : 3.14159f; return V{0, -cosf(a), sinf(a)}; }).hits == 0);
  // 휙 돌리기(0.3초에 180°)
  CHECK(fresh(3, [](float s) { float a = s < 1 ? 0 : s < 1.3f ? (s - 1) / 0.3f * 3.14159f : 3.14159f; return V{0, -cosf(a), sinf(a)}; }).hits == 0);

  // 6) 쿨다운: 8초 내내 흔들면 두 번(첫 번 + 5초 뒤), 한 번 터진 뒤 5초 안에는 안 센다
  { Run r = fresh(8, sine_axis(0, 3.0f, 4.0f)); CHECK(r.hits == 2); std::printf("  8초 계속 흔들기: %d번\n", r.hits); }
  { Run r = fresh(5.5f, sine_axis(0, 3.0f, 4.0f)); CHECK(r.hits == 1); }
  { shk::Config c2; c2.cooldown_ms = 2000; CHECK(fresh(8, sine_axis(0, 3.0f, 4.0f), c2).hits >= 3); }   // 상수가 실제로 먹는지

  // 7) 끊김: 흔들다 멈춘 사이 loop 가 멈췄으면(녹음·재생) 이어 세지 않는다
  {
    shk::State s; uint32_t t = 1000;
    Run a = run(s, c, t, 0.45f, sine_axis(0, 3.0f, 4.0f));
    t += 3000;                                               // 3초 샘플 없음
    Run b = run(s, c, t, 0.45f, sine_axis(0, 3.0f, 4.0f));
    CHECK(a.hits == 0 && b.hits == 0);
  }
  // 짧은 멈춤(210 ms > max_dt 200)도 사슬을 끊는다: 앞 0.4초 · 멈춤 · 다시 0.45초(혼자선 봉우리 3개, 첫 봉우리가 앞의 마지막과 반대) — 이어 세면 5개를 넘는다
  {
    shk::State s; uint32_t t = 1000;
    Run a = run(s, c, t, 0.40f, sine_axis(0, 3.0f, 4.0f), 0);
    t += 210;
    Run b = run(s, c, t, 0.45f, sine_axis(0, 3.0f, 4.0f), 0);
    CHECK(a.hits == 0 && b.hits == 0);
  }
  // reset() 도 같다 — 버튼을 누른 동안 펌웨어가 부른다
  {
    shk::State s; uint32_t t = 1000;
    Run a = run(s, c, t, 0.45f, sine_axis(0, 3.0f, 4.0f));
    shk::reset(s);
    Run b = run(s, c, t, 0.45f, sine_axis(0, 3.0f, 4.0f));
    CHECK(a.hits == 0 && b.hits == 0);
  }
  // reset() 은 쿨다운을 지우지 않는다
  {
    shk::State s; uint32_t t = 1000;
    CHECK(run(s, c, t, 1.5f, sine_axis(0, 3.0f, 4.0f)).hits == 1);
    shk::reset(s);
    CHECK(run(s, c, t, 2.0f, sine_axis(0, 3.0f, 4.0f)).hits == 0);
  }
  // 로그용 값
  {
    shk::State s; uint32_t t = 1000;
    run(s, c, t, 1.5f, sine_axis(0, 3.0f, 4.0f));
    CHECK(s.last_peaks == c.need && s.last_max_g > 2.5f && s.last_max_g < 4.0f && s.last_dur_ms > 300 && s.last_dur_ms < c.window_ms);
  }
  // 8) 8 g 포화(BMI270 ±8 g)에서도 센다
  //    아주 세게 흔들면 거의 네모파라 0 근처 샘플이 없다 — 문턱 위에서 방향이 뒤집히는 것도 봉우리 끝으로 본다
  for (float hz : {4.0f, 5.0f, 6.0f})
    for (float nz : {0.0f, 0.03f})
      CHECK(fresh(2, [=](float s) { V v = sine_axis(0, 12.0f, hz)(s); if (v.x > 8) v.x = 8; if (v.x < -8) v.x = -8; return v; }, c, nz).hits == 1);

  std::printf("shake: %d/%d ok\n", total - fails, total);
  return fails ? 1 : 0;
}
