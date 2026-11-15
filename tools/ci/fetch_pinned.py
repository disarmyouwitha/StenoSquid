#!/usr/bin/env python3
"""Fetch one project from config/west.yml at its pinned revision, without west.

Usage: fetch_pinned.py <west project name> <destination folder>
Run from the repository root. Used by the host tests, which need the pinned
Javelin engine and ZMK sources but not a full west workspace.
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main(name, destination):
    manifest = (ROOT / 'config/west.yml').read_text()
    remotes = dict(re.findall(r'- name: (\w+)\s+url-base: (\S+)', manifest))
    match = re.search(rf'- name: {re.escape(name)}\s+remote: (\w+)\s+revision: (\w+)', manifest)
    if not match:
        sys.exit(f'{name} not found in config/west.yml')
    url = f'{remotes[match[1]]}/{name}'
    subprocess.run(['git', 'init', '-q', destination], check=True)
    subprocess.run(['git', '-C', destination, 'fetch', '-q', '--depth', '1', url, match[2]], check=True)
    subprocess.run(['git', '-C', destination, 'checkout', '-q', 'FETCH_HEAD'], check=True)
    print(f'{name} {match[2]} -> {destination}')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
