#!/usr/bin/env python3
"""Phase-0 footprint measurements and explicitly labeled allocator estimates."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools/dictionaries'), str(ROOT / 'tools/dict_compiler')]
from manifest import BASES, XIP_BASE, LAPWING_COMMIT, PLOVER_COMMIT
from prepare import fetch
from compile_dict import DictionaryCollectionBuilder, parse_outline
from verify_mapped_dict import verify
from segment_image import Image

SEGMENT = 65536
DATA_BYTES = 255 * SEGMENT


def packed_segments(sizes):
    if not sizes:
        return 0
    segments, used = 1, 16
    for size in sizes:
        if size > SEGMENT - 16:
            raise ValueError('A record exceeds one segment')
        if used + size > SEGMENT:
            segments += 1
            used = 16
        used += size
    return segments


def estimate_log(dictionaries, dictionary_id_bytes=4, load=0.5):
    """A hypothetical shared two-index log, not a serialized production format."""
    sizes = []
    for entries in dictionaries:
        for outline, text in entries.items():
            sizes.append(8 + dictionary_id_bytes + 4 * len(parse_outline(outline)) +
                         ((len(text.encode('utf-8')) + 3) & ~3))
    count = len(sizes)
    records = packed_segments(sizes)
    # No current 32-table-segment cap: this measures a proposed format explicitly.
    per_table = max(1, math.ceil(count / load / ((SEGMENT - 16) // 4)))
    index_bytes = 2 * per_table * SEGMENT
    catalog = SEGMENT * 2  # Explicit assumption: two committed catalog generations.
    reserve = SEGMENT * 3
    allocated = records * SEGMENT + index_bytes + catalog + reserve
    return dict(entries=count, packed_record_bytes=sum(sizes), record_segments=records,
                index_allocated_bytes=index_bytes, catalog_allocated_bytes=catalog,
                gc_reserve_bytes=reserve, allocated_bytes=allocated,
                free_bytes=DATA_BYTES-allocated, fits_16_mib=allocated<=DATA_BYTES,
                dictionary_id_bytes=dictionary_id_bytes, index_target_load=load)


def measure(output):
    output.mkdir(parents=True, exist_ok=True)
    rows, sources, dictionaries = [], [], []
    named = []
    for base in BASES.values():
        for url, filename, name, sha in base['sources']:
            path = output / 'sources' / filename
            path.parent.mkdir(exist_ok=True)
            fetch(url, path, sha)
            entries = json.loads(path.read_text(encoding='utf-8'))
            canonical = {}
            for outline, text in entries.items():
                key = parse_outline(outline)
                if not 1 <= len(key) <= 16 or len(text.encode('utf-8')) > 2047 or '\0' in text:
                    raise ValueError(f'Unsupported entry: {filename}: {outline}')
                if key in canonical and canonical[key] != text:
                    raise ValueError(f'Conflicting canonical outline: {filename}: {outline}')
                canonical[key] = text
            blob = DictionaryCollectionBuilder(base_addr=XIP_BASE).build([(name, entries)])
            verify(entries, blob, XIP_BASE)
            image_path = output / (filename.removesuffix('.json') + '.bin')
            image_path.write_bytes(blob)
            baseline = Image({key: text.encode('utf-8') for key, text in canonical.items()})
            record_bytes = sum(8 + 4*len(key) + ((len(text.encode('utf-8'))+3)&~3)
                               for key, text in canonical.items())
            row = dict(compiled_base_address=hex(XIP_BASE),name=name, entries=len(canonical), unique_texts=len(set(canonical.values())),
                       compiled_bytes=len(blob), compiled_crc32=f'{zlib.crc32(blob):08x}',
                       current_log_record_bytes=record_bytes,
                       current_log_image_segments=baseline.total_segments,
                       current_log_image_bytes=baseline.total_segments*SEGMENT,
                       contiguous_extent_bytes=math.ceil(len(blob)/SEGMENT)*SEGMENT,
                       source_sha256=sha, source=str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path))
            rows.append(row); sources.append(dict(url=url,sha256=sha,file=filename)); dictionaries.append(entries)
            named.append((name, entries))
    combined = DictionaryCollectionBuilder(base_addr=XIP_BASE).build(named)
    for index, (_, entries) in enumerate(named):
        verify(entries, combined, XIP_BASE, index, len(named))
    grouped_extras = DictionaryCollectionBuilder(base_addr=XIP_BASE).build(named[2:])
    for index, (_, entries) in enumerate(named[2:]):
        verify(entries, grouped_extras, XIP_BASE, index, 4)
    ownership = {}
    for index, entries in enumerate(dictionaries):
        for outline in entries:
            ownership.setdefault(parse_outline(outline), set()).add(index)
    compiled = sum(row['compiled_bytes'] for row in rows)
    current_grouped = rows[0]['compiled_bytes'] + rows[1]['compiled_bytes'] + len(grouped_extras)
    estimates = {str(load):estimate_log(dictionaries,load=load) for load in (0.5,0.7,0.875)}
    # Deterministic synthetic distinct keys; retain fixed text size so capacity is interpretable.
    growth=[]
    for count in (50000,100000,190000,200000):
        record_segments=packed_segments([32]*count)
        tables=2*max(1,math.ceil(2*count/16380))
        growth.append(dict(entries=count,record_bytes_each=32,
                           allocated_bytes=(record_segments+tables+2+3)*SEGMENT,
                           note='Analytical uniform 32-byte records; not an imported image or churn measurement'))
    report=dict(schema=1,firmware_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                python=platform.python_version(),compiler_source_sha256=hashlib.sha256((ROOT/'tools/dict_compiler/compile_dict.py').read_bytes()).hexdigest(),javelin_manifest=(ROOT/'config/west.yml').read_text(),
                pins=dict(lapwing=LAPWING_COMMIT,plover=PLOVER_COMMIT),sources=sources,measurements=rows,
                totals=dict(entries=sum(r['entries'] for r in rows),cross_dictionary_duplicate_outlines=sum(len(ids)>1 for ids in ownership.values()),
                            all_dictionary_compiled_bytes=len(combined),separate_compiled_bytes=compiled,
                            current_grouped_compiled_bytes=current_grouped,
                            extras_grouped_compiled_bytes=len(grouped_extras),
                            current_packed_record_bytes=sum(r['current_log_record_bytes'] for r in rows)),
                candidate_a_estimates=estimates,
                candidate_b_estimate=dict(extent_bytes=sum(r['contiguous_extent_bytes'] for r in rows),
                    catalog_bytes=2*SEGMENT,gc_reserve_bytes=3*SEGMENT,
                    update_staging_bytes=max(r['contiguous_extent_bytes'] for r in rows),
                    empty_overlay_bytes=4*SEGMENT,
                    note='Analytical fresh allocator; fragmentation and overlay churn NOT measured'),
                synthetic_growth_estimates=growth,
                pending=['10k-edit allocator/GC churn', 'multi-dictionary log prototype',
                         'on-device candidate comparisons','hardware current measurements'],
                assumptions=['Candidate A adds 4 dictionary-ID bytes per record and two shared 4-byte indexes',
                             'Catalog: two 64 KiB generations; GC reserve: three segments',
                             'All measured images use 0x12000000 for footprint parity, not their production offsets; never install these measurement files',
                             'All-collection compact image is footprint-only, not an installed replacement'])
    (output/'footprint.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    with (output/'footprint.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'build/dictionary-phase0/footprint')
    args=parser.parse_args(); report=measure(args.output)
    print(json.dumps(report['totals'],indent=2))
    print('Measured images verified against every source entry; allocator figures remain estimates.')


if __name__=='__main__':main()
