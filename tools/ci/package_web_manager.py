#!/usr/bin/env python3
"""Build the single-file offline Manager (HTML). Only maintainers need Python; users open the HTML in Chrome or Edge."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from manifest import BASES
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundle_modules import bundle as bundle_modules


def package(output):
    output = output.resolve()
    if output == ROOT or ROOT in output.parents and output.parts[len(ROOT.parts)] in {"tools", "src", "config", ".git"}:
        raise ValueError("Choose a separate build output directory")
    output.mkdir(parents=True, exist_ok=True)
    manager = ROOT / "tools/manager"
    # The pinned factory dictionaries travel inside the page; the Manager reads them for the editor, setup and factory restore.
    dictionaries = io.BytesIO()
    with zipfile.ZipFile(dictionaries, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for base in BASES.values():
            for _, name, _, sha in base["sources"]:
                path = ROOT / "build" / base["directory"] / name
                if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                    raise ValueError(f"Pinned dictionary checksum mismatch: {name}. Run tools/dictionaries/prepare.py first.")
                archive.write(path, Path("build") / base["directory"] / name)
    assets = {"dictionaries": base64.b64encode(dictionaries.getvalue()).decode("ascii")}
    setup = "globalThis.offlineAssets = " + json.dumps(assets, separators=(",", ":")) + ";\n" + r"""
globalThis.offlineBytes = (source) => Uint8Array.from(atob(source), character => character.charCodeAt(0));
// The page never talks to a server, including on first launch.
globalThis.fetch = async (input) => {
  throw new Error("Offline Manager blocked an unexpected network request: " + String(input instanceof Request ? input.url : input));
};
"""
    # The page's modules (hid, console, the Manager and everything they import) become one scoped script; see bundle_modules.py.
    backend = bundle_modules(manager / "browser/backend.js") + 'const {BrowserBackend} = __modules["backend"];\n'
    ui = (manager / "manager.js").read_text(encoding="utf-8").replace('new (await import("./backend.js")).BrowserBackend()', 'new BrowserBackend()')
    script = lambda code, module=False: '<script' + (' type="module"' if module else '') + '>' + re.sub(r"</script", r"<\\/script", code, flags=re.I) + '</script>'
    html = (manager / "index.html").read_text(encoding="utf-8").replace('<html lang="en">', '<html lang="en" data-backend="browser">')
    html = html.replace('  <meta name="manager-token" content="MANAGER_SESSION_TOKEN">\n', '')
    html = html.replace('  <link rel="stylesheet" href="/manager.css">', '<style>' + (manager / "manager.css").read_text(encoding="utf-8") + '</style>')
    html = html.replace('  <script type="module" src="/manager.js"></script>', '')
    notices = json.dumps({"project": (ROOT / "LICENSE").read_text(encoding="utf-8"),
                          "third_party": (manager / "browser/THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")}).replace("<", "\\u003c")
    html = html.replace('</body>', '<script id="manager-licenses" type="application/json">' + notices + '</script></body>')
    html = html.replace('</body>', script(setup) + script(backend + ui, True) + '</body>')
    (output / "StenoSquidManager.html").write_text(html, encoding="utf-8")
    shutil.copyfile(ROOT / "LICENSE", output / "LICENSE")
    shutil.copyfile(manager / "browser/README.md", output / "README.md")
    shutil.copyfile(manager / "browser/THIRD_PARTY_LICENSES.txt", output / "THIRD_PARTY_LICENSES.txt")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (output / "BUILD.json").write_text(json.dumps({"source_revision": revision}, indent=2) + "\n")
    with zipfile.ZipFile(output / "StenoSquidManagerOffline.zip", "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in ("StenoSquidManager.html", "README.md", "LICENSE", "THIRD_PARTY_LICENSES.txt", "BUILD.json"):
            bundle.write(output / name, name)
    print(f"Offline browser Manager built: {output / 'StenoSquidManager.html'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    package(args.output)


if __name__ == "__main__":
    main()
