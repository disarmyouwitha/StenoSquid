#!/usr/bin/env python3
"""Replay a pinned stenography passage through the real host Javelin/Native stack."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/tests"))
from hostlib import compiler
from run_repeat_translation import build_engine
from run_dictionary_benchmark import percentile
from snapshot_feasibility import apply, occupied, open_image
from build_native_image import SEGMENT, build
from compile_dict import parse_outline
from native_snapshot import compile_snapshot, plan_change
from native_update import Edit, encode_record

CORPUS = ROOT / "docs/fixtures/adaptive-snapshot/typing-passage.json"
RATES = (100, 150, 200, 300)


def pool(corpus, output):
    image, manifest = build(ROOT / "build")
    view = open_image(image)
    baseline = {parse_outline(outline): text.encode() for outline, text in corpus["personal_snapshot"].items()}
    compiled = compile_snapshot("user_dictionary", baseline)
    update = plan_change(view, occupied(view), view.personal, compiled, convert=True)
    image, view = apply(image, view, update)
    segment = view.journals[-1][0]
    address = segment * SEGMENT + 256 + view.used[segment]
    for sequence, (outline, text) in enumerate(corpus["personal_overlay"].items(), view.highwater + 1):
        entry = Edit(address, view.personal, int(text is None), parse_outline(outline), (text or "").encode())
        blob = encode_record(entry, sequence)
        image[address:address + len(blob)] = blob
        address += entry.span
    (output / "simulated-pool.bin").write_bytes(image)
    return dict(sha256=hashlib.sha256(image).hexdigest(), pinned_images=manifest["base_images"])


def schedule(corpus, minimum_seconds, burst_seconds, virtual):
    words = sum(len(re.findall(r"[A-Za-z]+", row[1])) for passage in corpus["passages"] for row in passage)
    if not words:
        raise ValueError("No words in replay tape.")
    phases = {}
    for rate in RATES:
        minimum = burst_seconds if rate == 300 else minimum_seconds
        repeats = 1 if virtual else math.ceil(minimum * rate / (60 * words))
        events, expected = [], ""
        for _ in range(repeats):
            for passage in corpus["passages"]:
                for outline, final, *intermediate in passage:
                    strokes = outline.split("/")
                    suffixes = [*intermediate, final]
                    if len(strokes) != len(suffixes):
                        raise ValueError(f"Missing intermediate output for {outline}")
                    for stroke, suffix in zip(strokes, suffixes):
                        if expected.endswith(".") and suffix != ".":
                            suffix = suffix.capitalize()
                        join = " " if expected and suffix != "." else ""
                        events.append((stroke, expected + join + suffix))
                    expected = events[-1][1]
            miss = corpus["miss_with_undo"]
            events.extend(((miss, expected + " " + miss), ("*", expected)))
        count = len(events)
        # Uniform chord cadence within a phase, derived from the actual word
        # count. The deliberate miss and undo consume chord slots, zero words.
        active = round(60e9 * words * repeats / rate)
        chords_per_cycle = count // repeats
        gap = 2_000_000_000 if rate == 300 else 0
        idle = (repeats - 1) * gap
        events = [(round(i * active / count) + ((i - 1) // chords_per_cycle) * gap, stroke, state)
                  for i, (stroke, state) in enumerate(events, 1)]
        phases[rate] = dict(repeats=repeats, words=words * repeats, chords=count, active_ns=active,
                            idle_ns=idle, duration_ns=active + idle, expected_text=expected, events=events)
    return phases


def latency(values):
    return {f"p{percent}": percentile(values, percent / 100) / 1e3 for percent in (50, 95, 99)} | {
        "max": max(values) / 1e3}


def execute(binary, output, corpus, phases, virtual, *, verify_negative, live_tail=False):
    probes = output / "probes.tsv"
    probes.write_text("".join(f"{outline}\t{owner}\t{text or ''}\n" for outline, text, owner in corpus["probes"]),
                      encoding="ascii")
    tape = output / "tape.tsv"
    with tape.open("w", encoding="ascii") as stream:
        for rate, phase in phases.items():
            stream.write(f"{rate} {phase['chords']}\n")
            for deadline, stroke, expected in phase["events"]:
                stream.write(f"{deadline}\t{stroke}\t{expected}\n")
    command = [str(binary), str(output / "simulated-pool.bin"), str(probes), str(tape)]
    options = ["live-tail"] if live_tail else []
    if verify_negative:
        bad = output / "bad-output.tsv"
        first, row, rest = tape.read_text(encoding="ascii").split("\n", 2)
        bad.write_text(first + "\n" + row + " WRONG\n" + rest, encoding="ascii")
        refusal = subprocess.run([*command[:-1], str(bad), "virtual", *options], capture_output=True, text=True)
        if refusal.returncode != 1 or f"{next(iter(phases))} WPM stroke 1" not in refusal.stderr:
            raise RuntimeError(f"A corrupted output oracle did not fail at the first stroke "
                               f"(exit {refusal.returncode}): {refusal.stderr.strip()}")
    with (output / "samples.tsv").open("w", encoding="ascii") as samples:
        replay = subprocess.run([*command, "virtual" if virtual else "paced", *options], stdout=samples,
                                stderr=subprocess.PIPE, text=True)
    if replay.returncode:
        raise RuntimeError(f"Replay exited with {replay.returncode}: {replay.stderr.strip()}")
    measurements = {rate: [] for rate in phases}
    with (output / "samples.tsv").open(encoding="ascii") as samples:
        for row in samples:
            rate, index, *sample = map(int, row.split())
            values = measurements[rate]
            if index != len(values) + 1:
                raise ValueError("Missing or out-of-order replay sample.")
            values.append(sample)
    for rate, phase in phases.items():
        values = measurements[rate]
        if len(values) != phase["chords"]:
            raise ValueError(f"Incomplete replay at {rate} WPM.")
        process, lateness, finished, reads, read_bytes, probes, disabled_probes = zip(*values)
        interval = phase["active_ns"] / phase["chords"]
        phase.update(
            planned_strokes_per_second=phase["chords"] * 1e9 / phase["active_ns"],
            verified_chords=len(values), errors=0, engine_us=latency(process),
            journal_reads=sum(reads), journal_bytes=sum(read_bytes),
            compact_provider_calls=sum(probes), disabled_compact_provider_calls=sum(disabled_probes),
            deadline_lateness_us=None if virtual else latency(lateness),
            deadline_overruns=None if virtual else sum(p + lag >= interval for p, lag in zip(process, lateness)),
            achieved_wpm=None if virtual else phase["words"] * 60e9 / (finished[-1] - phase["idle_ns"]),
        )
        del phase["events"]
        print(f"{rate:3} target WPM · {phase['words']} words · {phase['chords']} verified chords · "
              f"engine p99 {phase['engine_us']['p99']:.1f} µs", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", type=Path, help="Pinned javelin-steno checkout")
    parser.add_argument("--out", type=Path, default=ROOT / "build/typing-replay")
    parser.add_argument("--seconds", type=int, default=60, help="Minimum duration per sustained rate")
    parser.add_argument("--burst-seconds", type=int, default=20, help="Minimum active time across 300 WPM bursts")
    parser.add_argument("--virtual", action="store_true", help="One passage cycle per rate with a virtual clock for CI")
    args = parser.parse_args()
    if args.seconds < 1 or args.burst_seconds < 1:
        parser.error("Phase durations must be positive")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    raw = CORPUS.read_bytes()
    corpus = json.loads(raw)
    (out / "corpus.json").write_bytes(raw)
    phases = schedule(corpus, args.seconds, args.burst_seconds, args.virtual)
    report = dict(
        timing_mode="virtual deadlines" if args.virtual else "native steady-clock absolute deadlines",
        host=dict(os=platform.system(), architecture=platform.machine()),
        requested_sustained_seconds=args.seconds, requested_burst_seconds=args.burst_seconds,
        corpus_sha256=hashlib.sha256(raw).hexdigest(),
        timing="Words in final prose determine phase duration. All strokes occupy equal active slots. 300 WPM cycles have 2 s idle gaps excluded from achieved WPM.",
        scope="Host-only Javelin/Native engine and captured HID key events. No physical scan, split, USB/BLE delivery, QSPI timing, or firmware scheduling.",
        phases={},
    )
    try:
        checked_engine = subprocess.check_output(["git", "-C", str(args.engine), "rev-parse", "HEAD"], text=True).strip()
        manifest = (ROOT / "config/west.yml").read_text(encoding="utf-8")
        pinned = re.search(r"- name: javelin-steno\s+remote: \S+\s+revision: ([0-9a-f]{40})", manifest)
        if not pinned or checked_engine != pinned[1]:
            raise ValueError("The replay requires the pinned Javelin checkout from config/west.yml.")
        if subprocess.check_output(["git", "-C", str(args.engine), "status", "--porcelain", "--untracked-files=normal"]):
            raise ValueError("The pinned Javelin checkout contains local modifications.")
        report["engine_commit"] = checked_engine
        report["image"] = pool(corpus, out)
        binary = build_engine(args.engine, out / "native", Path(__file__).with_name("typing_replay.cc"), compiler(),
                              optimization="-O2")
        execute(binary, out, corpus, phases, args.virtual, verify_negative=args.virtual)
        report.update(phases=phases, exact_lookup_probes=2 * len(corpus["probes"]), result="pass")
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        report.update(result="fail", error=str(exc))
        raise
    finally:
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
