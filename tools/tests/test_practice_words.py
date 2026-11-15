#!/usr/bin/env python3
"""Validate every built-in prompt and outline against the pinned Lapwing JSON."""
import hashlib
import json
from pathlib import Path
import re
import sys
ROOT = Path(__file__).resolve().parents[2]
source = Path(sys.argv[1]) if len(sys.argv)>1 else ROOT / 'build/lapwing/lapwing-base.json'
entries = json.loads(source.read_text())
header = (ROOT / 'src/steno_squid/practice_words.h').read_text()
rows = re.findall(r'\{"([A-Z]+)", "([#A-Z*\-]+)"\}', header)
assert len(rows)==300 and len({word for word,_ in rows})==300
for word,outline in rows:
    assert len(word)<16 and len(outline)<24
    assert '/' not in outline and entries[outline].upper()==word,(word,outline)
fingerprint = int.from_bytes(hashlib.sha256(json.dumps(rows).encode()).digest()[:4],'little')
assert fingerprint==int(re.search(r'STENO_SQUID_WORD_SET_ID 0x([0-9a-f]+)',header)[1],16)
print('Practice word pool: 300 unique words/outlines and persistent-ID fingerprint verified')
