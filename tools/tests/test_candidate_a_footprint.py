#!/usr/bin/env python3
"""Verify host model identity, reverse duplicates, record packing and staging math."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/bench'))
from candidate_a_footprint import pack_records,read_record,quantify,record_lower_bounds,SEGMENT,CAPACITY


def main():
    dictionaries=[{'KAT':'cat','TKOG':'cat','HE/HROE':'hello'},{'KAT':'dog','TEFT':'ça va'}]
    image,entries,payload=pack_records(dictionaries)
    assert len(entries)==5 and payload==sum(12+len(key)+((len(text)+3)&~3) for _,key,text,_ in entries)
    assert read_record(image,entries[0][3])==(1,entries[0][1],b'cat')
    assert read_record(image,entries[3][3])==(2,entries[3][1],b'dog')
    lower=record_lower_bounds(dictionaries)
    assert lower['current_record_only_bytes']==payload-4*5
    assert lower['optimistic_24bit_unaligned_record_only_bytes']<lower['current_record_only_bytes']
    r=quantify(dictionaries,.5)
    assert r['verified_forward_entries']==5 and r['verified_reverse_keys']==4
    assert r['allocated_bytes']==8*SEGMENT # catalog 2 + records 1 + indexes 2 + GC 3
    assert r['free_after_record_staging_bytes']==CAPACITY-9*SEGMENT
    assert r['free_after_record_and_shadow_indexes_bytes']==CAPACITY-11*SEGMENT
    # Header CRC binds dictionary ID as well as keys/text.
    image[entries[0][3]]^=1
    try:read_record(image,entries[0][3])
    except ValueError:pass
    else:raise AssertionError('Corruption accepted')
    # Large translations cross segment boundaries without overlapping records.
    data={f'S/{"K/"*i}T':'x'*2047 for i in range(1,15)}
    for outline in ['KAT','TKOG','PWA','PHA','SUPB','PEPB','TEPB','RED','TOP','TPAPB','K','T','P','R','S','A','O','E','U','*']:
        data[outline]='y'*2047
    image,entries,_=pack_records([data])
    assert len(image)>3*SEGMENT
    for _,key,text,address in entries:
        assert address%SEGMENT>=16
        assert address%SEGMENT+12+len(key)+((len(text)+3)&~3)<=SEGMENT
        assert read_record(image,address)==(1,key,text)
    print('Candidate A host model: dictionary isolation, reverse duplicates, CRC, segment boundaries and staging accounting passed')


if __name__=='__main__':main()
