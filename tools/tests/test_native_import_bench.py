#!/usr/bin/env python3
"""Exercise the large-import harness with real native storage and a small oracle."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from build_native_image import build


def main():
    keys = [
        "S",
        "T",
        "K",
        "P",
        "W",
        "H",
        "R",
        "A",
        "O",
        "E",
        "U",
        "-F",
        "-R",
        "-P",
        "-B",
        "-L",
        "-G",
        "-T",
        "-S",
        "-D",
    ]
    source = {f"{keys[i // 20]}/{keys[i % 20]}": f"entry {i}" for i in range(300)}
    source["S/S"] = "ça va\n"
    source["S/T"] = ""
    source["/".join(["KAT"] * 16)] = "long outline"
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        input_path, report_path = temporary / "input.json", temporary / "report.json"
        input_path.write_text(json.dumps(source))
        image_path = ROOT / "build/native-store/image.bin"
        if not image_path.exists():
            image, _ = build(ROOT / "build")
            image_path = temporary / "image.bin"
            image_path.write_bytes(image)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/storage/bench_native_import.py"),
                "--input",
                str(input_path),
                "--image",
                str(image_path),
                "--limit",
                "0",
                "--report",
                str(report_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        report = json.loads(report_path.read_text())
        assert report["outcome"] == "complete"
        assert report["committed"] == report["verified_after_reopen"] == 301
        assert report["maintenance_jobs"] >= 2
        assert report["free_segments"] > 0
        assert len(report["storage_source_sha256"]) == 64
        assert report["cold_reopen_verified"]
        headers = temporary / "alternate-headers"
        headers.mkdir()
        (headers / "native_store.h").write_text(
            "#error EXPECTED_ALTERNATE_STORAGE_HEADER\n"
        )
        alternate = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/storage/bench_native_import.py"),
                "--input",
                str(input_path),
                "--image",
                str(image_path),
                "--limit",
                "1",
                "--report",
                str(temporary / "unused.json"),
                "--storage-headers",
                str(headers),
            ],
            text=True,
            capture_output=True,
        )
        assert (
            alternate.returncode != 0
            and "EXPECTED_ALTERNATE_STORAGE_HEADER" in alternate.stderr
        )
    print(
        "Native import harness: real factory pool, checkpointed UTF-8/empty/16-stroke entries complete cold-reopen oracle and alternate header selection passed"
    )


if __name__ == "__main__":
    main()
