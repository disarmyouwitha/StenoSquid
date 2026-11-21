#!/usr/bin/env python3
"""Run the native regression suite, including actual display rendering.

python3 tools/tests/run_all.py --workspace /path/to/west-workspace
CI can instead supply --engine, --zmk and --lvgl individually.
Pinned dictionary sources must be prepared first with tools/dictionaries/prepare.py.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from hostlib import compiler as host_compiler

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--engine', type=Path)
    parser.add_argument('--zmk', type=Path)
    parser.add_argument('--lvgl', type=Path)
    parser.add_argument('--full', action='store_true', help='Full randomized personal-store test')
    args = parser.parse_args()
    engine = args.engine or (args.workspace / 'javelin-steno' if args.workspace else ROOT / 'javelin-steno')
    zmk = args.zmk or (args.workspace / 'zmk' if args.workspace else ROOT / 'zmk')
    lvgl = args.lvgl or (args.workspace / 'modules/lib/gui/lvgl' if args.workspace else ROOT / 'lvgl')
    for path in (engine / 'engine.h', zmk / 'app/src/keymap.c', lvgl / 'CMakeLists.txt'):
        if not path.is_file():
            parser.error(f'Missing pinned dependency: {path}')
    build = ROOT / 'build/native-tests'
    build.mkdir(parents=True, exist_ok=True)

    def run(command):
        print('+ ' + ' '.join(map(str, command)), flush=True)
        subprocess.run(list(map(str, command)), cwd=ROOT, check=True, env={**os.environ, "STENO_SQUID_TEST_ZEPHYR": str(zmk.parent / "zephyr")})

    # Discover Python tests in the maintained test directories.
    special = {'test_practice_menu.py': [engine], 'test_practice_entry.py': [zmk / 'app']}
    for directory in ('tools/tests', 'tools/dictionaries', 'tools/dict_compiler', 'tools/manager'):
        for test in sorted((ROOT / directory).glob('test_*.py')):
            run([sys.executable, test, *special.get(test.name, [])])
    for language, flags in [('c', ['-std=c11']), ('c++', ['-std=c++17', '-x', 'c++'])]:
        binary = build / ('stroke-feed-' + language.replace('+', 'p'))
        run([*host_compiler(language), *flags, '-O2', '-Wall', '-Wextra', '-Werror',
             ROOT / 'tools/tests/test_stroke_feed.c', '-o', binary])
        run([binary])
    run([sys.executable, ROOT / 'tools/tests/run_repeat_translation.py', engine])
    with tempfile.TemporaryDirectory(prefix="stenosquid-replay-") as folder:
        run([sys.executable, ROOT / 'tools/bench/run_typing_replay.py', engine,
             '--virtual', '--out', Path(folder) / 'report'])

    run([sys.executable, ROOT / 'tools/tests/run_display_preview.py', lvgl])
    print('PASS: all native checks')


if __name__ == '__main__':
    main()
