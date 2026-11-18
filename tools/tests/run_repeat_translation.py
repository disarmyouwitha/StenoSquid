"""Run repeat-command regressions against the actual pinned Javelin engine.

Requires a native C++20 compiler. Builds a private copy of the pinned engine, which
is the javelin-steno fork named in config/west.yml.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hostlib import compiler as host_compiler
import shlex
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", type=Path)
    parser.add_argument("--build-dir", type=Path, default=Path("build/repeat-native"))
    parser.add_argument("--cxx", default=None)
    args = parser.parse_args()
    cxx = shlex.split(args.cxx) if args.cxx else host_compiler()
    repo = Path(__file__).resolve().parents[2]
    output = args.build_dir.resolve()
    engine = output / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    shutil.copytree(args.engine.resolve(), engine, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"))
    # The pinned engine is the fork with the firmware's changes already applied.
    # Its QSPI reader includes two firmware headers: use host stand-ins.
    stubs = repo / "tools/tests/stubs"
    objects_dir = output / "obj"
    objects_dir.mkdir(exist_ok=True)
    sources = list(engine.rglob("*.cc"))
    sources.append(stubs / "lapwing_stub.cc")
    sources.append(repo / "tools/tests/repeat_translation.cc")

    def compile_source(source):
        name = "_".join(source.relative_to(engine).parts) if source.is_relative_to(engine) else source.name
        obj = objects_dir / (name + ".o")
        command = [*cxx, "-std=c++20", "-O0", "-w", "-DRUN_TESTS=1",
                   "-DJAVELIN_USE_EMBEDDED_STENO=1", "-DJAVELIN_BOARD_CONFIG=<stddef.h>",
                   "-I" + str(stubs), "-I" + str(engine), "-c", str(source), "-o", str(obj)]
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(str(source) + "\n" + result.stderr)
        return str(obj)

    with ThreadPoolExecutor(max_workers=8) as pool:
        objects = list(pool.map(compile_source, sources))
    binary = output / "repeat_translation"
    subprocess.run([*cxx, *objects, "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
