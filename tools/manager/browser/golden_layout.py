#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript flash-map code (layout.js, Manager milestone M3).

Oracle: native_layout.py (parse_layout, parse_maps, read_layout, read_images, validate_compact_layout). Cases:
  * maps: parse_maps / parse_layout on valid, odd and malformed flash maps (both pool sizes).
  * scans: read_layout against a scripted console (single-page MAP1, paged MAP2, changes and errors mid-scan); the commands
    sent and the replies served are recorded so the JavaScript must ask the same questions in the same order.
  * images: read_images against a device-shaped catalog, with damaged rows.
  * compact: validate_compact_layout on compiled images and on single-byte damage; the reads it makes are recorded too.

    python3 tools/manager/browser/golden_layout.py            # rewrite golden/layout_vectors.json
    python3 tools/manager/browser/golden_layout.py --check    # fail if the committed file is stale
"""
import base64
import hashlib
import json
from pathlib import Path
import random
import struct
import sys
import zlib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_snapshot as base_vectors  # noqa: F401  (puts the tools/ directories on sys.path)
from build_native_image import LARGE_POOL_END, SEGMENT, SMALL_POOL_END, XIP
from compile_dict import DictionaryCollectionBuilder
from golden_decode import dictionary
from golden_snapshot import catalog_of, fixture, ownership
from native_layout import parse_layout, parse_maps, read_images, read_layout, validate_compact_layout
from native_update import Snapshot

OUT = HERE / "golden" / "layout_vectors.json"
REGION_CHARS = "sbhjfr"


def outcome(function, *args):
    try:
        return {"value": function(*args)}
    except Exception as error:
        return {"error": {"type": type(error).__name__, "message": str(error)}}


def digested(function, *args):
    """Like outcome, but a flash map result is recorded as a hash of its exact JSON plus a readable summary."""
    result = outcome(function, *args)
    if "value" in result:
        value = result["value"]
        short = {"retained-journal": "J", "retained": "R"}
        result["value"] = {"digest": hashlib.sha256(json.dumps(value).encode("utf-8")).hexdigest(),
                           "summary": {**{k: v for k, v in value.items() if k != "segments"},
                                       "kinds": "".join(short.get(row["kind"], row["kind"]) for row in value["segments"])}}
    return result


def layer(rng, pool_end, kind):
    """One catalog map: 'v' valid mixed regions, 'e' empty, '?' unknown."""
    data = ["c", "c"] + ["."] * (pool_end - 2) + ["x"]
    if kind == "v":
        for index in range(2, pool_end):
            if rng.random() < 0.35:
                data[index] = rng.choice(REGION_CHARS)
    elif kind == "?":
        for index in range(2, pool_end):
            data[index] = "?"
    return "".join(data)


def maps_cases():
    rng = random.Random(7)
    cases = []

    def add(title, selected, access, states, first, second):
        cases.append({"title": title, "args": [selected, access, states, first, second],
                      "expect": digested(parse_maps, selected, access, states, first, second)})

    for pool_end in (SMALL_POOL_END, LARGE_POOL_END):
        label = "small" if pool_end == SMALL_POOL_END else "large"
        valid = layer(rng, pool_end, "v")
        other = layer(rng, pool_end, "v")
        empty = layer(rng, pool_end, "e")
        unknown = layer(rng, pool_end, "?")
        for selected in "01":
            for access in "wrp":
                add(f"{label} valid, slot {selected}, access {access}", selected, access, "vv", valid, other)
        add(f"{label} second slot empty", "0", "w", "ve", valid, empty)
        add(f"{label} first slot empty, active second", "1", "w", "ev", empty, valid)
        add(f"{label} active slot empty", "0", "w", "ev", empty, valid)
        add(f"{label} unknown alternate", "0", "w", "v?", valid, unknown)
        add(f"{label} unknown active", "0", "w", "?v", unknown, valid)
        add(f"{label} both unknown", "0", "r", "??", unknown, unknown)
        add(f"{label} empty claims but has regions", "0", "w", "ve", valid, valid)
        add(f"{label} unknown claims but has regions", "0", "w", "v?", valid, valid)
        add(f"{label} valid claims but has unknown", "0", "w", "vv", valid, unknown)
        add(f"{label} bad selected", "2", "w", "vv", valid, other)
        add(f"{label} bad access", "0", "x", "vv", valid, other)
        add(f"{label} bad states length", "0", "w", "v", valid, other)
        add(f"{label} bad state letter", "0", "w", "vx", valid, other)
        add(f"{label} unknown region letter", "0", "w", "vv", valid[:5] + "z" + valid[6:], other)
        add(f"{label} missing catalog roots", "0", "w", "vv", "xc" + valid[2:], other)
        add(f"{label} second catalog root wrong", "0", "w", "vv", "cs" + valid[2:], other)
        add(f"{label} missing final segment", "0", "w", "vv", valid[:-1] + "s", other)
        add(f"{label} second map shorter", "0", "w", "vv", valid, other[:-1])
        add(f"{label} second map longer", "0", "w", "vv", valid, other + "x")
        add(f"{label} odd pool size", "0", "w", "vv", valid[:100] + "x", other[:100] + "x")
        add(f"{label} only retained journals", "0", "w", "vv", layer(rng, pool_end, "e").replace(".", ".", 1),
            "cc" + "j" * (pool_end - 2) + "x")
        add(f"{label} retained everything", "0", "w", "vv", layer(rng, pool_end, "e"),
            "cc" + "".join(rng.choice("sbhjfr") for _ in range(pool_end - 2)) + "x")
        add(f"{label} every region on both", "1", "p", "vv", "cc" + (REGION_CHARS * 400)[:pool_end - 2] + "x",
            "cc" + (REGION_CHARS[::-1] * 400)[:pool_end - 2] + "x")
    return cases


def layout_cases(maps):
    cases = []

    def add(title, response):
        cases.append({"title": title, "response": response, "expect": digested(parse_layout, response)})

    valid = layer(random.Random(3), SMALL_POOL_END, "v")
    other = layer(random.Random(4), SMALL_POOL_END, "v")
    reply = f"OK MAP1 1 w vv {valid} {other}"
    add("valid", reply)
    add("pinned", reply.replace("1 w vv", "1 p vv"))
    add("trailing space and newline", reply + " \n")
    add("double spaces", reply.replace(" vv ", "  vv  "))
    add("too short", reply[:-1])
    add("missing a field", "OK MAP1 1 w vv " + valid)
    add("extra field", reply + " extra")
    add("wrong tag", reply.replace("MAP1", "MAP3"))
    add("not OK", reply.replace("OK", "NO", 1))
    add("large map in a one-page reply", f"OK MAP1 1 w vv {layer(random.Random(5), LARGE_POOL_END, 'v')} {layer(random.Random(6), LARGE_POOL_END, 'v')}")
    add("empty reply", "")
    add("whitespace reply", "   ")
    add("map in a tab-separated reply", reply.replace(" vv ", "\tvv\t"))
    return cases


class Script:
    """Serves canned replies in order and records the commands asked."""

    def __init__(self, replies):
        self.replies, self.commands = list(replies), []

    def command(self, text, timeout=30):
        self.commands.append(text)
        return self.replies[len(self.commands) - 1]


def scan_case(title, replies):
    console = Script(replies)
    case = {"title": title, "replies": list(replies)}
    case["expect"] = digested(read_layout, console)
    case["commands"] = console.commands
    return case


def pages(rng, states="vv", access="w", selected="1", first=None, second=None):
    first = first or layer(rng, LARGE_POOL_END, "v")
    second = second or layer(rng, LARGE_POOL_END, "v")
    header = ["7", "12", selected, access, states]  # two leading fields the reader only compares between pages
    out = []
    for start in range(0, LARGE_POOL_END + 1, 256):
        out.append(" ".join(["OK", "MAP2", str(LARGE_POOL_END + 1), str(start), *header, first[start:start + 256], second[start:start + 256]]))
    return out


def scans_cases():
    rng = random.Random(11)
    cases = []
    small_valid = layer(rng, SMALL_POOL_END, "v")
    small_other = layer(rng, SMALL_POOL_END, "v")
    cases.append(scan_case("single page", [f"OK MAP1 1 w vv {small_valid} {small_other}"]))
    cases.append(scan_case("single page, invalid", [f"OK MAP1 1 w vv {small_valid[:-1]} {small_other}"]))
    cases.append(scan_case("error on the first command", ["ERR Layout scan deferred until the flash and input are idle"]))
    cases.append(scan_case("bare OK", ["OK"]))
    cases.append(scan_case("busy", ["ERR Native flash busy"]))
    good = pages(rng)
    cases.append(scan_case("paged", good))
    cases.append(scan_case("paged, active slot 0, read-only", pages(rng, selected="0", access="r")))
    cases.append(scan_case("paged, unknown alternate", pages(rng, states="v?", second=layer(rng, LARGE_POOL_END, "?"))))
    cases.append(scan_case("paged, ERR on page 3", good[:2] + ["ERR Layout scan deferred until the flash and input are idle"]))
    cases.append(scan_case("paged, bare OK on page 2", [good[0], "OK"]))
    for index in range(1, 4):
        changed = list(good)
        parts = changed[index].split()
        parts[6] = "0" if parts[6] == "1" else "1"
        changed[index] = " ".join(parts)
        cases.append(scan_case(f"selected slot changes on page {index + 1}", changed))
        changed = list(good)
        parts = changed[index].split()
        parts[7] = "p"
        changed[index] = " ".join(parts)
        cases.append(scan_case(f"access changes on page {index + 1}", changed))
        changed = list(good)
        parts = changed[index].split()
        parts[8] = "ve"
        changed[index] = " ".join(parts)
        cases.append(scan_case(f"states change on page {index + 1}", changed))
    wrong_size = [reply.replace("MAP2 1024", "MAP2 512") for reply in good]
    cases.append(scan_case("wrong total size", wrong_size))
    wrong_start = list(good)
    wrong_start[2] = wrong_start[2].replace(" 512 ", " 256 ")
    cases.append(scan_case("page 3 claims the wrong offset", wrong_start))
    short_row = list(good)
    parts = short_row[1].split()
    parts[9] = parts[9][:-1]
    short_row[1] = " ".join(parts)
    cases.append(scan_case("short row on page 2", short_row))
    extra = list(good)
    extra[0] += " extra"
    cases.append(scan_case("extra field on page 1", extra))
    missing = list(good)
    missing[3] = " ".join(missing[3].split()[:-1])
    cases.append(scan_case("missing field on page 4", missing))
    for index in (1, 2):
        for field, label in ((4, "first"), (5, "second")):
            changed = list(good)
            parts = changed[index].split()
            parts[field] = str(int(parts[field]) + 1)
            changed[index] = " ".join(parts)
            cases.append(scan_case(f"{label} header field changes on page {index + 1}", changed))
    padded = list(good)
    padded[0] += " " + "." * 256
    cases.append(scan_case("a third row on page 1", padded))
    other_tag = list(good)
    other_tag[1] = other_tag[1].replace("MAP2", "MAP1", 1)
    cases.append(scan_case("page 2 carries another tag", other_tag))
    cases.append(scan_case("page 1 only, then ERR", [good[0], "ERR boom"]))
    bad_region = list(good)
    parts = bad_region[2].split()
    parts[9] = "z" + parts[9][1:]
    bad_region[2] = " ".join(parts)
    cases.append(scan_case("unknown region letter on page 3", bad_region))
    first_bad = pages(rng, first="xc" + layer(rng, LARGE_POOL_END, "v")[2:])
    cases.append(scan_case("catalog roots wrong after assembly", first_bad))
    return cases


def repatch(image, change):
    """Apply `change(payload)` to the catalog payload and fix its checksum, as a damaged-but-valid catalog."""
    address, size, _ = catalog_of(image)
    payload = bytearray(image[address:address + size])
    change(payload)
    image[address:address + size] = payload
    struct.pack_into("<II", image, SEGMENT + 16, size, zlib.crc32(payload))


class ImageConsole:
    def __init__(self, image, answer, mode=None):
        self.image, self.answer, self.mode = image, answer, mode
        self.commands, self.replies = [], []

    def command(self, text, timeout=30):
        self.commands.append(text)
        verb = text.split()[2]
        if verb == "begin":
            reply = "ERR Native flash busy" if self.mode == "busy" else self.answer
        elif verb == "read":
            _, _, _, start, count = text.split()
            reply = "OK " + base64.b64encode(bytes(self.image[int(start):int(start) + int(count)])).decode()
        elif verb == "abort":
            reply = "ERR abort failed" if self.mode == "abort-error" else "OK"
        else:
            raise AssertionError(text)
        self.replies.append(reply)
        return reply


def image_case(title, image, pool_end, mode=None, answer=None):
    address, size, crc = catalog_of(image)
    if answer is None:
        try:
            probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=pool_end)
            bitmap = ownership(probe, pool_end)
        except ValueError:  # a deliberately damaged catalog: any well-formed bitmap will do
            bitmap = "0" * (64 if pool_end == SMALL_POOL_END else 256)
        answer = f"OK {address} {size} {crc} {bitmap}"
    console = ImageConsole(image, answer, mode)
    case = {"title": title, "poolEnd": pool_end}
    case["expect"] = outcome(read_images, console)
    case.update(commands=console.commands, replies=console.replies)
    return case


def images_cases():
    cases = []
    for pool_end in (SMALL_POOL_END, LARGE_POOL_END):
        label = "small" if pool_end == SMALL_POOL_END else "large"
        base = fixture(pool_end)
        cases.append(image_case(f"{label} pool fixture", base, pool_end))
        cases.append(image_case(f"{label} pool, flash busy at begin", base, pool_end, mode="busy"))
        cases.append(image_case(f"{label} pool, abort fails", base, pool_end, mode="abort-error"))
    base = fixture(SMALL_POOL_END)

    def swap(payload):
        count = struct.unpack_from("<H", payload, 4)[0]
        first, last = 32 + 8, 32 + (count - 1) * 96 + 8
        payload[first:first + 2], payload[last:last + 2] = bytes(payload[last:last + 2]), bytes(payload[first:first + 2])
    swapped = bytearray(base)
    repatch(swapped, swap)
    cases.append(image_case("extents swapped between two rows (sorted by start)", swapped, SMALL_POOL_END))

    def unextent(payload):
        struct.pack_into("<H", payload, 32 + 96 * 2 + 8, 65535)
    skipped = bytearray(base)
    repatch(skipped, unextent)
    cases.append(image_case("a row without an extent is skipped", skipped, SMALL_POOL_END))

    def member(payload):
        payload[32 + 96 * 2 + 10] = 1
    grouped = bytearray(base)
    repatch(grouped, member)
    cases.append(image_case("a grouped member is refused", grouped, SMALL_POOL_END))

    def snapshot_flag(payload):
        payload[32 + 96 * 1 + 11] = 1
    flagged = bytearray(base)
    repatch(flagged, snapshot_flag)
    cases.append(image_case("a row flagged as a snapshot", flagged, SMALL_POOL_END))

    def bad_extent(payload):
        struct.pack_into("<H", payload, 32 + 8, 9000)
    out_of_range = bytearray(base)
    repatch(out_of_range, bad_extent)
    cases.append(image_case("an extent index past the table", out_of_range, SMALL_POOL_END))
    address, size, crc = catalog_of(base)
    cases.append(image_case("wrong catalog checksum", base, SMALL_POOL_END, answer=f"OK {address} {size} {crc ^ 1} " + "0" * 64))
    return cases


def compact_image(start, dictionaries):
    return bytes(DictionaryCollectionBuilder(XIP + start).build(dictionaries))


def compact_case(title, image, start, size=None, flips=(), cut=None, key=None, short=None):
    data = bytearray(image)
    for offset, mask in flips:
        data[offset] ^= mask
    if cut is not None:
        data = data[:cut]
    reads = []

    def read(address, length):
        reads.append([address, length])
        chunk = bytes(data[address - start:address - start + length])
        return chunk[:-short[1]] if short and short[0] == address else chunk

    size = len(image) if size is None else size
    case = {"title": title, "image": key, "start": start, "size": size, "flips": [list(f) for f in flips], "cut": cut, "short": short}
    try:
        validate_compact_layout(read, start, size)
        case["expect"] = {"ok": True}
    except Exception as error:
        case["expect"] = {"error": {"type": type(error).__name__, "message": str(error)}}
    case["reads"] = reads
    return case



def put(image, offset, data):
    """Flips (offset, xor) that make `data` appear at `offset`."""
    return [(offset + i, image[offset + i] ^ byte) for i, byte in enumerate(data) if image[offset + i] != byte]


def parse_fields(image, start):
    base = XIP + start
    pointer = struct.unpack_from("<I", image, 36)[0]
    definition = pointer - base
    name, = struct.unpack_from("<I", image, definition + 4)
    strokes, = struct.unpack_from("<I", image, definition + 12)
    directory, = struct.unpack_from("<I", image, len(image) - 8)
    groups = [struct.unpack_from("<III", image, strokes - base + 12 * i) for i in range(image[definition + 1])]
    return dict(base=base, definition=definition, maximum=image[definition + 1], name=name - base, strokes=strokes - base,
                directory=directory, groups=groups)


def compact_cases():
    images = {}
    big = dictionary(5, 900)
    specs = {
        "one": [("One", {"KAT": "cat", "KAT/TA": "cat"})],
        "singles": [("Singles", {"KAT": "cat", "TKOG": "dog", "PWAOEUT": "bite"})],
        "long outlines": [("Long", {"KAT/TKOG/KAT/TKOG/KAT/TKOG": "long", "KAT": "short"})],
        "large": [("Large", big)],
        "long name": [("A name that is quite a bit longer than usual", {"KAT": "cat"})],
    }
    cases = []
    for label, dictionaries in specs.items():
        for start in (0x30000, 0, 0x10000):
            image = compact_image(start, dictionaries)
            images[(label, start)] = image
            cases.append(compact_case(f"{label} at {start:#x}", image, start, key=f"{label}@{start}"))
    image = images[("one", 0x30000)]
    start = 0x30000
    size = len(image)
    hot = list(range(0, 64)) + list(range(size - 64, size))
    for offset in hot:
        cases.append(compact_case(f"one: flip byte {offset}", image, start, flips=[(offset, 0x80)], key="one@196608"))
    for offset in range(64, size - 64, max(1, (size - 128) // 40)):
        cases.append(compact_case(f"one: flip byte {offset} (spread)", image, start, flips=[(offset, 1)], key="one@196608"))
    for cut in (0, 4, 8, 36, 40, size - 13, size - 12, size - 1):
        cases.append(compact_case(f"one: short read, cut to {cut}", image, start, cut=cut, key="one@196608"))
    for delta in (-4, -1, 1, 4, -20, 20, 64):
        cases.append(compact_case(f"one: size claimed {delta:+d}", image, start, size=size + delta, key="one@196608"))
    for size_claim in (0, 11, 12, 40):
        cases.append(compact_case(f"one: size claimed {size_claim}", image, start, size=size_claim, key="one@196608"))
    wide = images[("long outlines", 0x30000)]
    for offset in list(range(0, 64)) + list(range(len(wide) - 40, len(wide))):
        cases.append(compact_case(f"long outlines: flip byte {offset}", wide, 0x30000, flips=[(offset, 0x40)], key="long outlines@196608"))
    large = images[("large", 0x10000)]
    for offset in list(range(0, 64)) + list(range(len(large) - 64, len(large))):
        cases.append(compact_case(f"large: flip byte {offset}", large, 0x10000, flips=[(offset, 0x08)], key="large@65536"))

    # Single fields set to chosen values: the limits the byte-flip survey cannot land on.
    u32 = lambda value: struct.pack("<I", value & 0xffffffff)
    named = {f"n{i}": [("N" * i, {"KAT": "cat", "TKOG": "dog"})] for i in range(1, 9)}
    for label, dictionaries in named.items():
        images[(label, 0x30000)] = compact_image(0x30000, dictionaries)
        cases.append(compact_case(f"{label} at 0x30000", images[(label, 0x30000)], 0x30000, key=f"{label}@196608"))
    for (label, start), image in list(images.items()):
        if start != 0x30000:
            continue
        key = f"{label}@{start}"
        f = parse_fields(image, start)
        size = len(image)

        def poke(title, offset, data, cut=None):
            cases.append(compact_case(f"{label}: {title}", image, start, flips=put(image, offset, data), key=key, cut=cut))

        for value in (0, 1, 16, 17, 255):
            poke(f"maximum strokes {value}", f["definition"] + 1, bytes([value]))
        for count in (0, 1, 3):
            for extra in (0, 1, 2):
                poke(f"hint table starts {extra} past a count of {count}", f["directory"], u32(f["directory"] - 8 * count + extra) + u32(count))
        poke("name with no terminator", f["name"], b"A" * min(45, size - f["name"]))
        poke("name pointer before the image", f["definition"] + 4, u32(0))
        # The total-size limit: find where growing the hint count tips the image over, and test each side of it.
        edge = None
        for count in range(0, f["directory"] // 8):
            probe = compact_case("probe", image, start, flips=put(image, f["directory"], u32(0) + u32(count)), key=key)
            if "error" in probe["expect"] and probe["expect"]["error"]["message"] == "Compact tables exceed the image size":
                edge = count
                break
        if edge is not None:
            for count in range(max(0, edge - 2), edge + 2):
                poke(f"hint count {count} against the size limit", f["directory"], u32(0) + u32(count))
        for length, (mask, data, offsets) in enumerate(f["groups"], 1):
            if not mask:
                continue
            row = f["strokes"] + 12 * (length - 1)
            hash_bytes = ((mask + 1) // 128) * 20
            tail = offsets - f["base"] + hash_bytes - 20
            masks = struct.unpack_from("<4I", image, tail)
            rank, = struct.unpack_from("<I", image, tail + 16)
            entries = ((rank + 1) & 0xffffffff) + sum(bits.bit_count() for bits in masks)
            data_bytes = entries * (3 + 3 * length)
            for value in (0x7fffff, 0xffffff, 0x1ffffff, 127, 128, 255, 0x3fffff):
                poke(f"group {length} mask {value:#x}", row, u32(value))
            for delta in (-1, 0, 1, 2):
                poke(f"group {length} data at the boundary {delta:+d}", row + 4, u32(offsets - data_bytes + delta))
            poke(f"group {length} data before the image base", row + 4, u32(f["base"] - 1))
            poke(f"group {length} data at the image base", row + 4, u32(f["base"]))
            poke(f"group {length} no entries", tail, u32(0) * 4 + u32(0xffffffff))
            for extra in (1, 2):
                poke(f"group {length} entry count {extra} over the hash capacity", tail + 16,
                     u32(mask + 1 - sum(bits.bit_count() for bits in masks) - 1 + extra))
            poke(f"group {length} entry count at the hash capacity", tail + 16, u32(mask + 1 - sum(bits.bit_count() for bits in masks) - 1))
        # The stroke table (and each later read) answered short by a whole word, a byte, or entirely.
        for drop in (1, 4, 12):
            cases.append(compact_case(f"{label}: stroke table reply short by {drop}", image, start, key=key,
                                      short=[start + f["strokes"], drop]))
    return cases, {f"{label}@{start}": image.hex() for (label, start), image in images.items()}


def generate():
    maps = maps_cases()
    compact, compact_images = compact_cases()
    return {"schema": 1, "maps": maps, "layouts": layout_cases(maps), "scans": scans_cases(), "images": images_cases(),
            "compact": compact, "compactImages": compact_images}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/layout_vectors.json is stale; rerun golden_layout.py")
        print("layout vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    data = json.loads(text)
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {sum(len(data[k]) for k in ('maps', 'layouts', 'scans', 'images', 'compact'))} cases)")


if __name__ == "__main__":
    main()
