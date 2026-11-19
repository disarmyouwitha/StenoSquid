#!/usr/bin/env python3
"""Render the firmware's fixed regions and dynamic personal pool as SVG/PNG.

Run after preparing the three base images. Requires cairosvg for PNG output.
One square is a 4-KiB sector; heavy outlines group the personal pool in 64-KiB segments.
"""
import argparse
import json
from pathlib import Path
import math
import re
from html import escape
import cairosvg

ROOT = Path(__file__).resolve().parents[2]

def constant(text, name):
    return int(re.search(rf'\b{name}\s*(?:=\s*|\s+)(0x[0-9a-fA-F]+|\d+)', text)[1], 0)

def render():
    header = (ROOT / 'src/steno_squid/dictionary_flash.h').read_text(encoding="utf-8")
    store = (ROOT / 'src/steno_squid/user_dictionary_log.h').read_text(encoding="utf-8")
    start, count, segment = [constant(store, x) for x in ('REGION_START', 'SEG_COUNT', 'SEG_BYTES')]
    bases = [
        ('Lapwing base', 0, 'lapwing/lapwing-base.bin', '#2faac5'),
        ('Plover base', constant(header, 'PLOVER_FLASH_OFFSET'), 'plover/plover-main.bin', '#8c78df'),
        ('Lapwing extras', constant(header, 'EXTRAS_FLASH_OFFSET'), 'extras/lapwing-extras.bin', '#efb747'),
    ]
    bases = [(name, offset, (ROOT/'build'/file).stat().st_size, color) for name,offset,file,color in bases]
    ends = [x[1] for x in bases[1:]] + [start]
    for (_, offset, size, _), limit in zip(bases, ends):
        assert offset % segment == 0 and offset + size <= limit
    assert start == constant(header, 'BASE_DICTIONARY_LIMIT') and start + count*segment == 0xff0000
    sector, total, bg, ink, muted = 4096, 16*1024*1024, '#f6f8fc', '#192b40', '#52657b'
    pool, headroom, tail, scratch = '#a2ddca', '#e0e7f0', '#b9c8df', '#e87368'
    width,height = 1430,1090
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', f'<rect width="{width}" height="{height}" fill="{bg}"/>', '<style>text{font-family:DejaVu Sans,sans-serif}</style>']
    def text(x,y,label,size=20,color=ink,bold=False):
        parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{700 if bold else 400}">{escape(str(label))}</text>')
    def rect(x,y,w,h,fill,stroke='none',sw=1):
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
    text(45,57,'STENO SQUID / EXTERNAL QSPI FLASH',36,bold=True)
    text(45,96,'16 MiB W25Q128  ·  4,096 sectors  ·  4 KiB per square',24)
    text(45,131,'Fixed base regions with growth room; one shared segment pool for personal entries.',20,muted)
    x0,y0,pitch=100,208,10
    text(x0,184,'LOW ADDRESS FIRST / each row = 256 KiB',18,bold=True)
    for n in range(total//sector):
        address=n*sector
        color=headroom
        for _,offset,size,c in bases:
            if offset <= address < offset+math.ceil(size/sector)*sector: color=c
        if start <= address < start+count*segment: color=pool
        elif address >= 0xff0000: color=tail
        if address == 0xfff000: color=scratch
        rect(x0+(n%64)*pitch,y0+(n//64)*pitch,pitch,pitch,color,bg,0.65)
    for mib in range(16):text(18,y0+mib*40+14,f'{mib} MiB',16)
    # Four segments per row; borders show erase-block granularity without
    # assigning record/index/meta roles to permanent physical addresses.
    for s in range(count):
        n=(start+s*segment)//sector
        rect(x0+(n%64)*pitch,y0+(n//64)*pitch,16*pitch,pitch,'none','#62a48f',1.2)
    text(x0,881,'Read left to right, then continue on the next row.',17,muted)
    text(800,184,'STORAGE REGIONS',25,bold=True)
    def legend(y,color,title,detail,more):
        rect(802,y-19,21,21,color)
        text(840,y,title,22,bold=True)
        text(840,y+28,detail,17)
        text(840,y+53,more,16,muted)
    for i,((name,offset,size,c),limit) in enumerate(zip(bases,ends)):
        legend(230+96*i,c,name,
               f'0x{offset:06X}  ·  {size/1048576:.2f} MiB image',
               f'{(limit-offset)/1048576:g} MiB region  ·  {(limit-offset-size)/1024:,.1f} KiB growth room')
    legend(518,pool,'Personal dictionary pool',
           f'0x{start:06X}–0xFEFFFF  ·  {count*segment/1048576:.4f} MiB',
           f'{count} × 64 KiB segments  ·  roles allocated dynamically')
    spare=sum((limit-offset)-math.ceil(size/sector)*sector for (_,offset,size,_),limit in zip(bases,ends))
    legend(614,headroom,'Base-dictionary headroom',
           f'{spare/1024:,.0f} KiB in sector-aligned gaps',
           'Reserved for future base-image growth')
    legend(710,tail,'Tail reserve', '0xFF0000–0xFFEFFF  ·  60 KiB', 'Outside the personal storage pool')
    legend(806,scratch,'Diagnostic scratch sector','0xFFF000–0xFFFFFF  ·  4 KiB','Kept separate from dictionaries')
    rect(45,918,1340,134,'#eaf3ef')
    text(70,951,'INSIDE THE PERSONAL POOL',21,bold=True)
    text(70,987,'Records  +  forward index  +  reverse index  +  metadata  +  free segments',21)
    text(70,1022,'Edits append changed records. Cleanup reuses segments. No full-size second snapshot.',18,muted)
    text(45,1074,'Region allocation, not a live occupancy report. Image sizes come from the prepared firmware dictionaries.',16,muted)
    parts.append('</svg>')
    out=ROOT/'assets/qspi-flash-map.svg'
    out.write_text('\n'.join(parts), encoding="utf-8")
    cairosvg.svg2png(url=str(out),write_to=str(out.with_suffix('.png')))
    print(f'Rendered {out.name} and {out.with_suffix(".png").name}: {count} personal segments')

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
    parser.add_argument("--native", type=Path, metavar="MANIFEST")
    args = parser.parse_args()
    if args.native:
        render_native(args.native)
    else:
        render()
