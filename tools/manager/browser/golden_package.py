#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript factory-package planner (package_plan.js, Manager milestone M2).

Oracle: plan_package in tools/storage/native_base_update.py. Images have some factory dictionaries installed and some
absent (so both install and uninstall paths run), plus patched catalogs for each refusal. A package's compiled image is a
deterministic synthetic one (the planner only relocates and checksums it); the report and its confirmation hash are
compared in full.

    python3 tools/manager/browser/golden_package.py            # rewrite golden/package_vectors.json
    python3 tools/manager/browser/golden_package.py --check    # fail if the committed file is stale
"""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_plan as plan_vectors
import golden_snapshot as base_vectors  # also puts the tools/ directories on sys.path
import golden_snapshot_plan_extra as patches
from build_native_image import FACTORY_KEYS, LARGE_POOL_END, SMALL_POOL_END, SOURCES, build
from native_base_update import plan_package
from native_update import NoSpace

OUT = HERE / "golden" / "package_vectors.json"


def spec(length, pointers, name, delta=None):
    return {"length": length, "pointers": pointers, "pointerDelta": delta, "digest": hashlib.sha256(name.encode()).hexdigest()}


def build_compiled(item, name):
    length, pointers = item["length"], item["pointers"]
    data = bytearray((i * 7 + 3) % 251 for i in range(length))
    for offset in pointers:
        struct.pack_into("<I", data, offset, (offset * 13 + 8) % length)
    if item["pointerDelta"] is not None:
        struct.pack_into("<I", data, pointers[0], length + item["pointerDelta"])
    return SimpleNamespace(contents=bytes(data), pointers=list(pointers), digests={name: bytes.fromhex(item["digest"])})


def image_with(packages, pool_end):
    rows = [(identity, name, enabled) for identity, name, enabled, _ in SOURCES] + [(8, "Custom A", True)]
    entries = {(1, (0x10,)): (0, b"hello"), (8, (0x50,)): (0, b"alpha")}
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for _, name, _, relative in SOURCES:
            if relative:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"KAT": name}))
        image, _ = build(folder, collection=(rows, entries, 9), packages=packages, pool_end=pool_end)
    return bytearray(image)


def serialise(result):
    update, report = result
    return {"metadata": update.metadata, "payload": update.payload.hex(), "report": report,
            "segments": [[segment, len(block), hashlib.sha256(bytes(block)).hexdigest()] for segment, block in update.segments.items()]}


def outcome(function):
    try:
        return {"value": function()}
    except NoSpace as error:
        return {"error": str(error), "kind": "NoSpace"}
    except ValueError as error:
        return {"error": str(error), "kind": "ValueError"}


def generate():
    images, cases = {}, []
    present = {"lapwing-commands", "lapwing-base"}
    sources = {"partial": (image_with(present, SMALL_POOL_END), SMALL_POOL_END),
               "partial_large": (image_with(present, LARGE_POOL_END), LARGE_POOL_END),
               "full": (image_with(set(FACTORY_KEYS), SMALL_POOL_END), SMALL_POOL_END)}
    full = sources["full"][0]
    sources["shared"] = (patches.patch_catalog(full, patches.share_base("Lapwing Numbers", "Lapwing Commands")), SMALL_POOL_END)
    sources["flagged"] = (patches.patch_catalog(full, patches.patch_row("Lapwing Commands", {11: 1})), SMALL_POOL_END)
    sources["badbase"] = (patches.patch_catalog(full, patches.patch_row("Lapwing Numbers", {8: 200, 9: 0})), SMALL_POOL_END)
    def base_at_count(payload):
        count = struct.unpack_from("<H", payload, 6)[0]
        at = patches.row_at(payload, "Lapwing Numbers")
        struct.pack_into("<H", payload, at + 8, count)
        return payload
    sources["edgebase"] = (patches.patch_catalog(full, base_at_count), SMALL_POOL_END)
    sources["member"] = (patches.patch_catalog(full, patches.patch_row("Lapwing Numbers", {10: 1})), SMALL_POOL_END)
    sources["missing_row"] = (patches.patch_catalog(full, patches.patch_row("Lapwing Base", {16: ord("X")})), SMALL_POOL_END)
    partial = sources["partial"][0]
    sources["ext252"] = (patches.patch_catalog(partial, patches.pad_extents(252)), SMALL_POOL_END)
    sources["ext253"] = (patches.patch_catalog(partial, patches.pad_extents(253)), SMALL_POOL_END)
    for label, (data, pool_end) in sources.items():
        images[label] = plan_vectors.record_image(data, pool_end)

    def case(label, package, restore, item=None, protected=None, title=None):
        data, pool_end = sources[label]
        snapshot = plan_vectors.snapshot_of(data, pool_end)
        owned = plan_vectors.owned(data, pool_end) if protected is None else protected
        name = FACTORY_KEYS[package][1]
        compiled = None if item is None else build_compiled(item, name)
        cases.append({"kind": "package", "image": label, "package": package, "restore": restore, "compiled": item,
                      "title": title or f"{label}: {'install' if restore else 'uninstall'} {package}", "protected": sorted(owned),
                      "expect": outcome(lambda: serialise(plan_package(snapshot, owned, package, restore, compiled)))})

    one, two = "one", "two"
    for label in ("partial", "partial_large"):
        for package in FACTORY_KEYS:
            name = FACTORY_KEYS[package][1]
            installed = package in present
            case(label, package, True, spec(40000, [0, 8, 4096], name))
            case(label, package, False)
            if not installed:
                case(label, package, True, spec(90000, [0, 12, 70000], name), title=f"{label}: install {package} (two segments)")
    case("partial", "lapwing-numbers", True, None, title="install without a compiled image")
    case("partial", "lapwing-numbers", True, spec(40000, [0, 8], "Lapwing Numbers", delta=5), title="install with a bad pointer")
    case("partial", "lapwing-numbers", True, spec(40000, [0, 8], "Lapwing Numbers", delta=0), title="install with a pointer at the end")
    case("partial", "lapwing-numbers", True, spec(40000, [0, 8], "Lapwing Numbers", delta=-1), title="install with a pointer just inside")
    everything = set(range(SMALL_POOL_END + 1))
    tight = lambda free: everything - set(free)
    for free, title in (({252, 253, 254}, "only the reserve free"), ({251, 252, 253, 254}, "one image segment, no metadata room"),
                        ({250, 251, 252, 253, 254}, "exactly enough"), ({2, 3, 252, 253, 254}, "image runs need adjacent segments")):
        case("partial", "lapwing-numbers", True, spec(40000, [0, 8], "Lapwing Numbers"), protected=tight(free), title=f"install with {title}")
        case("partial", "lapwing-numbers", True, spec(90000, [0, 8], "Lapwing Numbers"), protected=tight(free), title=f"install two segments with {title}")
    case("partial", "lapwing-base", False, protected=tight({252, 253, 254}), title="uninstall with only the reserve free")
    case("partial", "lapwing-base", False, protected=tight({253, 254, 252, 251}), title="uninstall with four free segments")
    case("partial", "lapwing-base", False, protected=set(), title="uninstall with nothing protected")
    for package in ("lapwing-commands", "lapwing-numbers", "lapwing-base", "plover"):
        case("full", package, False, title=f"full: uninstall {package} (later extents are renumbered)")
    case("full", "lapwing-numbers", True, spec(40000, [0, 8], "Lapwing Numbers"), title="full: install an installed package")
    case("shared", "lapwing-commands", False, title="uninstall a package whose extent is shared")
    case("flagged", "lapwing-commands", False, title="uninstall a row with the snapshot flag")
    case("badbase", "lapwing-numbers", False, title="uninstall a row whose base is past the catalog")
    case("edgebase", "lapwing-numbers", False, title="uninstall a row whose base equals the extent count")
    case("member", "lapwing-numbers", False, title="uninstall a row naming a member")
    case("missing_row", "lapwing-base", False, title="a package whose row name no longer matches")
    case("ext252", "lapwing-numbers", True, spec(40000, [0, 8], "Lapwing Numbers"), title="install into a 252-extent catalog")
    case("ext253", "lapwing-numbers", True, spec(40000, [0, 8], "Lapwing Numbers"), title="install into a 253-extent catalog")
    case("ext253", "lapwing-base", False, title="uninstall from a 253-extent catalog")
    cases.append({"kind": "approval", "serial": "Sérial-123", "confirmation": "ab" * 32,
                  "expect": hashlib.sha256(("Sérial-123:" + "ab" * 32).encode()).hexdigest()})
    return {"schema": 1, "images": images, "cases": cases}


def main():
    text = json.dumps(generate(), indent=1) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/package_vectors.json is stale; rerun golden_package.py")
        print("package vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
