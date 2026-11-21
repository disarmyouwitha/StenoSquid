#!/usr/bin/env python3
"""Check reproducible estimates, dictionary identity and read-only runner reports."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/bench'))
import dictionary_footprint as footprint
import run_dictionary_benchmark as runner


def main():
    assert footprint.packed_segments([])==0
    assert footprint.packed_segments([65520])==1
    assert footprint.packed_segments([65520,4])==2
    # Identical outlines in separate dictionaries must remain separate records.
    x=footprint.estimate_log([{'KAT':'cat'},{'KAT':'dog'}])
    assert x['entries']==2 and x['packed_record_bytes']==40
    assert x['index_allocated_bytes']==2*65536
    for seed in range(32):
        miss=runner.missing_outline({'KAT':'cat'},seed)
        assert len(runner.parse_outline(miss))==1 and miss!='KAT'
    assert runner.fnv('cat')==108289031
    assert runner.percentile([4,1,3,2],0.95)==4
    assert runner.summarize([64,128],64000000)['max_us']==2
    assert runner.cases({'KAT':'cat','HE/HROE':'hello'},1,12)==runner.cases({'HE/HROE':'hello','KAT':'cat'},1,12)
    class FakeConsole:
        commands=[]
        def command(self,text,timeout=30):
            self.commands.append(text)
            if text=='dictionary_benchmark meta':return '{"version":1,"timer_hz":64000000}'
            if text=='native_install status':return 'NATIVE2 READY 1'
            if text=='list_dictionaries':return '[]'
            if text=='user_dictionary_status':return '{"format":"SQBCAT02","ready":true,"generation":1,"tail":0,"personal_id":1}'
            if text=='print_dictionary user_dictionary':return '{"KAT":"cat"}'
            if text in ['dictionary_benchmark capture start','dictionary_benchmark capture stop']:return 'OK'
            if text=='dictionary_benchmark capture read':return '{"overflow":0,"samples":[[8,64,42,1],[4,128,43,0]]}'
            kind,target,outline=text.removeprefix('dictionary_benchmark ').split(' ',2)
            assert kind=='lookup'
            expected={'KAT':'cat','HE/HROE':'hello'}.get(outline)
            return json.dumps(dict(samples_cycles=[64,128],hits=2 if expected else 0,fingerprint=runner.fnv(expected) if expected else 2166136261,
                                   xip_word_loads=7,personal_read_calls=0,personal_read_bytes=0,bracket_cycles=2))
    @contextlib.contextmanager
    def connection(serial):yield FakeConsole()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);folder=root/'build/dictionary-phase0/footprint';folder.mkdir(parents=True)
        source=folder/'fixture.json';source.write_text('{"KAT":"cat","HE/HROE":"hello"}')
        row={'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
        (folder/'footprint.json').write_text(json.dumps({'pins':{},'sources':[],'measurements':[row]*6}))
        with mock.patch.object(runner,'ROOT',root),mock.patch.object(runner,'open_console',connection),mock.patch.object(runner.subprocess,'check_output',return_value='a'*40+'\n'),contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(sys,'argv',['bench','--output',str(root/'lookup'),'--samples-per-length','1']):runner.main()
            report=json.loads((root/'lookup/raw.json').read_text())
            assert len(report['rows'])==43 and 'error' not in report
            assert report['personal_export_entries']==1
            assert (root/'lookup/personal-before.json').is_file() and (root/'lookup/summary.csv').is_file()
            with mock.patch.object(sys,'argv',['bench','--output',str(root/'trace'),'--capture-seconds','1']),mock.patch.object(runner.time,'sleep'):
                runner.main()
            trace=json.loads((root/'trace/raw.json').read_text())
            assert trace['trace_summary']['dropped_chords']==1
            assert 'dictionary_benchmark capture stop' in FakeConsole.commands
        assert not any(c.startswith(('add_user_entry','reset_user_dictionary','remove_user_entry','native_install begin')) for c in FakeConsole.commands)
    print('Phase-0 host: estimate accounting, identity, deterministic cases, raw/CSV reports, personal hits and read-only capture passed')


if __name__=='__main__':main()
