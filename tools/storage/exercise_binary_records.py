#!/usr/bin/env python3
"""Check the binary codec against every pinned source entry, without devices."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from binary_journal import BinaryJournal
from hybrid import canonical
from journal import JournalHybrid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from manifest import BASES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = {
        "schema": 1,
        "host_only": True,
        "sources": [],
        "entries": 0,
        "binary_payload_bytes": 0,
        "json_payload_bytes": 0,
        "binary_page_bytes": 0,
        "json_page_bytes": 0,
    }
    identity = 0
    for base in BASES.values():
        for _, file, _, sha in base["sources"]:
            identity += 1
            raw = (args.sources / file).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == sha
            entries = json.loads(raw)
            for outline, text in entries.items():
                entry = dict(id=identity, outline=outline, text=text, op="set")
                payload = BinaryJournal._payload(entry)
                decoded = BinaryJournal._decode_payload(payload)
                assert decoded["text"] == text and canonical(
                    decoded["outline"]
                ) == canonical(outline)
                assert decoded["id"] == identity and decoded["op"] == "set"
                report["binary_payload_bytes"] += len(payload)
                report["json_payload_bytes"] += len(JournalHybrid._payload(entry))
                report["binary_page_bytes"] += BinaryJournal._encoded(entry, 1)[2]
                report["json_page_bytes"] += JournalHybrid._encoded(entry, 1)[2]
            report["entries"] += len(entries)
            report["sources"].append(dict(file=file, sha256=sha, entries=len(entries)))
    report["passed"] = True
    report["limits"] = [
        "Codec stress corpus only: bases remain compiled, not journal records",
        "Page padding and record headers remain; payload savings are not equal capacity savings",
        "Does not establish firmware latency or total memory",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("sources", "limits")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
