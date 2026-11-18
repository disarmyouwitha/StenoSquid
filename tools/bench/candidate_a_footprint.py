#!/usr/bin/env python3
"""Serialize/verify a host-only multi-dictionary log model; evaluate A's space gate.

This is a footprint experiment, not a firmware format or uploadable image.
Records extend today's 8-byte header with a four-byte dictionary ID. Both
indexes contain four-byte image-relative record offsets; production slot tag
encoding, power-failure transactions and GC are deliberately not implemented.
"""
import argparse
from array import array
import hashlib
import json
import math
import subprocess
from pathlib import Path
import struct
import sys
import zlib

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'tools/dictionaries'),str(ROOT/'tools/dict_compiler')]
from manifest import BASES
from compile_dict import parse_outline
from segment_image import hash_bytes

SEGMENT=65536
HEADER=16
SLOTS=(SEGMENT-HEADER)//4
CAPACITY=255*SEGMENT
EMPTY=0xffffffff


def pack_records(dictionaries):
    """One physical byte image: dictionary identity survives overlapping outlines."""
    data=bytearray(b'\xff'*(2*SEGMENT)) # Two assumed catalog generations.
    expected=[]
    used=HEADER
    data.extend(b'\xff'*SEGMENT)
    payload=0
    for dictionary_id,entries in enumerate(dictionaries,1):
        seen={}
        for outline,text in entries.items():
            strokes=parse_outline(outline)
            encoded=text.encode('utf-8')
            if not 1<=len(strokes)<=16 or len(encoded)>2047 or b'\0' in encoded:
                raise ValueError(f'Unsupported entry {dictionary_id}:{outline}')
            if strokes in seen:
                if seen[strokes]!=encoded:raise ValueError('Conflicting canonical outline')
                continue
            seen[strokes]=encoded
            key=struct.pack('<'+'I'*len(strokes),*strokes)
            size=12+len(key)+((len(encoded)+3)&~3)
            if used+size>SEGMENT:
                data.extend(b'\xff'*SEGMENT);used=HEADER
            address=len(data)-SEGMENT+used
            crc=zlib.crc32(struct.pack('<I',dictionary_id)+key+encoded)
            struct.pack_into('<IHHI',data,address,dictionary_id,len(strokes),len(encoded),crc)
            data[address+12:address+12+len(key)]=key
            data[address+12+len(key):address+12+len(key)+len(encoded)]=encoded
            expected.append((dictionary_id,key,encoded,address))
            used+=size;payload+=size
    return data,expected,payload


def read_record(image,address):
    dictionary_id,n,text_size,crc=struct.unpack_from('<IHHI',image,address)
    key=bytes(image[address+12:address+12+4*n])
    text=bytes(image[address+12+4*n:address+12+4*n+text_size])
    if zlib.crc32(struct.pack('<I',dictionary_id)+key+text)!=crc:
        raise ValueError('Record checksum mismatch')
    return dictionary_id,key,text


def indexes(image,entries,load):
    segments=max(1,math.ceil(len(entries)/load/SLOTS))
    count=segments*SLOTS
    tables=[array('I',[EMPTY])*count for _ in range(2)]
    probes=[[],[]]
    for dictionary_id,key,text,address in entries:
        prefix=struct.pack('<I',dictionary_id)
        for table,raw,timing in zip(tables,(key,text),probes):
            slot=hash_bytes(prefix+raw)%count
            distance=1
            while table[slot]!=EMPTY:
                slot=(slot+1)%count;distance+=1
                if distance>count:raise ValueError('Index full')
            table[slot]=address;timing.append(distance)
    # Validate every forward and every unique dictionary-qualified reverse key.
    reverse={}
    for dictionary_id,key,text,address in entries:
        slot=hash_bytes(struct.pack('<I',dictionary_id)+key)%count
        for _ in range(count):
            found=tables[0][slot]
            if found==EMPTY:raise AssertionError('Missing forward entry')
            actual=read_record(image,found)
            if actual[:2]==(dictionary_id,key):
                assert actual[2]==text and found==address
                break
            slot=(slot+1)%count
        else:raise AssertionError('Unterminated forward probe')
        reverse.setdefault((dictionary_id,text),set()).add(address)
    for (dictionary_id,text),wanted in reverse.items():
        slot=hash_bytes(struct.pack('<I',dictionary_id)+text)%count
        found=set()
        for _ in range(count):
            address=tables[1][slot]
            if address==EMPTY:break
            actual=read_record(image,address)
            if (actual[0],actual[2])==(dictionary_id,text):found.add(address)
            slot=(slot+1)%count
        else:raise AssertionError('Unterminated reverse probe')
        assert found==wanted
    # Materialize the same four-byte offsets in 64 KiB index segments with headers.
    for table in tables:
        for start in range(0,count,SLOTS):
            header=struct.pack('<4sIII',b'HIDX',len(image)//SEGMENT,start//SLOTS,count)
            image.extend(header)
            encoded=struct.pack('<'+'I'*SLOTS,*table[start:start+SLOTS])
            image.extend(encoded)
            assert struct.unpack_from('<'+'I'*SLOTS,image,len(image)-len(encoded))==tuple(table[start:start+SLOTS])
    return segments,probes,len(reverse)


def quantify(dictionaries,load,write_image=None):
    image,entries,payload=pack_records(dictionaries)
    record_segments=len(image)//SEGMENT-2
    per_table,probes,reverse_count=indexes(image,entries,load)
    image.extend(b'\xff'*(3*SEGMENT))
    # One entire dictionary's newly encoded records alongside its old version.
    # Existing indexes are assumed updated in place: a deliberately optimistic
    # lower-bound staging model, not an atomic update implementation.
    staging=max((len(pack_records([d])[0])-2*SEGMENT for d in dictionaries),default=0)
    if write_image:
        write_image.parent.mkdir(parents=True,exist_ok=True)
        write_image.write_bytes(image)
    def stats(values):
        ordered=sorted(values)
        return {'mean':sum(values)/len(values) if values else 0,
                'p99':ordered[max(0,math.ceil(len(ordered)*.99)-1)] if ordered else 0,
                'maximum':max(values,default=0)}
    return dict(entries=len(entries),record_payload_bytes=payload,record_segments=record_segments,
                record_allocated_bytes=record_segments*SEGMENT,slots_per_table=per_table*SLOTS,
                actual_load=len(entries)/(per_table*SLOTS),index_segments_per_table=per_table,
                index_bytes=2*per_table*SEGMENT,catalog_bytes=2*SEGMENT,gc_reserve_bytes=3*SEGMENT,
                allocated_bytes=len(image),free_bytes=CAPACITY-len(image),
                largest_dictionary_record_staging_bytes=staging,
                free_after_record_staging_bytes=CAPACITY-len(image)-staging,
                full_shadow_index_staging_bytes=2*per_table*SEGMENT,
                free_after_record_and_shadow_indexes_bytes=CAPACITY-len(image)-staging-2*per_table*SEGMENT,
                forward_insertion_probes=stats(probes[0]),reverse_insertion_probes=stats(probes[1]),
                verified_forward_entries=len(entries),verified_reverse_keys=reverse_count,
                image_sha256=hashlib.sha256(image).hexdigest())



def record_lower_bounds(dictionaries):
    count=strokes=text=aligned_text=0
    for entries in dictionaries:
        for outline,value in entries.items():
            count+=1;strokes+=len(parse_outline(outline))
            size=len(value.encode('utf-8'));text+=size;aligned_text+=(size+3)&~3
    return dict(entries=count,stroke_words=strokes,text_bytes=text,aligned_text_bytes=aligned_text,
                current_record_only_bytes=8*count+4*strokes+aligned_text,
                optimistic_24bit_unaligned_record_only_bytes=8*count+3*strokes+text)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources',type=Path,default=ROOT/'build/dictionary-phase0/footprint/sources')
    parser.add_argument('--output',type=Path,default=ROOT/'build/dictionary-phase0/candidate-a')
    parser.add_argument('--compact-report',type=Path,default=ROOT/'build/dictionary-phase0/footprint/footprint.json')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    inputs=[];sources=[]
    for base in BASES.values():
        for url,name,label,sha in base['sources']:
            path=args.sources/name;raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError(f'Wrong pinned input: {name}')
            inputs.append(json.loads(raw.decode('utf-8')))
            sources.append(dict(file=name,name=label,sha256=sha,url=url))
    cases={}
    for load in (.5,.7,.875):
        print(f'Building and verifying {load:.1%} index target',flush=True)
        cases[str(load)]=quantify(inputs,load,args.output/f'host-model-{load}.bin')
    # Favor A: today's records without dictionary IDs, any indexes, segment
    # headers/slack, catalog, reserve or staging. This is not a viable image.
    bounds=record_lower_bounds(inputs)
    lower=bounds['current_record_only_bytes']
    compact=json.loads(args.compact_report.read_text(encoding='utf-8'))
    assert [s['sha256'] for s in compact['sources']]==[s['sha256'] for s in sources], 'Compact corpus differs'
    compiled=compact['totals']['current_grouped_compiled_bytes']
    report=dict(schema=1,tool_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                lower_bounds=bounds,model='Host-only current-log-derived records + dictionary ID + shared indexes',
                firmware_format=False,data_capacity_bytes=CAPACITY,sources=sources,cases=cases,
                optimistic_record_only_lower_bound_bytes=lower,current_compiled_bytes=compiled,
                optimistic_lower_bound_overhead_bytes=lower-compiled,
                reject_overhead_threshold_bytes=2*1024*1024,
                gate='FAIL' if lower-compiled>2*1024*1024 else 'INCONCLUSIVE',
                scope='Reject current-log-derived Candidate A for this pinned 16 MiB corpus; other compressed/deduplicated editable formats are unmeasured',
                limitations=['No GC/power-failure implementation, device latency or RAM claim',
                    'Full-dictionary record staging assumes whole-version replacement; incremental update alternatives may use less',
                    '24-bit unaligned bound is hypothetical and excludes indexes, IDs and metadata; not an nRF-safe serialized format',
                    'Reverse index retains every record; hashes are dictionary-qualified',
                    'Four-byte relative offsets are a host model, not an approved flash slot encoding',
                    'Staging includes no extra user entries, overlay growth or fragmentation',
                    'Record-only lower bound applies to the current 8-byte header/4-byte-stroke/per-record-text structure, not every possible log format'])
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('sources','cases')},indent=2))


if __name__=='__main__':main()
