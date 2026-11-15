#!/usr/bin/env python3
"""Print section sizes and RAM / flash use grouped by owner, as Markdown.

Usage: memory_by_owner.py <zephyr.elf>

Symbols come from the Zephyr SDK's arm-zephyr-eabi-nm. RAM is 0x20000000-0x30000000
and flash is below 0x100000. Owners are the source folder or object of each symbol.
"""
import collections
import glob
import re
import subprocess
import sys

SDK_BIN = '/opt/zephyr-sdk-*/arm-zephyr-eabi/bin/arm-zephyr-eabi-'


def tool(name):
    found = sorted(glob.glob(SDK_BIN + name))
    if not found:
        sys.exit(f'arm-zephyr-eabi-{name} not found in the Zephyr SDK')
    return found[0]


def owner(path):
    if not path:
        return '(no path)'
    path = path.rsplit(':', 1)[0]
    if '/repo/' in path:
        return 'repo/' + '/'.join(path.split('/repo/', 1)[1].split('/')[:3])
    if '/ws/' in path:
        return '/'.join(path.split('/ws/', 1)[1].split('/')[:4])
    if '/out/' in path:
        return 'out/' + '/'.join(path.split('/out/', 1)[1].split('/')[:2])
    return path.split('/')[-1]


def report(title, totals, symbols):
    print(f'{title}: {sum(totals.values())} bytes')
    print('```')
    for key, size in totals.most_common(20):
        print(f'{size:9d}  {key}')
    print('-- biggest symbols')
    for size, name, key in sorted(symbols, reverse=True)[:12]:
        print(f'{size:9d}  {name[:60]}  [{key[:50]}]')
    print('```')


def main(elf):
    print('```')
    print(subprocess.run([tool('size'), elf], capture_output=True, text=True, check=True).stdout.strip())
    print('```')
    nm = subprocess.run([tool('nm'), '-S', '-l', '--defined-only', elf],
                        capture_output=True, text=True, check=True, errors='replace').stdout
    ram, flash = collections.Counter(), collections.Counter()
    ram_symbols, flash_symbols = [], []
    for line in nm.splitlines():
        m = re.match(r'([0-9a-f]+) ([0-9a-f]+) \w (\S+)(?:\t(\S+))?', line)
        if not m:
            continue
        address, size, name, path = int(m[1], 16), int(m[2], 16), m[3], m[4]
        if not size:
            continue
        key = owner(path)
        if 0x20000000 <= address < 0x30000000:
            ram[key] += size
            ram_symbols.append((size, name, key))
        elif address < 0x100000:
            flash[key] += size
            flash_symbols.append((size, name, key))
    report('RAM (bss and data)', ram, ram_symbols)
    report('Flash (text and rodata)', flash, flash_symbols)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
