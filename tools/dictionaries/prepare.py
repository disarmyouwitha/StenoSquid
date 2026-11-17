"""Fetch, hash-check, compile and verify the pinned base dictionary images."""
import argparse
import hashlib
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

from manifest import BASES, ROOT, XIP_BASE


def fetch(url, path, sha):
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha:
        return
    context = None
    system_certs = Path('/etc/ssl/cert.pem')
    if sys.platform == 'darwin' and not ssl.get_default_verify_paths().cafile and system_certs.is_file():
        context = ssl.create_default_context(cafile=system_certs)
    with urlopen(url, timeout=60, context=context) as response:
        source = response.read()
    if hashlib.sha256(source).hexdigest() != sha:
        raise RuntimeError(f'Pinned {path.name} source checksum mismatch')
    path.write_bytes(source)


def prepare(name, output_dir=None):
    base = BASES[name]
    directory = Path(output_dir) if output_dir else ROOT / 'build' / base['directory']
    directory.mkdir(parents=True, exist_ok=True)
    sources = []
    for url, file, _, sha in base['sources']:
        path = directory / file
        fetch(url, path, sha)
        sources.append(path)
    address = hex(XIP_BASE + base['offset'])
    image = directory / base['image']
    compiler = ROOT / 'tools/dict_compiler'
    args = [sys.executable, str(compiler / 'compile_dict.py'), *map(str, sources)]
    for _, _, label, _ in base['sources']:
        args += ['--name', label]
    subprocess.run(args + ['--base-addr', address, '-o', str(image)], check=True)
    for index, source in enumerate(sources):
        subprocess.run([sys.executable, str(compiler / 'verify_mapped_dict.py'), str(source), str(image),
                        '--base-addr', address, '--index', str(index), '--count', str(len(sources))], check=True)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', choices=['lapwing', 'plover', 'extras', 'all'], default='all')
    args = parser.parse_args()
    for name in BASES if args.base == 'all' else [args.base.upper()]:
        prepare(name)


if __name__ == '__main__':
    main()
