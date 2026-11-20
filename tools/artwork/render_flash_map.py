#!/usr/bin/env python3
"""Render a fresh Native dictionary pool as PNG.

Run after building the Native image manifest. Requires cairosvg.
"""
import argparse
import json
from pathlib import Path
from html import escape
import cairosvg

ROOT = Path(__file__).resolve().parents[2]

def render_native(manifest):
    report = json.loads(manifest.read_text())
    segment = 65536
    pool_end = report["bytes"]
    lapwing, plover = report["base_images"]
    assert pool_end == 255 * segment and lapwing["address"] == 3 * segment
    assert lapwing["address"] + lapwing["segments"] * segment == plover["address"]
    pool_start = plover["address"] + plover["segments"] * segment
    assert report["free_segments"] == (pool_end - pool_start) // segment
    bg, ink, muted = "#f6f8fc", "#192b40", "#52657b"
    rows = [
        (0, segment, "#273d64", "Catalog A", "First copy of the dictionary catalog"),
        (segment, 2 * segment, "#506899", "Catalog B", "Second copy for recovery"),
        (2 * segment, 3 * segment, "#eeb35c", "First edit journal", "Starts empty on a fresh install"),
        (lapwing["address"], plover["address"], "#39b5ca", "Lapwing collection",
         f'{lapwing["bytes"] / 1048576:.2f} MiB of data · five independent checkboxes'),
        (plover["address"], pool_start, "#9c86e6", "Plover Base",
         f'{plover["bytes"] / 1048576:.2f} MiB of data · separate checkbox'),
        (pool_start, pool_end, "#9cd8c2", "Shared pool",
         f'{report["free_segments"]} free segments at install · journals and indexes grow here'),
        (pool_end, 256 * segment, "#cbd3e3", "Reserved tail", "Outside the installer’s erase range"),
    ]
    width, height = 1240, 720
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">']
    def rect(x, y, w, h, color, radius=0):
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{color}"/>')
    def label(x, y, value, size=20, color=ink, bold=False):
        parts.append(f'<text x="{x}" y="{y}" fill="{color}" font-family="DejaVu Sans,sans-serif" '
                     f'font-size="{size}" font-weight="{700 if bold else 400}">{escape(value)}</text>')
    rect(0, 0, width, height, bg)
    label(48, 57, "External memory map", 38, bold=True)
    label(49, 95, "StenoSquid Native · 16 MiB QSPI · 64 KiB per allocation segment", 22, muted)
    for lo, hi, color, _, _ in rows:
        rect(48 + lo * 1144 / (256 * segment), 136, (hi - lo) * 1144 / (256 * segment), 44, color)
    label(48, 205, "0x000000", 16, muted)
    label(1110, 205, "16 MiB", 16, muted)
    label(71, 256, "REGION", 15, muted, True)
    label(377, 256, "ADDRESS RANGE", 15, muted, True)
    label(672, 256, "FRESH INSTALL", 15, muted, True)
    for i, (lo, hi, color, name, detail) in enumerate(rows):
        y = 272 + i * 47
        rect(48, y, 1144, 42, "#ffffff", 6)
        rect(64, y + 12, 18, 18, color, 3)
        label(97, y + 27, name, 19, bold=True)
        label(377, y + 27, f"0x{lo:06X}–0x{hi - 1:06X}", 19)
        label(672, y + 27, detail, 17)
    label(48, 639, "Lapwing Base · Commands · Numbers · UK Additions · Proper Nouns", 20, bold=True)
    label(48, 674, "The allocator keeps three free segments for maintenance. Pool roles change as dictionaries grow.", 17, muted)
    parts.append("</svg>")
    out = ROOT / "assets/qspi-flash-map-native.png"
    cairosvg.svg2png(bytestring="\n".join(parts).encode(), write_to=str(out))
    print(f"Rendered {out.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, metavar="MANIFEST", required=True)
    args = parser.parse_args()
    render_native(args.native)
