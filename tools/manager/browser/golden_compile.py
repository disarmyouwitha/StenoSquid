#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript dictionary compiler (compile.js, Manager milestone M1/M2).

Oracle: compile_dict.DictionaryCollectionBuilder and native_snapshot.compile_snapshot. Each case records the input
(ordered outline/translation pairs, or stroke tuples), then the image length, its SHA-256, a digest of the pointer
offsets, and the translation-length warnings; or the error. test_compile.mjs requires byte-identical output.

    python3 tools/manager/browser/golden_compile.py            # rewrite golden/compile_vectors.json
    python3 tools/manager/browser/golden_compile.py --check    # fail if the committed file is stale
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import random
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_decode as decode_vectors
import golden_snapshot as base_vectors  # also puts the tools/ directories on sys.path
from compile_dict import DictionaryCollectionBuilder
from native_snapshot import compile_snapshot

OUT = HERE / "golden" / "compile_vectors.json"
BASE = 0x12030000


def digest(builder, image):
    return {"length": len(image), "sha256": hashlib.sha256(image).hexdigest(),
            "pointers": hashlib.sha256(json.dumps(builder.pointer_offsets).encode()).hexdigest(), "pointerCount": len(builder.pointer_offsets)}


def compile_case(title, dictionaries, base_addr=0, max_bytes=127):
    builder = DictionaryCollectionBuilder(base_addr, max_translation_bytes=max_bytes)
    errors = io.StringIO()
    record = {"title": title, "kind": "compile", "dictionaries": [{"name": name, "entries": [list(pair) for pair in entries]} for name, entries in dictionaries],
              "baseAddr": base_addr, "maxTranslationBytes": max_bytes}
    try:
        with contextlib.redirect_stderr(errors):
            image = builder.build([(name, dict(entries)) for name, entries in dictionaries])
        warnings = [[int(m.group(1))] for m in re.finditer(r"warning: (\d+)-byte translation", errors.getvalue())]
        record["expect"] = {**digest(builder, image), "warnings": warnings}
    except ValueError as error:
        record["expect"] = {"error": str(error)}
    return record


def snapshot_case(title, entries):
    record = {"title": title, "kind": "snapshot", "name": "Snap", "entries": [[list(strokes), text.hex()] for strokes, text in entries.items()]}
    try:
        compiled = compile_snapshot("Snap", entries)
        record["expect"] = {"length": len(compiled.data), "sha256": hashlib.sha256(compiled.data).hexdigest(),
                            "pointers": hashlib.sha256(json.dumps(compiled.pointers).encode()).hexdigest(), "pointerCount": len(compiled.pointers)}
    except (ValueError, AssertionError, UnicodeDecodeError) as error:
        record["expect"] = {"error": type(error).__name__, "message": str(error)}
    return record


def generate():
    rng = random.Random(21)
    cases = []
    small = list(decode_vectors.dictionary(1, 60).items())
    big = list(decode_vectors.dictionary(2, 1500).items())
    other = list(decode_vectors.dictionary(3, 400).items())
    cases.append(compile_case("one small dictionary", [("Small", small)]))
    cases.append(compile_case("one small dictionary at a flash address", [("Small", small)], BASE))
    cases.append(compile_case("a larger dictionary (many hash blocks)", [("Big", big)], BASE))
    cases.append(compile_case("three dictionaries sharing texts", [("Big", big), ("Other", other), ("Small", small)], BASE))
    cases.append(compile_case("the same outlines in two dictionaries", [("A", small), ("B", small)]))
    cases.append(compile_case("empty dictionary", [("Empty", [])]))
    cases.append(compile_case("empty then non-empty", [("Empty", []), ("Small", small)]))
    cases.append(compile_case("no dictionaries", []))
    cases.append(compile_case("non-ASCII dictionary name", [("Déjà Vu 日本", small)]))
    cases.append(compile_case("one-stroke outlines only", [("Ones", [(f"{k}", t) for k, t in small if "/" not in k])]))
    cases.append(compile_case("a gap in outline lengths (1 and 3 strokes)", [("Gap", [("KAT", "cat"), ("KAT/TKOG/PWAOEUD", "cat dog bird"), ("TKOG", "dog")])]))
    cases.append(compile_case("sixteen-stroke outline", [("Long", [("/".join(["KAT"] * 16), "sixteen"), ("KAT", "one")])]))
    cases.append(compile_case("numeric alias outlines collide", [("Alias", [("1", "first"), ("#S", "second"), ("2", "x"), ("#T", "y")])]))
    cases.append(compile_case("a repeated outline keeps its first position", [("Dup", [("KAT", "a"), ("TKOG", "b"), ("KAT", "c")])]))
    cases.append(compile_case("empty and unicode translations", [("Text", [("KAT", ""), ("TKOG", "café"), ("TKPWAOEG", "日本語"), ("PWAOEUD", "{^ing}"), ("S", "\U0001f600")])]))
    cases.append(compile_case("identical translations deduplicate", [("Same", [("KAT", "x"), ("TKOG", "x"), ("TKPWAOEG", "x"), ("S", "y")])]))
    cases.append(compile_case("hint index: fewest keys then lowest mask", [("Hint", [("STK", "w"), ("S", "w"), ("T", "w"), ("SK", "w"), ("-S", "v"), ("-T", "v"), ("*", "u"), ("#", "t"), ("1", "t")])]))
    long127, long128, long2047 = "a" * 127, "b" * 128, "c" * 2047
    cases.append(compile_case("translations at the 127-byte limit", [("Limit", [("KAT", long127), ("TKOG", long128), ("S", long2047)])]))
    cases.append(compile_case("translations with a raised limit", [("Limit", [("KAT", long127), ("TKOG", long128), ("S", long2047)])], max_bytes=2047))
    cases.append(compile_case("multibyte translation over the limit", [("Limit", [("KAT", "é" * 64), ("TKOG", "日" * 43)])]))
    cases.append(compile_case("invalid outline", [("Bad", [("KAT", "ok"), ("TS", "out of order")])]))
    cases.append(compile_case("empty outline", [("Bad", [("", "x")])]))
    cases.append(compile_case("outline with an empty stroke", [("Bad", [("KAT//TKOG", "x")])]))
    cases.append(compile_case("invalid outline in the second dictionary", [("Good", small), ("Second", [("ZZZ", "x")])]))
    cases.append(compile_case("quote in a bad outline", [("Bad", [("K'AT", "x")])]))
    for count in (1, 2, 63, 64, 65, 127, 128, 129, 191, 192, 193, 256, 257):
        pool = {}
        while len(pool) < count:
            pool[decode_vectors.outline(rng, 1)] = "t%d" % len(pool)
        cases.append(compile_case(f"{count} one-stroke entries (hash block count boundary)", [("Edge", list(pool.items()))]))
    stress = [(decode_vectors.outline(rng, rng.choice((1, 1, 2, 3))), "t%d" % rng.randrange(40)) for _ in range(900)]
    cases.append(compile_case("many entries sharing few translations", [("Stress", stress)], BASE))

    # compile_snapshot: stroke tuples and UTF-8 bytes.
    def masks(count):
        out = {}
        while len(out) < count:
            length = rng.choice((1, 1, 2, 3))
            out[tuple(rng.randrange(1, 1 << 24) for _ in range(length))] = ("v%d" % rng.randrange(10**6)).encode()
        return out

    cases.append(snapshot_case("a small snapshot", {(0x10,): b"hello", (0x10, 0x20): b"hello world", (0x44,): "café".encode()}))
    cases.append(snapshot_case("random masks", masks(150)))
    cases.append(snapshot_case("an empty snapshot", {}))
    cases.append(snapshot_case("a zero stroke", {(0,): b"x"}))
    cases.append(snapshot_case("a stroke over 24 bits", {(1 << 24,): b"x"}))
    cases.append(snapshot_case("a stroke just under 24 bits", {((1 << 24) - 1,): b"x"}))
    cases.append(snapshot_case("seventeen strokes", {tuple([0x10] * 17): b"x"}))
    cases.append(snapshot_case("sixteen strokes", {tuple([0x10] * 16): b"x"}))
    cases.append(snapshot_case("a translation of 2047 bytes", {(0x10,): b"a" * 2047}))
    cases.append(snapshot_case("a translation of 2048 bytes", {(0x10,): b"a" * 2048}))
    cases.append(snapshot_case("a NUL in a translation", {(0x10,): b"a\0b"}))
    cases.append(snapshot_case("invalid UTF-8", {(0x10,): b"\xff\xfe"}))
    cases.append(snapshot_case("two outlines that render alike", {(0x2,): b"a", (0x3,): b"b"}))
    cases.append(snapshot_case("masks that do not survive formatting", {(0x800000,): b"caret", (0x400000 | 0x2,): b"z"}))
    return {"schema": 1, "cases": cases}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/compile_vectors.json is stale; rerun golden_compile.py")
        print("compile vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
