#!/usr/bin/env python3
"""push·배포·공개 전 민감정보 점검. 리포(또는 내보낸 트리) 안의 파일을 읽기만 한다.

■ 무엇을 보는가
  secret_value   운영자 기기의 실제 비밀 값이 파일에 그대로 들어 있다 (값은 출력하지 않는다)
  term           리포 밖 목록에 적은 개인 용어(실명·사용자명·기기 이름·서버 주소·에이전트 이름 등)
  key_shape      개인키 머리줄, 흔한 토큰 모양(GitHub·Anthropic·OpenAI·Slack·AWS·Google·텔레그램 봇)
  assignment     password=·token: 같은 대입에 실제 값처럼 보이는 것이 붙어 있다
  ipv4           루프백·문서용 대역(192.0.2/24·198.51.100/24·203.0.113/24) 밖의 IPv4
  email          example.* 밖의 이메일 주소
  home_path      /Users/<이름>·/home/<이름>
  wiki_link      [[노트 이름]] 꼴 (개인 노트 링크가 딸려 온 것)
  file           DB·로그·키·인증서·압축·백업·.env 류 파일
  image_meta     그림의 EXIF·XMP·텍스트 덩어리. 크기·색공간만 든 EXIF 는 통과, 기기·GPS·시각·소프트웨어는 걸린다
  gitleaks       gitleaks 를 함께 돌려 그 결과 수를 더한다. PATH 에 없으면 실값 대조가 빠진 것과 같다(미검사)

■ 설정 (전부 리포 밖. 경로를 리포에 적지 않는다)
  SECRET_SOURCES   콜론으로 구분한 파일·폴더. 그 안의 값(JSON 값·KEY=VALUE·한 줄 파일·긴 토큰·
                         C/헤더의 문자열 상수 — #define NAME "값", {"ssid","pass"} 배열)을 실값으로 보고 대조한다.
                         출력은 「출처 번호·값 sha256 앞 8자」뿐이다. 기본값은 없다(리포가 공개라 개인 경로를
                         적지 않는다) — 부르는 쪽(ops/hooks/pre-push 가 읽는 리포 밖 precheck.env 등)이 반드시 넘긴다.
                         없거나 읽은 실값이 0개면 결과는 「미검사」다(통과 아님)
  SENSITIVE_TERMS  한 줄에 「라벨<TAB>정규식」. 기본 ~/.config/oakle-gadget/sensitive_terms.txt
  허용 목록               ops/sensitive_allow.txt (리포 안, 공개돼도 되는 것만). 한 줄에
                         「검사<TAB>경로 glob<TAB>정규식」 — 그 경로에서 걸린 글자가 정규식과 맞으면 통과

    python3 ops/sensitive_check.py                 # 리포 루트, git 이 아는 파일(추적 + 안 무시된 새 파일)
    python3 ops/sensitive_check.py <폴더>          # 내보낸 트리 등 아무 폴더 (.git 제외 전부)
    python3 ops/sensitive_check.py --strict ...    # 실값 대조·용어 목록이 없으면 exit 2 (공개·배포 전엔 이것)

  exit 0 = 걸린 것 0 이고 실값 대조도 했다, 1 = 걸림(목록 출력),
       2 = 설정 문제(--strict 에서 출처가 없거나 못 읽거나 읽은 실값 0개, 또는 gitleaks 가 PATH 에 없음),
       3 = 미검사(--strict 없이, 걸린 것은 0 인데 실값 대조나 gitleaks 를 못 했다 — 통과로 읽지 말 것)
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALLOW_FILE = REPO / "ops" / "sensitive_allow.txt"
TERMS_DEFAULT = "~/.config/oakle-gadget/sensitive_terms.txt"
SELF = {"ops/sensitive_check.py", "ops/sensitive_allow.txt"}

TEXT_LIMIT = 5 * 1024 * 1024
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
BAD_EXT = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".log", ".pem", ".key", ".p12", ".pfx", ".crt",
           ".tar", ".gz", ".tgz", ".zip", ".7z", ".bak", ".dump", ".sql", ".ovpn", ".kdbx")
BAD_NAME = re.compile(r"(^|/)(\.env(\..*)?|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|.*\.secret|secret|credentials?(\..*)?)$",
                      re.I)
OK_NAME = re.compile(r"(\.example|\.template|\.sample)$")

KEY_SHAPES = [
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("github_token", r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai_key", r"\bsk-(proj-)?[A-Za-z0-9]{32,}"),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("aws_key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("google_key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("telegram_bot", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    ("notion_key", r"\b(secret_|ntn_)[A-Za-z0-9]{40,}"),
]
ASSIGN = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b[\"']?\s*[:=]\s*"
                    r"[\"']([^\"'\s$<{}()\[\]]{8,})[\"']")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
HOME = re.compile(r"(/Users/|/home/)[A-Za-z0-9._-]+")
WIKI = re.compile(r"\[\[[^\]\[\n\d\"',\s-][^\]\n]*\]\]")
TOKEN = re.compile(r"[A-Za-z0-9_\-+/=.:@]{8,}")
CSTR = re.compile(r'"((?:[^"\\\n]|\\.){8,})"')     # 실값 출처 안의 C 문자열 상수

# EXIF 에서 통과시키는 태그: ExifIFD 포인터, 색공간, 픽셀 가로·세로, 방향, 해상도·단위
BENIGN_EXIF = {0x8769, 0xA001, 0xA002, 0xA003, 0x0112, 0x011A, 0x011B, 0x0128}


def is_doc_ip(o: tuple[int, ...]) -> bool:
    return (o[0] == 127 or o == (0, 0, 0, 0) or o[:3] in ((192, 0, 2), (198, 51, 100), (203, 0, 113)))


def files_of(root: Path) -> list[Path]:
    if (root / ".git").exists():
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "-co", "--exclude-standard"],
                             capture_output=True, check=True).stdout.decode().split("\0")
        return [root / p for p in out if p and (root / p).is_file()]
    return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts]


def load_allow() -> list[tuple[str, str, re.Pattern]]:
    rows = []
    if ALLOW_FILE.exists():
        for ln in ALLOW_FILE.read_text(encoding="utf-8").splitlines():
            if ln.strip() and not ln.startswith("#"):
                check, glob, rx = ln.split("\t", 2)
                rows.append((check, glob, re.compile(rx)))
    return rows


def load_terms(strict: bool, problems: list[str]) -> list[tuple[str, re.Pattern]]:
    path = Path(os.environ.get("SENSITIVE_TERMS", TERMS_DEFAULT)).expanduser()
    if not path.exists():
        problems.append(f"개인 용어 목록 없음({'SENSITIVE_TERMS' if 'SENSITIVE_TERMS' in os.environ else '기본 위치'})"
                        " — term 검사 건너뜀")
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip() and not ln.startswith("#"):
            label, _, rx = ln.partition("\t")
            out.append((label, re.compile(rx or label)))
    return out


def secret_values(problems: list[str]) -> dict[str, str]:
    """값 → 출처 표시(「출처 n」). 짧거나 흔한 값은 거른다."""
    raw = os.environ.get("SECRET_SOURCES", "")
    if not raw:
        problems.append("SECRET_SOURCES 없음 — 실값 대조 건너뜀")
        return {}
    vals: dict[str, str] = {}

    def add(v, src):
        v = str(v).strip()
        # 짧은 값·소문자 낱말·주소 뼈대·경로·날짜/시각은 비밀이 아니고 어디에나 나온다
        if len(v) >= 8 and not re.fullmatch(r"[a-z_ ]+|https?://[^/]+/?|[~/.][\w./-]*|true|false|null|[\d\-:T+.Z ]+", v):
            vals.setdefault(v, src)

    def walk(o, src):
        if isinstance(o, dict):
            for x in o.values():
                walk(x, src)
        elif isinstance(o, list):
            for x in o:
                walk(x, src)
        elif isinstance(o, (str, int)) and not isinstance(o, bool):
            add(o, src)

    n = 0
    for part in raw.split(":"):
        p = Path(part).expanduser()
        if not p.exists():
            problems.append(f"실값 출처를 못 찾음: 출처 {n + 1}")
            continue
        for f in ([p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file())):
            n += 1
            src = f"출처 {n}"
            try:
                b = f.read_bytes()[:1024 * 1024]
                t = b.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            is_json = False
            try:
                walk(json.loads(t), src)
                is_json = True
            except ValueError:
                pass
            lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
            if len(lines) == 1 and not lines[0].startswith(("{", "[")):
                add(lines[0], src)
            for ln in lines:
                if "=" in ln and not ln.startswith("#"):
                    add(ln.split("=", 1)[1].strip().strip("\"'"), src)
            if not is_json:                # #define NAME "값"·{"ssid","pass"} — JSON 은 키도 문자열이라 값만(walk) 본다
                for m in CSTR.finditer(t):
                    add(m.group(1), src)
            for tok in TOKEN.findall(t):
                if len(tok) >= 16 and re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok):
                    add(tok, src)
    if not vals:
        problems.append("실값을 하나도 못 읽음 — 실값 대조 건너뜀 (출처 형식 확인)")
    return vals


def exif_tags(b: bytes) -> set[int]:
    """TIFF 머리에서 IFD0·ExifIFD·GPS 태그 번호를 모은다. 읽다 깨지면 모은 데까지."""
    tags: set[int] = set()
    try:
        if b.startswith(b"Exif\0\0"):
            b = b[6:]
        e = "<" if b[:2] == b"II" else ">"
        seen = set()
        todo = [struct.unpack(e + "I", b[4:8])[0]]
        while todo:
            off = todo.pop()
            if off in seen or off + 2 > len(b):
                continue
            seen.add(off)
            n = struct.unpack(e + "H", b[off:off + 2])[0]
            for i in range(n):
                ent = b[off + 2 + 12 * i: off + 14 + 12 * i]
                tag = struct.unpack(e + "H", ent[:2])[0]
                tags.add(tag)
                if tag in (0x8769, 0x8825):
                    todo.append(struct.unpack(e + "I", ent[8:12])[0])
    except (struct.error, IndexError):
        tags.add(-1)
    return tags


def image_meta(b: bytes, name: str) -> list[str]:
    found = []
    if name.endswith(".png") and b[:8] == b"\x89PNG\r\n\x1a\n":
        i = 8
        while i + 8 <= len(b):
            n = struct.unpack(">I", b[i:i + 4])[0]
            t = b[i + 4:i + 8].decode("latin1")
            d = b[i + 8:i + 8 + n]
            if t in ("tEXt", "iTXt", "zTXt", "tIME"):
                found.append(t)
            elif t == "eXIf":
                extra = exif_tags(d) - BENIGN_EXIF
                if extra:
                    found.append(f"eXIf 태그 {sorted(extra)}")
            i += 12 + n
    elif name.endswith((".jpg", ".jpeg")):
        i = 2
        while i + 4 <= len(b) and b[i] == 0xFF:
            m, n = b[i + 1], struct.unpack(">H", b[i + 2:i + 4])[0]
            d = b[i + 4:i + 2 + n]
            if m == 0xE1 and d.startswith(b"Exif"):
                extra = exif_tags(d) - BENIGN_EXIF
                if extra:
                    found.append(f"EXIF 태그 {sorted(extra)}")
            elif m == 0xE1 or m == 0xFE:
                found.append("XMP" if m == 0xE1 else "COM")
            if m == 0xDA:
                break
            i += 2 + n
    elif name.endswith(".webp") and b[:4] == b"RIFF":
        i = 12
        while i + 8 <= len(b):
            t = b[i:i + 4].decode("latin1")
            n = struct.unpack("<I", b[i + 4:i + 8])[0]
            if t == "EXIF":
                extra = exif_tags(b[i + 8:i + 8 + n]) - BENIGN_EXIF
                if extra:
                    found.append(f"EXIF 태그 {sorted(extra)}")
            elif t == "XMP ":
                found.append("XMP")
            i += 8 + n + (n & 1)
    return found


def run_gitleaks(root: Path) -> tuple[int, str]:
    exe = shutil.which("gitleaks")
    if not exe:
        return 0, "gitleaks 없음 — 미검사"
    with tempfile.TemporaryDirectory() as td:
        rep = Path(td) / "r.json"
        p = subprocess.run([exe, "dir", str(root), "--no-banner", "--redact", "--report-format", "json",
                            "--report-path", str(rep), "--exit-code", "0"], capture_output=True, text=True)
        if p.returncode != 0 or not rep.exists():
            return 1, f"gitleaks 실패 rc={p.returncode}: {p.stderr.strip()[:200]}"
        found = json.loads(rep.read_text() or "[]")
    allow = load_allow()
    rows = []
    for f in found:
        rel = os.path.relpath(f.get("File", ""), root)
        if any(c == "gitleaks" and fnmatch.fnmatch(rel, g) and r.search(f.get("RuleID", "")) for c, g, r in allow):
            continue
        rows.append(f"{rel}:{f.get('StartLine')} gitleaks {f.get('RuleID')}")
    return len(rows), "\n".join(rows) if rows else "gitleaks 0건"


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    args = [a for a in argv if not a.startswith("--")]
    root = Path(args[0]).resolve() if args else REPO
    problems: list[str] = []
    allow = load_allow()
    terms = load_terms(strict, problems)
    secrets = secret_values(problems)
    gl_missing = shutil.which("gitleaks") is None
    if gl_missing:                         # 실값 대조와 같은 규칙: 못 돌린 검사의 0 은 통과가 아니다
        problems.append("gitleaks 가 PATH 에 없음 — gitleaks 검사 건너뜀")
    if strict and problems:
        print("설정 문제(--strict):\n  " + "\n  ".join(problems))
        return 2

    hits: list[str] = []

    def hit(rel: str, line: int, check: str, text: str, show: str | None = None):
        if any(c == check and fnmatch.fnmatch(rel, g) and r.search(text) for c, g, r in allow):
            return
        hits.append(f"{rel}:{line} [{check}] {show if show is not None else text[:120]}")

    files = files_of(root)
    for f in files:
        rel = str(f.relative_to(root))
        low = rel.lower()
        if (low.endswith(BAD_EXT) or BAD_NAME.search(low)) and not OK_NAME.search(low):
            hit(rel, 0, "file", rel)
        b = f.read_bytes()
        if low.endswith(IMAGE_EXT):
            for m in image_meta(b, low):
                hit(rel, 0, "image_meta", m)
            continue
        if len(b) > TEXT_LIMIT or b"\0" in b[:8192]:
            hit(rel, 0, "file", f"바이너리·큰 파일 {len(b):,}B")
            continue
        if rel in SELF:                    # 이 검사기와 허용 목록은 검사 모양을 글자로 담고 있다(gitleaks 는 본다)
            continue
        text = b.decode("utf-8", "replace")
        for no, ln in enumerate(text.splitlines(), 1):
            for v, src in secrets.items():
                if v in ln:
                    hit(rel, no, "secret_value", v, f"{src} 값(sha256 {hashlib.sha256(v.encode()).hexdigest()[:8]})")
            for label, rx in terms:
                for m in rx.finditer(ln):
                    hit(rel, no, "term", m.group(0), f"{label}: {m.group(0)}")
            for label, rx in KEY_SHAPES:
                for m in re.finditer(rx, ln):
                    hit(rel, no, "key_shape", m.group(0), f"{label}: {m.group(0)[:4]}…")
            for m in ASSIGN.finditer(ln):
                hit(rel, no, "assignment", m.group(0), m.group(0)[:60])
            for m in IPV4.finditer(ln):
                o = tuple(int(x) for x in m.groups())
                if max(o) <= 255 and not is_doc_ip(o):
                    hit(rel, no, "ipv4", m.group(0))
            for m in EMAIL.finditer(ln):
                dom = m.group(0).split("@", 1)[1].lower()
                if not re.search(r"(^|\.)example\.(com|net|org)$|\.example$|\.invalid$|\.test$", dom):
                    hit(rel, no, "email", m.group(0))
            for m in HOME.finditer(ln):
                hit(rel, no, "home_path", m.group(0))
            for m in WIKI.finditer(ln):
                hit(rel, no, "wiki_link", m.group(0))

    gl_n, gl_text = run_gitleaks(root)
    print(f"민감정보 점검: {root} · 파일 {len(files)} · 실값 {len(secrets)}개 대조 · 용어 {len(terms)}개")
    for p in problems:
        print(f"  주의: {p}")
    print(f"  {gl_text}" if gl_n == 0 else gl_text)
    if hits:
        print(f"걸림 {len(hits)}건:")
        print("\n".join("  " + h for h in hits))
    total = len(hits) + gl_n
    if total:
        print(f"결과: 걸림 {total}건 — push·배포·공개 금지")
        return 1
    if not secrets or gl_missing:          # 실값 대조·gitleaks 를 안 한 0 은 통과가 아니다
        miss = "·".join(m for m, bad in (("실값 대조", not secrets), ("gitleaks", gl_missing)) if bad)
        print(f"결과: 미검사 (걸린 것 0 이지만 {miss}를 못 했다 — 출처를 넘기거나 gitleaks 를 설치해 다시 돌릴 것, 통과 아님)")
        return 3
    print("결과: 통과 (걸린 것 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
