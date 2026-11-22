#!/usr/bin/env python3
"""Bundle the Manager and its runtime files from a release commit."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
FOLDER = "StenoSquidManager"
README = """# Steno Squid Manager

Extract the whole ZIP. For the offline browser app, open StenoSquidManager.html
in desktop Chrome or Edge 137+. No installation or internet is needed, including
for the first factory dictionary install. See BROWSER-README.md for connection
and recovery details. Use the matching left firmware from this release.

The Python tools are also included and use the same Manager and dictionary code.
They require Python 3.10 or newer. Open a terminal in the StenoSquidManager folder:

    python3 -m pip install hidapi
    python3 stenosquid.py

On Windows, use `py -3` in place of `python3`. Close other dictionary tools
before opening Manager. Plug in the left half or pair it in your computer's
Bluetooth settings. Manager prefers USB when both are available. JSON imports,
USB Repack, and recovery restores require USB.

For a new keyboard, flash the matching Left and Right UF2s from the same
release. In the Python version, the first factory install downloads the pinned dictionary sources and
verifies their checksums.

For diagnostic serial commands, install `pyserial` separately.
Run `python3 stenosquid.py --help` for the command-line options.
"""


def package(output, revision, offline_bundle):
    revision = subprocess.check_output(["git", "rev-parse", f"{revision}^{{commit}}"], cwd=ROOT, text=True).strip()
    with zipfile.ZipFile(offline_bundle) as browser:
        if browser.testzip() is not None:
            raise ValueError("The offline browser bundle is damaged")
        if json.loads(browser.read("BUILD.json"))["source_revision"] != revision:
            raise ValueError("The offline browser bundle must be built from the same release commit")
        browser_files = {name: browser.read(name) for name in
                         ("StenoSquidManager.html", "THIRD_PARTY_LICENSES.txt", "README.md", "BUILD.json")}
    tracked = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", revision],
                                      cwd=ROOT, text=True).splitlines()
    files = ["LICENSE", "stenosquid.py", "requirements.txt",
             "tools/dictionaries/import_personal.py", "tools/dictionaries/manifest.py",
             "tools/dictionaries/prepare.py", "tools/dictionaries/serial_tools.py",
             "tools/storage/build_native_image.py", "tools/storage/hybrid.py"]
    files.extend(path for path in tracked if
                 (path.startswith("tools/storage/native_") and path.endswith(".py")) or
                 (Path(path).parent.as_posix() in {"tools/manager", "tools/dict_compiler"} and
                  Path(path).suffix in {".py", ".js", ".html", ".css"} and
                  not Path(path).name.startswith("test_")))
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        directory = Path(directory)
        archive = directory / output.name
        subprocess.run(["git", "archive", "--format=zip", f"--prefix={FOLDER}/",
                        f"--output={archive}", revision, "--", *files], cwd=ROOT, check=True)
        with zipfile.ZipFile(archive, "a") as bundle:
            readme = zipfile.ZipInfo(f"{FOLDER}/README.md", bundle.getinfo(f"{FOLDER}/").date_time)
            bundle.writestr(readme, README, compress_type=zipfile.ZIP_DEFLATED)
            for name, data in browser_files.items():
                target = "BROWSER-README.md" if name == "README.md" else name
                entry = zipfile.ZipInfo(f"{FOLDER}/{target}", readme.date_time)
                bundle.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED)
            bundle.extractall(directory)
        extracted = directory / FOLDER
        subprocess.run([sys.executable, "stenosquid.py", "--help"], cwd=extracted, check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, 'tools/manager'); "
                        "import manager; assert all((manager.HERE / name).is_file() "
                        "for name in ('index.html', 'manager.css', 'manager.js'))"],
                       cwd=extracted, check=True)
        archive.replace(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--offline-bundle", required=True, type=Path,
                        help="Tested StenoSquidManagerOffline.zip built from the same revision")
    args = parser.parse_args()
    package(args.output, args.revision, args.offline_bundle)


if __name__ == "__main__":
    main()
