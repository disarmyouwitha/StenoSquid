#!/usr/bin/env python3
"""Package the tested offline Manager as one HTML file, with no extra files."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def package(output, revision, offline_bundle):
    revision = subprocess.check_output(["git", "rev-parse", f"{revision}^{{commit}}"], cwd=ROOT, text=True).strip()
    with zipfile.ZipFile(offline_bundle) as browser:
        if browser.testzip() is not None:
            raise ValueError("The offline browser bundle is damaged")
        if json.loads(browser.read("BUILD.json"))["source_revision"] != revision:
            raise ValueError("The offline browser bundle must be built from the same release commit")
        html = browser.read("StenoSquidManager.html")
        if b'id="manager-licenses"' not in html:
            raise ValueError("The standalone HTML must include its license notices")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        archive = Path(directory) / output.name
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("StenoSquidManager.html", html)
        archive.replace(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--offline-bundle", required=True, type=Path,
                        help="Tested internal browser build from the same revision")
    args = parser.parse_args()
    package(args.output, args.revision, args.offline_bundle)


if __name__ == "__main__":
    main()
