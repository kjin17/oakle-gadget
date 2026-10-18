// 저장 Wi-Fi 목록과 설정 페이지 — Arduino 없이도 컴파일된다(tools/test_wifi_list.cpp 가 PC 에서 시험)
// NVS 읽기·쓰기와 WebServer 는 oakle_gadget.ino 쪽에 있고, 여기는 값만 다룬다.
//
// 항목 = SSID + 비밀번호 + 「집」 표시. 집 표시가 붙은 망은 집 모드(LAN 의 voice_bridge), 아니면 바깥 모드(중계 서버).
// 펌웨어에 RELAY_SECRET 이 없으면(have_outdoor=false) 바깥 경로가 없으므로 모든 망을 집으로 본다.
// 붙는 순서: 보이는 저장 망 중 집 먼저(PREFER_OUTDOOR 면 바깥 먼저), 같은 쪽끼리는 신호 센 것부터.
#pragma once
#include <stdint.h>
#include <string.h>
#include <string>

namespace wl {

#ifdef WIFI_MAX_NETS
static const int MAX_NETS = WIFI_MAX_NETS;
#else
static const int MAX_NETS = 5;
#endif

struct Net {
  char ssid[33];   // 바이트 기준 최대 32(한글은 3바이트라 10자 남짓)
  char pass[65];   // 0(열린 망) 또는 8~63자, 64자면 16진 PSK
  bool home;
};

struct List {
  Net n[MAX_NETS];
  int count;
};

struct ScanItem {
  const char* ssid;
  int rssi;
};

enum Result { ADDED, UPDATED, FULL, BAD_SSID, BAD_PASS };

inline const char* result_text(Result r) {
  switch (r) {
    case ADDED:    return "저장했어요";
    case UPDATED:  return "고쳐 저장했어요";
    case FULL:     return "저장 칸이 다 찼어요. 아래에서 하나 지우고 다시 저장해 주세요";
    case BAD_SSID: return "Wi-Fi 이름이 비었거나 너무 길어요(32바이트까지, 한글은 10자 남짓)";
    case BAD_PASS: return "비밀번호는 비워 두거나(열린 망) 8~63자여야 해요";
  }
  return "";
}

inline int find(const List& l, const char* ssid) {
  for (int i = 0; i < l.count; i++)
    if (strcmp(l.n[i].ssid, ssid) == 0) return i;
  return -1;
}

inline bool ssid_ok(const char* s) { size_t n = strlen(s); return n >= 1 && n <= 32; }
inline bool pass_ok(const char* p) {
  size_t n = strlen(p);
  if (n == 0 || (n >= 8 && n <= 63)) return true;
  if (n != 64) return false;
  for (size_t i = 0; i < n; i++)
    if (!strchr("0123456789abcdefABCDEF", p[i])) return false;
  return true;
}

// 같은 SSID 가 있으면 고친다(비밀번호를 비워 보내면 예전 것을 그대로 둔다 — 집 표시만 바꿀 때).
// 새 망은 맨 앞에 넣는다(목록은 최근 것부터). 꽉 찼으면 아무것도 지우지 않고 FULL — 몰래 빠지는 망이 없게
inline Result upsert(List& l, const char* ssid, const char* pass, bool home) {
  if (!ssid_ok(ssid)) return BAD_SSID;
  int i = find(l, ssid);
  if (i >= 0) {
    if (pass[0]) {
      if (!pass_ok(pass)) return BAD_PASS;
      strcpy(l.n[i].pass, pass);
    }
    l.n[i].home = home;
    return UPDATED;
  }
  if (!pass_ok(pass)) return BAD_PASS;
  if (l.count >= MAX_NETS) return FULL;
  memmove(&l.n[1], &l.n[0], sizeof(Net) * l.count);
  strcpy(l.n[0].ssid, ssid);
  strcpy(l.n[0].pass, pass);
  l.n[0].home = home;
  l.count++;
  return ADDED;
}

inline bool remove(List& l, const char* ssid) {
  int i = find(l, ssid);
  if (i < 0) return false;
  memmove(&l.n[i], &l.n[i + 1], sizeof(Net) * (l.count - i - 1));
  l.count--;
  return true;
}

inline void clear(List& l) { l.count = 0; }

// 0 = 모름, 1 = 집 모드, 2 = 바깥 모드
inline int route_of(const List& l, const char* ssid, bool have_outdoor) {
  int i = find(l, ssid);
  if (i < 0) return 0;
  return (l.n[i].home || !have_outdoor) ? 1 : 2;
}

// 붙어 볼 순서(목록 번호)를 out 에 채우고 개수를 돌려준다. *blind = 저장 망이 하나도 안 보였다
// 안 보였으면 allow_blind 일 때만 저장 망 전부를 집 먼저 차례로(숨은 SSID·스캔 실패 대비, 부팅 때만 — 망당 WIFI_WAIT_MS)
inline int order(const List& l, const ScanItem* scan, int n_scan, bool have_outdoor, bool prefer_out,
                 bool allow_blind, int out[MAX_NETS], bool* blind) {
  int best[MAX_NETS];
  int k = 0;
  for (int i = 0; i < l.count; i++) {
    int r = -1000;
    for (int j = 0; j < n_scan; j++)
      if (scan[j].ssid && strcmp(scan[j].ssid, l.n[i].ssid) == 0 && scan[j].rssi > r) r = scan[j].rssi;
    best[i] = r;
    if (r > -1000) out[k++] = i;
  }
  *blind = k == 0;
  if (*blind) {
    if (!allow_blind) return 0;
    for (int i = 0; i < l.count; i++) { out[k++] = i; best[i] = 0; }
  }
  // 집 먼저(또는 바깥 먼저), 그다음 신호 센 순, 같으면 목록 순 — 삽입 정렬(최대 5개)
  auto rank = [&](int i) { bool h = l.n[i].home || !have_outdoor; return (prefer_out ? !h : h) ? 0 : 1; };
  for (int a = 1; a < k; a++) {
    int v = out[a], b = a - 1;
    while (b >= 0 && (rank(out[b]) > rank(v) || (rank(out[b]) == rank(v) && best[out[b]] < best[v]))) { out[b + 1] = out[b]; b--; }
    out[b + 1] = v;
  }
  return k;
}

// config.h 시드(WIFI_SSID·HOTSPOT_SSID)의 지문. NVS 에 적어 두고, 시드가 바뀐 펌웨어를 구웠을 때만 다시 합친다
// → 포털에서 지운 시드 망이 재부팅마다 되살아나지 않는다
inline uint32_t seed_hash(const Net* s, int n) {
  uint32_t h = 2166136261u;
  auto mix = [&](const char* p) { for (; *p; p++) { h ^= (uint8_t)*p; h *= 16777619u; } h ^= 0xFF; h *= 16777619u; };
  for (int i = 0; i < n; i++) { mix(s[i].ssid); mix(s[i].pass); mix(s[i].home ? "1" : "0"); }
  return h ? h : 1;   // 0 은 「아직 시드 안 함」 자리
}

// 시드를 합친다. 반환 = 새로 들어간 개수. 꽉 차서 못 넣은 것은 *skipped
inline int merge_seeds(List& l, const Net* s, int n, int* skipped) {
  int added = 0;
  *skipped = 0;
  for (int i = n - 1; i >= 0; i--) {            // 뒤에서부터 넣어야 첫 시드(집)가 맨 앞
    Result r = upsert(l, s[i].ssid, s[i].pass, s[i].home);
    if (r == ADDED) added++;
    else if (r != UPDATED) (*skipped)++;
  }
  return added;
}

// ---- 설정 페이지 HTML ----
inline void esc(std::string& o, const char* s) {
  for (; *s; s++) {
    switch (*s) {
      case '&': o += "&amp;"; break;
      case '<': o += "&lt;"; break;
      case '>': o += "&gt;"; break;
      case '"': o += "&quot;"; break;
      case '\'': o += "&#39;"; break;
      default: o += *s;
    }
  }
}

// scan 은 정렬·중복 제거 전 그대로 넘긴다. 숨은 SSID(빈 이름)는 뺀다. 비밀번호는 절대 페이지에 싣지 않는다
inline std::string page(const List& l, const ScanItem* scan, int n_scan, bool have_outdoor,
                        const char* ap_name, const char* msg, bool msg_err) {
  std::string o;
  o.reserve(4096);
  o += "<!doctype html><html lang=ko><head><meta charset=utf-8>"
       "<meta name=viewport content='width=device-width,initial-scale=1'><title>오클보이스 Wi-Fi 설정</title><style>"
       "body{font-family:-apple-system,sans-serif;margin:0 auto;max-width:28em;padding:1em;background:#111;color:#eee}"
       "h1{font-size:1.2em}h2{font-size:1em;margin-top:1.5em;color:#aaa}"
       "button,input{font-size:1em;padding:.6em;border-radius:.4em;border:1px solid #444;background:#222;color:#eee;box-sizing:border-box}"
       "input[type=text],input[type=password]{width:100%;margin:.3em 0}"
       ".n{display:flex;justify-content:space-between;width:100%;margin:.2em 0;text-align:left}"
       ".m{padding:.6em;border-radius:.4em;background:#143}.e{background:#511}.s{color:#8c8}.d{color:#f88;border-color:#733}"
       "small{color:#999}</style></head><body><h1>오클보이스 Wi-Fi 설정</h1>";
  if (msg && *msg) {
    o += msg_err ? "<p class='m e'>" : "<p class=m>";
    esc(o, msg);
    o += "</p>";
  }
  o += "<h2>주변 Wi-Fi <small>(눌러서 고르기)</small></h2>";
  // 같은 이름은 가장 센 것 하나만, 센 순서
  int idx[64];
  int k = 0;
  for (int j = 0; j < n_scan && k < 64; j++) {
    if (!scan[j].ssid || !scan[j].ssid[0] || strlen(scan[j].ssid) > 32) continue;
    int dup = -1;
    for (int t = 0; t < k; t++)
      if (strcmp(scan[idx[t]].ssid, scan[j].ssid) == 0) dup = t;
    if (dup >= 0) { if (scan[j].rssi > scan[idx[dup]].rssi) idx[dup] = j; continue; }
    idx[k++] = j;
  }
  for (int a = 1; a < k; a++) {
    int v = idx[a], b = a - 1;
    while (b >= 0 && scan[idx[b]].rssi < scan[v].rssi) { idx[b + 1] = idx[b]; b--; }
    idx[b + 1] = v;
  }
  if (k == 0) o += "<p><small>보이는 망이 없어요. 아래에 이름을 직접 적어도 돼요(숨은 망).</small></p>";
  for (int t = 0; t < k; t++) {
    const ScanItem& s = scan[idx[t]];
    int bars = s.rssi >= -55 ? 4 : s.rssi >= -67 ? 3 : s.rssi >= -78 ? 2 : 1;
    o += "<button type=button class=n data-s=\"";
    esc(o, s.ssid);
    o += "\" onclick='pick(this)'><span>";
    esc(o, s.ssid);
    if (find(l, s.ssid) >= 0) o += " <span class=s>저장됨</span>";
    o += "</span><span>";
    for (int b = 0; b < 4; b++) o += b < bars ? "▮" : "▯";
    o += "</span></button>";
  }
  o += "<p><a href=/scan style='color:#9cf'>다시 찾기</a></p>"
       "<h2>저장</h2><form method=post action=/save>"
       "<input type=text name=s id=s maxlength=32 placeholder='Wi-Fi 이름' autocapitalize=off autocorrect=off required>"
       "<input type=password name=p id=p maxlength=64 placeholder='비밀번호(열린 망이면 비움)'>";
  if (have_outdoor)
    o += "<label><input type=checkbox name=h value=1> 집 Wi-Fi (LAN 서버 직통)</label><br>"
         "<small>체크 안 하면 바깥 모드(중계 서버)로 써요. 휴대폰 핫스팟은 체크하지 마세요.</small><br>";
  o += "<small>이미 저장된 이름이면 고쳐 저장해요. 비밀번호를 비우면 예전 비밀번호 그대로.</small><br><br>"
       "<button>저장하고 다시 켜기</button></form>";
  o += "<h2>저장된 망 (";
  o += std::to_string(l.count) + "/" + std::to_string(MAX_NETS) + ")</h2>";
  if (l.count == 0) o += "<p><small>아직 없어요.</small></p>";
  for (int i = 0; i < l.count; i++) {
    o += "<form method=post action=/forget class=n><span>";
    esc(o, l.n[i].ssid);
    if (have_outdoor) o += l.n[i].home ? " <small>집</small>" : " <small>바깥</small>";
    o += "</span><input type=hidden name=s value=\"";
    esc(o, l.n[i].ssid);
    o += "\"><button class=d>지우기</button></form>";
  }
  if (l.count)
    o += "<form method=post action=/wipe onsubmit=\"return confirm('저장된 망을 모두 지울까요?')\">"
         "<br><button class=d>모두 지우기</button></form>";
  o += "<p><small>설정 Wi-Fi: ";
  esc(o, ap_name);
  o += " · 기기 A 버튼 = 설정 모드 나가기</small></p>"
       "<script>function pick(b){document.getElementById('s').value=b.dataset.s;var p=document.getElementById('p');p.value='';p.focus()}</script>"
       "</body></html>";
  return o;
}

}  // namespace wl
