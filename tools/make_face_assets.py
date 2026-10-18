#!/usr/bin/env python3
"""표정 그림 20장 → StickS3 용 표정 데이터 firmware/oakle_gadget/faces/faces_data.h.

원본은 360×360 투명 PNG(번호·이름 01_joy … 20_mindblown, faces/README.md).
120×120 으로 줄이고(premultiplied), 화면 배경(검정)에 알파를 합성해 RGB565 배열로 낸다.
값은 보통 순서 RGB565 — 펌웨어가 setSwapBytes(true) 로 그린다(안 켜면 깨진다, faces/README.md).
그림은 이 리포에 넣지 않는다 — faces/ 는 .gitignore 대상이고, 없으면 펌웨어가 도형 얼굴을 그린다.

    python3 tools/make_face_assets.py <원본 폴더> [--size 120] [--compare]

--compare 는 같은 그림을 RGB 8bit PNG(drawPng 용)로도 만들어 크기만 비교해 찍는다(파일은 안 남김).
필요한 것: Pillow.
"""
import argparse
import glob
import io
import os
import re
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "firmware", "oakle_gadget", "faces")
NAME_RE = re.compile(r"^(\d\d)_([a-z]+)\.png$")


def load(path, size, bg=(0, 0, 0)):
    im = Image.open(path).convert("RGBA").convert("RGBa").resize((size, size), Image.LANCZOS).convert("RGBA")
    base = Image.new("RGBA", im.size, bg + (255,))
    return Image.alpha_composite(base, im).convert("RGB")


def rgb565(im):
    px = im.tobytes()
    return [((px[i] & 0xF8) << 8) | ((px[i + 1] & 0xFC) << 3) | (px[i + 2] >> 3) for i in range(0, len(px), 3)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="01_joy.png … 20_mindblown.png 가 있는 폴더")
    ap.add_argument("--size", type=int, default=120)
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args()

    files = sorted(f for f in glob.glob(os.path.join(a.src, "*.png")) if NAME_RE.match(os.path.basename(f)))
    if len(files) != 20:
        sys.exit(f"원본이 20장이 아님: {len(files)}장")
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "faces_data.h")
    s = a.size
    png_total = 0
    names = []
    with open(out, "w") as fh:
        fh.write("// make_face_assets.py 가 만든 파일. 고치지 말고 다시 만들 것. 리포에 올리지 않는다.\n")
        fh.write(f"#pragma once\n#include <stdint.h>\n#define FACE_W {s}\n#define FACE_H {s}\n\n")
        for f in files:
            num, name = NAME_RE.match(os.path.basename(f)).groups()
            im = load(f, s)
            data = rgb565(im)
            fh.write(f"static const uint16_t face_{num}_{name}[{len(data)}] = {{\n")
            for i in range(0, len(data), 16):
                fh.write("  " + ",".join(f"0x{v:04X}" for v in data[i:i + 16]) + ",\n")
            fh.write("};\n")
            names.append((num, name))
            if a.compare:
                q = im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
                b = io.BytesIO(); q.save(b, "PNG", optimize=True)
                png_total += len(b.getvalue())
        fh.write("\nstruct FaceImg { const char* name; const uint16_t* px; };\n")
        fh.write("static const FaceImg FACE_TABLE[] = {\n")
        for num, name in names:
            fh.write(f'  {{"{name}", face_{num}_{name}}},\n')
        fh.write("};\n#define FACE_COUNT " + str(len(names)) + "\n")
    rgb_bytes = len(names) * s * s * 2
    print(f"{len(names)}장 {s}×{s} RGB565: 플래시 {rgb_bytes} B ({rgb_bytes / 1024:.0f} KB), 헤더 파일 {os.path.getsize(out)} B → {os.path.relpath(out)}")
    if a.compare:
        print(f"비교용 PNG(256색) 합계 {png_total} B ({png_total / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
