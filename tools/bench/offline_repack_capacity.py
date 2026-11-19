#!/usr/bin/env python3
"""Repack full pinned and legal synthetic corpora in host-owned test images."""
import argparse
import hashlib
import json
from pathlib import Path

from snapshot_feasibility import ROOT, apply, occupied, open_image, synthetic
from build_native_image import build
from import_personal import parse_named
from native_base_update import PINS
from native_recovery import capture
from native_repack import repack
from native_snapshot import compile_snapshot, plan_change


def measure(image):
    source = open_image(image)
    bundle = capture(lambda at, n: image[at:at + n], source, "Mac U.S.")
    _, plan = repack(bundle)
    return {key: ({key: value for key, value in value.items() if key != "regions"}
                  if key in {"source_layout", "target_layout"} else value)
            for key, value in plan.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = (ROOT / "build/plover/plover-main.json").read_bytes()
    pin = hashlib.sha256(raw).hexdigest()
    assert pin == PINS["Plover Base"][2]
    plover, _ = build(ROOT / "build", personal=parse_named(raw)[0])
    report = {"plover_sha256": pin, "full_147424_entry_personal_journal": measure(plover)}
    factory, _ = build(ROOT / "build")
    source = open_image(factory)
    compact = compile_snapshot("user_dictionary", synthetic(200000))
    staged = plan_change(source, occupied(source), source.personal, compact, convert=True)
    indexed, _ = apply(factory, source, staged)
    report["existing_200000_entry_personal_snapshot"] = measure(indexed)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
