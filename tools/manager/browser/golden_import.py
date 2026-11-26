#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript Plover JSON importer (importer.js, Manager milestone M1).

Oracle: parse_named and write_json in tools/dictionaries/import_personal.py. Parse cases are JSON texts (valid, invalid
and edge-case); each records the entries and outline names in order, or the exception type and message. Export cases
record the exact text write_json produces.

    python3 tools/manager/browser/golden_import.py            # rewrite golden/import_vectors.json
    python3 tools/manager/browser/golden_import.py --check    # fail if the committed file is stale
"""
import json
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_snapshot as base_vectors  # noqa: F401  (puts the tools/ directories on sys.path)
from import_personal import parse_named, write_json

OUT = HERE / "golden" / "import_vectors.json"
BS, NL, QUOTE = chr(92), chr(10), chr(34)


def error_message(error):
    # Python 3.13 reports these at the comma. Keep the fixture's established
    # messages and locations at the closing bracket across Python versions.
    if isinstance(error, json.JSONDecodeError):
        legacy = {
            "Illegal trailing comma before end of object": "Expecting property name enclosed in double quotes",
            "Illegal trailing comma before end of array": "Expecting value",
        }.get(error.msg)
        if legacy:
            pos = error.pos + 1
            while pos < len(error.doc) and error.doc[pos] in " \t\r\n":
                pos += 1
            return str(json.JSONDecodeError(legacy, error.doc, pos))
    return str(error)


def parse_case(title, source):
    record = {"title": title, "source": source}
    try:
        entries, names = parse_named(source)
        record["expect"] = {"entries": [[list(strokes), text.hex()] for strokes, text in entries.items()],
                            "names": [[list(strokes), outline] for strokes, outline in names.items()]}
    except Exception as error:
        record["expect"] = {"error": {"type": type(error).__name__, "message": error_message(error)}}
    return record


def export_case(title, source):
    entries, names = parse_named(source)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "out.json"
        write_json(path, entries, names)
        text = path.read_text(encoding="utf-8")
    return {"title": title, "entries": [[list(strokes), text_.hex()] for strokes, text_ in entries.items()],
            "names": [[list(strokes), outline] for strokes, outline in names.items()], "expect": text}


def generate():
    q = QUOTE
    parse_cases, export_cases = [], []

    def add(title, source):
        parse_cases.append(parse_case(title, source))

    add("a small dictionary", json.dumps({"KAT": "cat", "TKOG": "dog", "KAT/TKOG": "cat dog"}))
    add("integer-like keys keep file order", '{"10": "ten", "2": "two", "1": "one", "TKOG": "dog"}')
    add("numeric aliases with equal text", '{"1": "one", "#S": "one", "KAT": "cat"}')
    add("numeric aliases with different text", '{"1": "one", "#S": "uno"}')
    add("unicode text and escapes", '{"KAT": "caf' + BS + 'u00e9", "TKOG": "' + BS + 'ud83d' + BS + 'ude00", "S": "a' + BS + '/b", "T": "' + BS + 'n' + BS + 't' + BS + 'b"}')
    add("raw unicode", json.dumps({"KAT": "café 日本語 \U0001f600"}, ensure_ascii=False))
    add("whitespace everywhere", ' ' + NL + '{ ' + NL + q + 'KAT' + q + ' : ' + q + 'cat' + q + ' ' + NL + ', ' + q + 'TKOG' + q + ':' + q + 'dog' + q + NL + '} ' + NL)
    add("a raw unit separator inside a string", '{"KAT": "a' + chr(31) + 'b"}')
    add("the highest surrogate pair", '{"KAT": "' + BS + 'udbff' + BS + 'udfff"}')
    add("a high surrogate then a private-use escape", '{"KAT": "' + BS + 'ud800' + BS + 'ue000"}')
    add("a high surrogate then a high surrogate", '{"KAT": "' + BS + 'ud800' + BS + 'ud800' + BS + 'udc00"}')
    add("negative infinity as a value", '{"KAT": -Infinity}')
    add("an exponent with no digits", '{"KAT": 1e}')
    add("an exponent with digits", '{"KAT": 1e5}')
    add("a signed exponent and fraction", '{"KAT": -1.5E+3}')
    add("a fraction with no digits", '{"KAT": 1.}')
    add("garbage between pairs", '{"KAT": "cat"x"TKOG": "dog"}')
    add("two commas", '{"KAT": "cat",,"TKOG": "dog"}')
    add("a colon where a comma belongs", '{"KAT": "cat":"TKOG": "dog"}')
    add("an empty object", "{}")
    add("empty translation", '{"KAT": ""}')
    add("escaped key equals another key", '{"KAT": "a", "K' + BS + 'u0041T": "b"}')
    add("duplicate key", '{"KAT": "a", "TKOG": "b", "KAT": "c"}')
    add("nested duplicate key", '{"KAT": {"a": 1, "a": 2}}')
    add("duplicate key before a syntax error", '{"KAT": "a", "KAT": "b", }')
    add("nested duplicate before an outer syntax error", '{"X": {"a": 1, "a": 2}, }')
    add("top-level array", '["KAT", "cat"]')
    add("top-level string", '"KAT"')
    add("top-level number", "12")
    add("top-level null", "null")
    add("empty input", "")
    add("whitespace only", "  " + NL)
    add("byte order mark", "﻿" + '{"KAT": "cat"}')
    add("trailing comma", '{"KAT": "cat",}')
    add("trailing comma in a nested array", '{"KAT": ["a",]}')
    add("single quotes", "{'KAT': 'cat'}")
    add("a comment", '{"KAT": "cat"} // note')
    add("an unquoted key", '{KAT: "cat"}')
    add("a missing colon", '{"KAT" "cat"}')
    add("a missing comma", '{"KAT": "cat" "TKOG": "dog"}')
    add("extra data after the object", '{"KAT": "cat"} {}')
    add("an unterminated string", '{"KAT": "cat')
    add("an unterminated object", '{"KAT": "cat"')
    add("a raw newline inside a string", '{"KAT": "ca' + NL + 't"}')
    add("a raw tab inside a string", '{"KAT": "ca' + chr(9) + 't"}')
    add("an invalid escape", '{"KAT": "ca' + BS + 'xt"}')
    add("a short unicode escape", '{"KAT": "' + BS + 'u12"}')
    add("a bad hex digit in an escape", '{"KAT": "' + BS + 'u12G4"}')
    add("a lone high surrogate", '{"KAT": "' + BS + 'ud800"}')
    add("a lone low surrogate", '{"KAT": "' + BS + 'udc00"}')
    add("a high surrogate then a non-surrogate escape", '{"KAT": "' + BS + 'ud800' + BS + 'u0041"}')
    add("a surrogate pair split across text", '{"KAT": "' + BS + 'ud83d x' + BS + 'ude00"}')
    add("NaN as a value", '{"KAT": NaN}')
    add("Infinity as a value", '{"KAT": Infinity}')
    add("a number as a value", '{"KAT": 12}')
    add("a float as a value", '{"KAT": 1.5e3}')
    add("a leading-zero number", '{"KAT": 01}')
    add("a bare minus", '{"KAT": -}')
    add("true as a value", '{"KAT": true}')
    add("null as a value", '{"KAT": null}')
    add("an object as a value", '{"KAT": {"a": "b"}}')
    add("an array as a value", '{"KAT": ["a"]}')
    add("a NUL escape in a translation", '{"KAT": "a' + BS + 'u0000b"}')
    add("a raw NUL character", '{"KAT": "a' + chr(0) + 'b"}')
    add("an out-of-order outline", '{"TS": "ok", "ZZZ": "no"}')
    add("an empty outline", '{"": "x"}')
    add("an outline with an empty stroke", '{"KAT//TKOG": "x"}')
    add("an outline with a quote", '{"K' + BS + "'" + 'AT": "x"}')
    add("sixteen strokes", json.dumps({"/".join(["KAT"] * 16): "ok"}))
    add("seventeen strokes", json.dumps({"/".join(["KAT"] * 17): "no"}))
    add("a translation of 2047 bytes", json.dumps({"KAT": "a" * 2047}))
    add("a translation of 2048 bytes", json.dumps({"KAT": "a" * 2048}))
    add("a multibyte translation at the limit", json.dumps({"KAT": "日" * 682 + "a"}, ensure_ascii=False))
    add("a multibyte translation over the limit", json.dumps({"KAT": "日" * 683}, ensure_ascii=False))
    add("a key with a surrogate pair escape", '{"' + BS + 'ud83d' + BS + 'ude00": "x"}')
    add("the same strokes first different then equal", '{"1": "a", "#S": "a", "2": "b", "#T": "b"}')
    add("a large integer value", '{"KAT": 123456789012345678901234567890}')
    add("deep nesting in a value", '{"KAT": ' + "[" * 40 + "]" * 40 + "}")

    sources = ['{"KAT": "a' + BS + 'u001fb' + BS + 'bc' + BS + 'fd' + BS + 'u007fe"}', '{"KAT": "cat", "TKOG": "dog"}', '{"KAT": "' + BS + 'u00e9' + BS + '" quote' + BS + BS + ' back' + BS + 'n' + BS + 't' + BS + 'u0001' + BS + 'u007f' + BS + 'ud83d' + BS + 'ude00 end"}',
               '{"1": "one", "#S": "one", "KAT/TKOG": ""}', "{}", '{"KAT": "日本語", "TKOG": "café"}']
    for number, source in enumerate(sources, 1):
        export_cases.append(export_case(f"export {number}", source))
    return {"schema": 1, "parse": parse_cases, "export": export_cases}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/import_vectors.json is stale; rerun golden_import.py")
        print("import vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    data = json.loads(text)
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(data['parse']) + len(data['export'])} cases)")


if __name__ == "__main__":
    main()
