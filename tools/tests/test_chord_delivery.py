#!/usr/bin/env python3
"""An interrupted chord cannot leak releases or consume the next fresh chord."""
from hostlib import compiler
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CODE = r'''
#include <cassert>
#include "chord_delivery.h"
int main() {
    StenoSquidChordDelivery guard;
    guard.Event(3, true);assert(guard.fresh_press);
    guard.Event(4, true);guard.Drop();assert(!guard.fresh_press && guard.interrupted);
    guard.Event(3, false);assert(!guard.Recover() && guard.interrupted);
    guard.Event(4, false);assert(!guard.Recover() && !guard.interrupted);
    guard.Event(5, true);assert(guard.fresh_press && !guard.interrupted);
    guard.Event(5, false);
    // Both press and release were dropped while the flash was busy.
    guard.Event(6, true);guard.Drop();guard.Event(6, false);guard.Drop();
    guard.Event(7, true);assert(guard.Recover() && !guard.interrupted);
    guard.Event(7, false);assert(!guard.held);
}
'''

def main():
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);source=root/'test.cc';source.write_text(CODE,encoding='utf-8')
        subprocess.run([*compiler(),'-std=c++17','-Wall','-Wextra','-Werror','-I',str(ROOT/'src/steno_squid'),str(source),'-o',str(root/'test')],check=True)
        subprocess.run([str(root/'test')],check=True)
    print('PASS: interrupted chord cancellation and next fresh chord recovery')

if __name__ == '__main__': main()
