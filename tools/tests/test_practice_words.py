#!/usr/bin/env python3
"""Validate every built-in prompt and outline against the pinned Lapwing JSON."""
import hashlib
import json
from pathlib import Path
import re
import sys
ROOT = Path(__file__).resolve().parents[2]
source = Path(sys.argv[1]) if len(sys.argv)>1 else ROOT / 'build/lapwing/lapwing-base.json'
entries = json.loads(source.read_text(encoding="utf-8"))
header = (ROOT / 'src/steno_squid/practice_words.h').read_text(encoding="utf-8")
rows = re.findall(r'\{"([A-Z]+)", "([#A-Z*\-]+)"\}', header)
assert len(rows)==300 and len({word for word,_ in rows})==300
for word,outline in rows:
    assert len(word)<16 and len(outline)<24
    assert '/' not in outline and entries[outline].upper()==word,(word,outline)
fingerprint = int.from_bytes(hashlib.sha256(json.dumps(rows).encode()).digest()[:4],'little')
assert fingerprint==int(re.search(r'STENO_SQUID_WORD_SET_ID 0x([0-9a-f]+)',header)[1],16)
print('Practice word pool: 300 unique words/outlines and persistent-ID fingerprint verified')

multi_header=(ROOT / 'src/steno_squid/practice_multi.h').read_text(encoding="utf-8")
multi=re.findall(r'\{"([A-Z]+)", "([#A-Z*/\-]+)"\}',multi_header)
assert len(multi)==20 and len(set(w for w,_ in multi))==20
for word,outline in multi:
    assert outline.count('/')==1 and entries[outline].upper()==word,(word,outline)
    assert len(word)<16 and len(outline)<24
print('Multi drill: 20 common two-stroke outlines verified against Lapwing')

build_header=(ROOT / 'src/steno_squid/practice_build.h').read_text(encoding="utf-8")
ladders=re.findall(r'\{"([A-Z]+)",\s*\{"([A-Z-]+)",\s*"([A-Z-]+)",\s*"([A-Z-]+)"\}\}',build_header)
assert len(ladders)==10
for word,*strokes in ladders:
    assert entries[strokes[-1]].upper()==word,(word,strokes)
print('Build-Up: 10 three-step ladders end in verified Lapwing words')
