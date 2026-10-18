// firmware/oakle_gadget/wifi_list.h 호스트 시험. 리포 루트에서:
//   c++ -std=c++17 -Wall -I firmware/oakle_gadget tools/test_wifi_list.cpp -o /tmp/test_wifi_list && /tmp/test_wifi_list /tmp/wifi_portal.html
// 둘째 인자를 주면 시험용 설정 페이지를 그 파일에 쓴다(브라우저로 열어 눈으로 확인용).
// 이름은 일부러 한글·따옴표·꺾쇠를 섞는다 — ASCII 만으로 시험하면 인코딩 고장이 숨는다
#include "wifi_list.h"
#include <cstdio>
#include <cstdlib>
#include <fstream>

static int fails = 0, total = 0;
#define CHECK(c) do { total++; if (!(c)) { fails++; std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); } } while (0)

int main(int argc, char** argv) {
  using namespace wl;
  List l{};

  // 값 검사
  CHECK(upsert(l, "", "12345678", true) == BAD_SSID);
  CHECK(upsert(l, "123456789012345678901234567890123", "", true) == BAD_SSID);   // 33바이트
  CHECK(upsert(l, "집", "1234567", true) == BAD_PASS);                            // 7자
  CHECK(upsert(l, "집", std::string(64, 'z').c_str(), true) == BAD_PASS);         // 64자인데 16진 아님
  CHECK(upsert(l, "집", std::string(64, 'a').c_str(), true) == ADDED);            // 64자 16진 PSK
  CHECK(remove(l, "집") && l.count == 0);
  // 한글 SSID 32바이트 경계: 「가」 3바이트 × 10 = 30 + 「ab」 = 32 → 통과, 11자 = 33 → 거절
  std::string k10;
  for (int i = 0; i < 10; i++) k10 += "가";
  CHECK(upsert(l, (k10 + "ab").c_str(), "", false) == ADDED);
  CHECK(upsert(l, (k10 + "가").c_str(), "", false) == BAD_SSID);
  clear(l);

  // 추가·고침·순서(새 것이 앞)
  CHECK(upsert(l, "우리집 5G", "home-pass-1", true) == ADDED);
  CHECK(upsert(l, "Sam's \"phone\" <hot>", "hot-pass-1", false) == ADDED);
  CHECK(l.count == 2 && strcmp(l.n[0].ssid, "Sam's \"phone\" <hot>") == 0);
  CHECK(upsert(l, "우리집 5G", "", false) == UPDATED);                            // 비번 비움 = 예전 그대로, 집 표시만 바뀜
  CHECK(strcmp(l.n[1].pass, "home-pass-1") == 0 && !l.n[1].home);
  CHECK(upsert(l, "우리집 5G", "home-pass-2", true) == UPDATED);
  CHECK(strcmp(l.n[1].pass, "home-pass-2") == 0 && l.n[1].home);

  // 꽉 차면 FULL, 아무것도 안 빠진다
  CHECK(upsert(l, "c", "", false) == ADDED);
  CHECK(upsert(l, "d", "", false) == ADDED);
  CHECK(upsert(l, "e", "", false) == ADDED);
  CHECK(l.count == MAX_NETS);
  CHECK(upsert(l, "f", "", false) == FULL);
  CHECK(l.count == MAX_NETS && find(l, "우리집 5G") >= 0);
  CHECK(upsert(l, "e", "", true) == UPDATED);                                      // 꽉 차도 고치기는 된다
  CHECK(remove(l, "c") && !remove(l, "c") && l.count == 4);
  CHECK(remove(l, "d") && remove(l, "e"));

  // 경로 판정
  CHECK(route_of(l, "우리집 5G", true) == 1);
  CHECK(route_of(l, "Sam's \"phone\" <hot>", true) == 2);
  CHECK(route_of(l, "Sam's \"phone\" <hot>", false) == 1);                         // 중계 비밀 없는 펌웨어 = 전부 집
  CHECK(route_of(l, "모르는 망", true) == 0);

  // 붙는 순서
  upsert(l, "카페", "cafe-pass", false);                                            // [카페, 핫스팟, 우리집]
  int out[MAX_NETS];
  bool blind;
  ScanItem s1[] = {{"카페", -40}, {"Sam's \"phone\" <hot>", -70}, {"우리집 5G", -80}, {"남의 집", -30}};
  int n = order(l, s1, 4, true, false, false, out, &blind);
  CHECK(n == 3 && !blind);
  CHECK(strcmp(l.n[out[0]].ssid, "우리집 5G") == 0);                              // 약해도 집이 먼저(기존 동작)
  CHECK(strcmp(l.n[out[1]].ssid, "카페") == 0);                                   // 바깥끼리는 센 순
  n = order(l, s1, 4, true, true, false, out, &blind);                              // PREFER_OUTDOOR
  CHECK(n == 3 && strcmp(l.n[out[0]].ssid, "카페") == 0 && strcmp(l.n[out[2]].ssid, "우리집 5G") == 0);
  ScanItem s2[] = {{"남의 집", -30}, {"", -50}};
  n = order(l, s2, 2, true, false, false, out, &blind);
  CHECK(n == 0 && blind);                                                            // 재연결 땐 안 보이면 안 붙어 본다
  n = order(l, s2, 2, true, false, true, out, &blind);
  CHECK(n == 3 && blind && strcmp(l.n[out[0]].ssid, "우리집 5G") == 0);            // 부팅 땐 전부, 집 먼저
  ScanItem s3[] = {{"카페", -90}, {"카페", -45}};                                   // 같은 이름 AP 여럿이면 센 쪽
  CHECK(order(l, s3, 2, true, false, false, out, &blind) == 1);

  // 시드: config.h 값 → 처음 한 번, 바뀌었을 때만
  List e{};
  Net seeds[2] = {{"우리집 5G", "home-pass-1", true}, {"Sam's \"phone\" <hot>", "hot-pass-1", false}};
  int skipped;
  CHECK(merge_seeds(e, seeds, 2, &skipped) == 2 && skipped == 0);
  CHECK(strcmp(e.n[0].ssid, "우리집 5G") == 0 && e.n[0].home && !e.n[1].home);
  uint32_t h1 = seed_hash(seeds, 2);
  CHECK(h1 != 0 && h1 == seed_hash(seeds, 2));
  Net seeds2[2] = {{"우리집 5G", "home-pass-9", true}, {"Sam's \"phone\" <hot>", "hot-pass-1", false}};
  CHECK(seed_hash(seeds2, 2) != h1);                                                 // 비번만 바꿔도 다시 합친다
  CHECK(merge_seeds(e, seeds2, 2, &skipped) == 0 && strcmp(e.n[0].pass, "home-pass-9") == 0);
  CHECK(seed_hash(seeds, 1) != seed_hash(seeds, 2) && seed_hash(nullptr, 0) != 0);

  // 페이지: 이스케이프, 비밀번호 비노출, 집 칸은 바깥 경로가 있을 때만
  std::string html = page(l, s1, 4, true, "Oakle-Gadget-1A2B", "저장했어요 <ok>", false);
  CHECK(html.find("Sam&#39;s &quot;phone&quot; &lt;hot&gt;") != std::string::npos);
  CHECK(html.find("<hot>") == std::string::npos);
  CHECK(html.find("저장했어요 &lt;ok&gt;") != std::string::npos);
  CHECK(html.find("home-pass") == std::string::npos && html.find("cafe-pass") == std::string::npos && html.find("hot-pass") == std::string::npos);
  CHECK(html.find("name=h") != std::string::npos);
  CHECK(html.find("(3/5)") != std::string::npos);
  CHECK(html.find("남의 집") != std::string::npos);
  size_t at = html.find("data-s=\"카페\"");
  CHECK(at != std::string::npos && html.find("저장됨", at) < html.find("</button>", at));
  CHECK(html.find("data-s=\"남의 집\"") < html.find("data-s=\"카페\""));          // 센 것(-30)이 위
  std::string html2 = page(l, s3, 2, false, "Oakle-Gadget-1A2B", "", false);
  CHECK(html2.find("name=h") == std::string::npos);
  size_t c1 = html2.find("data-s=\"카페\"");
  CHECK(c1 != std::string::npos && html2.find("data-s=\"카페\"", c1 + 1) == std::string::npos);   // 같은 이름 한 번
  List z{};
  CHECK(page(z, nullptr, 0, true, "x", "", false).find("보이는 망이 없어요") != std::string::npos);

  if (argc > 1) { std::ofstream(argv[1]) << html; std::printf("page -> %s (%zu bytes)\n", argv[1], html.size()); }
  std::printf("wifi_list: %d/%d ok\n", total - fails, total);
  return fails ? 1 : 0;
}
