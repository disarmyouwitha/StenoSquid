#!/usr/bin/env python3
"""Run read-only Phase-0 device baselines; save raw cycles plus JSON/CSV summaries."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'tools/dictionaries'),str(ROOT/'tools/dict_compiler')]
from import_personal import open_console
from compile_dict import parse_outline


def percentile(values, fraction):
    ordered=sorted(values)
    return ordered[max(0,math.ceil(len(ordered)*fraction)-1)] if ordered else None


def summarize(cycles,hz):
    values=[n*1_000_000/hz for n in cycles]
    return dict(n=len(values),median_us=percentile(values,0.5),p95_us=percentile(values,0.95),
                p99_us=percentile(values,0.99),max_us=max(values) if values else None)


def fnv(text):
    value=2166136261
    for byte in text.encode('utf-8'): value=((value^byte)*16777619)&0xffffffff
    return value


def cases(source, count, seed):
    rng=random.Random(seed)
    buckets=[[],[],[]]
    for outline,text in sorted(source.items()):
        n=len(parse_outline(outline))
        buckets[min(n,3)-1].append((outline,text))
    chosen=[]
    for bucket in buckets:
        chosen.extend(rng.sample(bucket,min(count,len(bucket))))
    return chosen



def missing_outline(source, seed):
    """A real one-stroke hash miss, not an out-of-range-length shortcut."""
    present={parse_outline(outline) for outline in source}
    rng=random.Random(seed)
    left=((2,'S'),(4,'T'),(8,'K'),(16,'P'),(32,'W'),(64,'H'),(128,'R'))
    middle=((256,'A'),(512,'O'),(1024,'*'),(2048,'E'),(4096,'U'))
    right=((8192,'F'),(16384,'R'),(32768,'P'),(65536,'B'),(131072,'L'),(262144,'G'),(524288,'T'),(1048576,'S'),(2097152,'D'),(4194304,'Z'))
    for _ in range(1024):
        mask=rng.randrange(1,1<<23)
        a=''.join(c for bit,c in left if mask&bit)
        b=''.join(c for bit,c in middle if mask&bit)
        c=''.join(c for bit,c in right if mask&bit)
        outline=('#' if mask&1 else '')+a+b+('-' if c and not b else '')+c
        key=parse_outline(outline)
        assert key==(mask,)
        if key not in present:return outline
    raise RuntimeError('Could not select an absent one-stroke outline')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--serial',help='USB serial number, not a serial port')
    p.add_argument('--output',type=Path,default=ROOT/'build/dictionary-phase0'/datetime.now(timezone.utc).strftime('device-%Y%m%d-%H%M%SZ'))
    p.add_argument('--samples-per-length',type=int,default=3,help='Cases in each one/two/3+ stroke bucket')
    p.add_argument('--capture-seconds',type=int,default=0,help='Record physical Javelin typing instead of lookup tests (1–120 seconds)')
    p.add_argument('--reverse',action='store_true',help='Include one reverse lookup per dictionary; these can be slow')
    args=p.parse_args()
    if not 1<=args.samples_per_length<=32:p.error('--samples-per-length must be 1..32')
    if not 0<=args.capture_seconds<=120:p.error('--capture-seconds must be 0..120')
    args.output.mkdir(parents=True,exist_ok=False)
    report=dict(schema=1,started_utc=datetime.now(timezone.utc).isoformat(),host_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                seed=20261117,selected_usb_serial=args.serial,rows=[],limitations=['Read-only existing format baseline, not an A/B/C prototype comparison',
                'DWT elapsed cycles include interrupts; not isolated thread CPU time',
                'XIP counts cover instrumented word-load helpers; personal adapter reads are separate, not additive',
                'No hardware current measurement or cold-cache claim',
                'No destructive imports, layout changes or selection changes'])
    def save():
        (args.output/'raw.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    try:
        with open_console(args.serial) as console:
            def command(text,timeout=120):
                reply=console.command('dictionary_benchmark '+text,timeout=timeout)
                if reply.startswith('ERR'):raise RuntimeError(reply)
                return json.loads(reply)
            report['device']=command('meta')
            if report['device'].get('version')!=1:raise RuntimeError('Unsupported diagnostic benchmark protocol')
            report['base_status']=console.command('native_install status')
            report['selection']=console.command('list_dictionaries')
            report['personal_status']=json.loads(console.command('user_dictionary_status'))
            if report['personal_status'].get('format') != 'SQBCAT02' or not report['personal_status'].get('ready'):
                raise RuntimeError('A ready Native dictionary pool is required')
            hz=report['device']['timer_hz']
            if args.capture_seconds:
                console.command('dictionary_benchmark capture start')
                print(f'Recording {args.capture_seconds}s. With Javelin selected, type into a text editor. Keep the browser editor closed.',flush=True)
                try:time.sleep(args.capture_seconds)
                finally:console.command('dictionary_benchmark capture stop')
                trace=command('capture read');report['trace']=trace
                durations=[row[1] for row in trace['samples']]
                report['trace_summary']=summarize(durations,hz)
                report['trace_summary']['dropped_chords']=sum(row[3]==0 for row in trace['samples'])
                report['trace_summary']['ignored_chords']=sum(row[3]==2 for row in trace['samples'])
                report['limitations']+=['Trace covers final-release handler, engine translation, hints and display publication; excludes queue wait and USB/BLE delivery',
                                         'Processed outcome is not proof of exact translated text; compare the editor with the intended text',
                                         'Capture must start with all steno keys released and remain in ordinary Javelin mode']
                print(json.dumps(report['trace_summary'],indent=2))
            else:
                footprint=ROOT/'build/dictionary-phase0/footprint/footprint.json'
                if not footprint.exists():
                    raise RuntimeError('Run python3 tools/bench/dictionary_footprint.py first')
                meta=json.loads(footprint.read_text(encoding='utf-8'));report['source_pins']=meta['pins'];report['sources']=meta['sources']
                targets=['lapwing','plover','commands','numbers','uk','proper']
                # Footprint rows retain manifest ordering: commands, numbers, UK, proper.
                for index,(target,row) in enumerate(zip(targets,meta['measurements'])):
                    source_path=Path(row['source'])
                    if not source_path.is_absolute():source_path=ROOT/source_path
                    raw=source_path.read_bytes()
                    if hashlib.sha256(raw).hexdigest()!=row['source_sha256']:raise RuntimeError('Pinned corpus checksum mismatch')
                    source=json.loads(raw.decode('utf-8'))
                    chosen=cases(source,args.samples_per_length,report['seed']+index)
                    # A generated absent outline tests misses without assuming a familiar chord is absent.
                    missing=missing_outline(source,report['seed']+index)
                    for outline,expected in [*chosen,(missing,None)]:
                        result=command(f'lookup {target} {outline}')
                        samples=result['samples_cycles']
                        if not any(samples):raise RuntimeError('DWT timer did not advance')
                        passed=(result['hits']==(len(samples) if expected is not None else 0) and
                                (expected is None or result['fingerprint']==fnv(expected)))
                        record=dict(target=target,kind='hit' if expected is not None else 'miss',outline=outline,
                                    strokes=len(parse_outline(outline)),passed=passed,**result,**summarize(samples,hz))
                        report['rows'].append(record);save()
                        print(f'{target:8} {outline:24} p99 {record["p99_us"]:9.2f} us {"PASS" if passed else "FAIL"}',flush=True)
                        if not passed:raise RuntimeError('Lookup parity failed; raw report saved')
                    # Same deterministic outlines through the current personal log (usually misses) and enabled stack.
                    for target2 in ['personal','stack']:
                        for outline,_ in chosen[:2]:
                            result=command(f'lookup {target2} {outline}')
                            report['rows'].append(dict(target=target2,kind='observed',outline=outline,
                                                       **result,**summarize(result['samples_cycles'],hz)))
                    if args.reverse:
                        text=next((t for _,t in chosen if t.isascii() and t.isalpha()),None)
                        if text:
                            result=command(f'reverse {target} {text}',timeout=600)
                            report['rows'].append(dict(target=target,kind='reverse',text=text,
                                                       **result,**summarize(result['samples_cycles'],hz)))
                personal=json.loads(console.command("print_dictionary user_dictionary",timeout=1800))
                report['personal_export_entries']=len(personal)
                (args.output/'personal-before.json').write_text(json.dumps(personal,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                for outline,expected in sorted(personal.items())[:32]:
                    result=command(f'lookup personal {outline}')
                    passed=result['hits']==len(result['samples_cycles']) and result['fingerprint']==fnv(expected)
                    report['rows'].append(dict(target='personal',kind='existing-hit',outline=outline,passed=passed,
                                               **result,**summarize(result['samples_cycles'],hz)))
                    if not passed:raise RuntimeError('Existing personal lookup parity failed')
                final_status=json.loads(console.command('user_dictionary_status'))
                for key in ('generation','tail','personal_id'):
                    if final_status[key]!=report['personal_status'][key]:
                        raise RuntimeError('The Native dictionary pool changed during the benchmark')
                report['device_after']=command('meta')
                save()
                summary=args.output/'summary.csv'
                fields=['target','kind','outline','text','strokes','n','median_us','p95_us','p99_us','max_us','hits','xip_word_loads','personal_read_calls','personal_read_bytes','passed']
                with summary.open('w',encoding='utf-8',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(report['rows'])
    except BaseException as error:
        report['error']=str(error);raise
    finally:
        report['finished_utc']=datetime.now(timezone.utc).isoformat();save()
    print(f'Raw evidence: {args.output}',flush=True)


if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,ValueError) as error:sys.exit(f'Error: {error}')
