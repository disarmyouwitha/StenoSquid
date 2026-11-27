#!/usr/bin/env python3
"""Build a single offline HTML app; only maintainers need Python."""
import argparse
import base64
import io
import json
import re
import hashlib
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from manifest import BASES
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundle_modules import bundle as bundle_modules

# Keep the runtime pinned. Every runtime asset is embedded in the HTML,
# so opening Manager never needs network access.
PYODIDE_VERSION = "0.29.3"
RUNTIME_FILES = {
    "pyodide.mjs": "23213d27e9dacbda6f25f955fb38dabfc40ada082c04ee253047ea5dddcaa3b5",
    "pyodide.asm.js": "1263f02b5b26099b96112378156f242dd98b39a8201ba7765e5fe3d455c5ce91",
    "pyodide.asm.wasm": "e2f4ee75b325e35eb31bfb8c613d4dd5098f5502c156a97847686875b5025480",
    "python_stdlib.zip": "4298b6ee445cb724c3973437da47789752b9e6ff4e26619026b283ec801fc46b",
    "pyodide-lock.json": "3256ffc76388de0e37f4b34d42ab484268d1afc675179ff97b2a5bb14f84ccac",
}


def package(output, runtime=None):
    output = output.resolve()
    if output == ROOT or ROOT in output.parents and output.parts[len(ROOT.parts)] in {"tools", "src", "config", ".git"}:
        raise ValueError("Choose a separate build output directory")
    output.mkdir(parents=True, exist_ok=True)
    manager = ROOT / "tools/manager"
    files = [path for directory in ("tools/storage", "tools/dictionaries", "tools/dict_compiler", "tools/manager")
             for path in (ROOT / directory).glob("*.py") if not path.name.startswith("test_")]
    files.append(manager / "browser/bridge.py")
    runtime_zip = io.BytesIO()
    with zipfile.ZipFile(runtime_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(ROOT))
        for base in BASES.values():
            for _, name, _, sha in base["sources"]:
                path = ROOT / "build" / base["directory"] / name
                if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                    raise ValueError(f"Pinned dictionary checksum mismatch: {name}. Run tools/dictionaries/prepare.py first.")
                archive.write(path, Path("build") / base["directory"] / name)
    destination = output / "pyodide"
    destination.mkdir(exist_ok=True)
    for name in RUNTIME_FILES:
        if runtime:
            if (runtime / name).resolve() != (destination / name).resolve():
                shutil.copyfile(runtime / name, destination / name)
        else:
            with urlopen(f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/{name}", timeout=120,
                         context=ssl.create_default_context(cafile="/etc/ssl/cert.pem")
                         if sys.platform == "darwin" and Path("/etc/ssl/cert.pem").is_file() else None) as response:
                (destination / name).write_bytes(response.read())
        if hashlib.sha256((destination / name).read_bytes()).hexdigest() != RUNTIME_FILES[name]:
            raise ValueError(f"Pyodide runtime checksum mismatch: {name}")
    encoded = lambda data: base64.b64encode(data).decode("ascii")
    assets = {
        "runtime": encoded(runtime_zip.getvalue()),
        "lock": json.loads((destination / "pyodide-lock.json").read_text(encoding="utf-8")),
        "files": {name: encoded((destination / name).read_bytes()) for name in ("pyodide.asm.wasm", "python_stdlib.zip")},
    }
    setup = "globalThis.offlineAssets = " + json.dumps(assets, separators=(",", ":")) + ";\n" + r"""
globalThis.offlineBytes = (source) => Uint8Array.from(atob(source), character => character.charCodeAt(0));
// Pyodide asks its loader for two virtual assets. Both come from this HTML.
// No request is ever sent to a server, including on first launch.
globalThis.fetch = async (input) => {
  const url = String(input instanceof Request ? input.url : input);
  const name = url.startsWith("https://offline.invalid/") ? url.slice("https://offline.invalid/".length) : "";
  if (!(name in offlineAssets.files)) throw new Error("Offline Manager blocked an unexpected network request: " + url);
  return new Response(offlineBytes(offlineAssets.files[name]), {headers: {"Content-Type": name.endsWith(".wasm") ? "application/wasm" : "application/zip"}});
};
"""
    shim = (destination / "pyodide.mjs").read_text(encoding="utf-8").replace("export{ct as loadPyodide,U as version};", "globalThis.loadPyodide = ct;")
    # The page's modules (hid, console, the JavaScript Manager and everything they import) become one scoped script; see bundle_modules.py.
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
    html = html.replace('</body>', script(setup) + script((destination / "pyodide.asm.js").read_text(encoding="utf-8")) + script(shim, True) + script(backend + ui, True) + '</body>')
    (output / "StenoSquidManager.html").write_text(html, encoding="utf-8")
    shutil.copyfile(ROOT / "LICENSE", output / "LICENSE")
    shutil.copyfile(manager / "browser/README.md", output / "README.md")
    shutil.copyfile(manager / "browser/THIRD_PARTY_LICENSES.txt", output / "THIRD_PARTY_LICENSES.txt")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (output / "BUILD.json").write_text(json.dumps({"source_revision": revision, "pyodide_version": PYODIDE_VERSION}, indent=2) + "\n")
    with zipfile.ZipFile(output / "StenoSquidManagerOffline.zip", "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in ("StenoSquidManager.html", "README.md", "LICENSE", "THIRD_PARTY_LICENSES.txt", "BUILD.json"):
            bundle.write(output / name, name)
    print(f"Offline browser Manager built: {output / 'StenoSquidManager.html'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pyodide", type=Path, help="Reuse a local Pyodide 0.29.3 full distribution")
    args = parser.parse_args()
    package(args.output, args.pyodide)


if __name__ == "__main__":
    main()
