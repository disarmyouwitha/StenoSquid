#!/usr/bin/env python3
"""Check the 1-bit (LVGL A1) squid art against the 8-bit alpha it replaced.

The hashes below are SHA-256 of each image's old 8-bit alpha bytes (width*height,
255 or 0), taken from pet_sprites.h / pet_effects.h before the A1 conversion.
Expanding the packed bits of the current headers must reproduce them exactly.
"""
import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
DISPLAY = ROOT / 'src/steno_squid/display'

A8_SHA256 = {
    "idle": "ffa578203ccfe0e8b267b5e8f7f07a1da813af793aefe81feb0b8b7a8348c747",
    "blink": "5ada37f5fe8f3e4b5c730d08a86ebe06306ccb3b59a5be7b13b282ffd4f798b4",
    "typing": "74598e999757818d7a943606b69d26b42768c277f53a56c1b2e10b96e092469c",
    "jump": "50318aa8ffe3f8a05e9d3f82e9f6dde369d66019a02ec0e2c0f774c0cc375907",
    "spark": "4740ca1c88e9a4d7153f689dc684463364a73950b72bb6a1e4c97802f5fadd2c",
    "land": "f6ff366f0ce47055bde9a6ef9f2a8e92a84e3db8db00c4d6a5a02372ba6f75b9",
    "sleep": "5ada37f5fe8f3e4b5c730d08a86ebe06306ccb3b59a5be7b13b282ffd4f798b4",
    "rising_0": "1f4af7d749c035538d57015f42fd5316f8882f13878d5920d23ca7e65fca98c3",
    "rising_1": "a038da95c1f3cf575c62dd6b506ab48a7e431999c65aa6d385551881112219d7",
    "rising_2": "ebd5643581f31cb5b81c8fb6241f4ca7e7da63bfa92f3c2c11fdb5a534afbd7c",
    "rising_3": "b27e9a08abb8404e31ee228686118ee028b984fa51d19c3df76d4b1e26bed100",
    "rising_4": "6b045cea407a747322d2ed0fc9299600f7c3ac4d651b3a53eec2958bddcf4f5e",
    "rising_5": "a26aab382979064c526ead643c08c150bb77e652cb4f66c6d0c7967d694cc6e4",
    "rising_6": "4d191bb9a51f0cdc54315c8736e441016d2944a4c9d08c45f062be7a66f04b6a",
    "rising_7": "52dbd4365b026555e3382c056240376d3aa319c7e46c1aa7c38caa4883570517",
    "puff_0": "850c894cef861393486bd0064c7aabaa088fc6cc19f5bd0e906ef672b13a1fb7",
    "puff_1": "738c940a9d3f308a8e23045afb731b3cd36215b1a64c76cba9562bdd1043d8ae",
    "puff_2": "059027abe78fc964286a42d57ddf2f8032306db663fc8b39be9cae1d7b37360a",
    "confetti_0": "b98756bc22947e35d21a9ba9fc8f2e8aaf32eceb8206d6c86f0d3ab6db4acd44",
    "confetti_1": "9401866c481003b674092ad3d0a32a00046dad3603cb86879f44553e68201d35",
    "confetti_2": "1486387d50c281c3063e2b8ad7747d91c4db69183c09789081023095cca5c30d",
    "confetti_3": "659609b310c235d628dc516bb8b3a5b87e8c0ae035ed59ab058d5c3dd121452e",
    "sleep_bubble": "638ae4f0c494ca8e14786d2969c5bf3777236b0d5f31314ec9a07d6dd4d78a2c",
    "greeting_0": "06d515989420af2cfd6b398631f2af5e82c246186d509b31b8ecee4f69759ad6",
    "greeting_1": "67d53ee18dfe0672311f7a6de3de4b642f855eebd461ac80587af6c2dd13d69d",
    "greeting_2": "378064b00254410137ef5f4c28aa21bc6179fd08752b2d3d4956832b6b66aec0",
    "puff_pop": "f49b0561d2c840fc5d7275f59e6bb43906b1df7f342e27cc17c1cb62053fbb53",
    "layer_flip_0": "e892bdb02d9ada8456c2c0bb317738706181be38f112808d5a39842b81818c40",
    "layer_flip_1": "19bab8d776fce88eec2f2211757d709543177a3a9e960af3f53e0ad439a39294",
    "layer_flip_2": "481dc5c80a1b77f72e12568c30ea1fd375374064dbd4095a6e5fa114b8805719",
}


def strip_comments(text):
    return re.sub(r'/\*.*?\*/', '', text, flags=re.S)


def images(path, default_size):
    text = path.read_text().replace('\r', '')
    pattern = (r'static const uint8_t (\w+)_pixels\[\] = \{(.*?)\n\};\n'
               r'(?:PET_IMAGE|FX_IMAGE|FX_SIZED_IMAGE)\(\w+(?:, *(\d+), *(\d+))?\);')
    for m in re.finditer(pattern, text, re.S):
        data = [int(v, 16) for v in re.findall(r'0x[0-9a-fA-F]{2}', strip_comments(m.group(2)))]
        size = (int(m.group(3)), int(m.group(4))) if m.group(3) else default_size
        yield m.group(1), size, data


def expand(width, height, data):
    stride = (width + 7) // 8
    assert len(data) == stride * height, f'size {len(data)} != {stride * height}'
    alpha = bytearray()
    for y in range(height):
        row = data[y * stride:(y + 1) * stride]
        for x in range(width):
            alpha.append(255 if row[x // 8] & (0x80 >> (x % 8)) else 0)
        for x in range(width, stride * 8):
            assert not row[x // 8] & (0x80 >> (x % 8)), 'padding bit set'
    return bytes(alpha)


def main():
    seen = {}
    for header, default in (('pet_sprites.h', (48, 48)), ('pet_effects.h', (48, 30))):
        for name, (w, h), data in images(DISPLAY / header, default):
            seen[name] = hashlib.sha256(expand(w, h, data)).hexdigest()
    missing = set(A8_SHA256) - set(seen)
    extra = set(seen) - set(A8_SHA256)
    bad = [n for n in A8_SHA256 if n in seen and seen[n] != A8_SHA256[n]]
    if missing or extra or bad:
        print('missing:', sorted(missing), 'extra:', sorted(extra), 'mismatch:', bad)
        return 1
    print(f'{len(seen)} images match their 8-bit originals')
    return 0


if __name__ == '__main__':
    sys.exit(main())
