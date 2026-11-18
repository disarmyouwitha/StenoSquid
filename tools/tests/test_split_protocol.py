#!/usr/bin/env python3
"""Round-trip the exact layer-name wire format as C and C++."""
from pathlib import Path
import subprocess
import tempfile
from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]
CODE = r'''
#include <assert.h>
#include <string.h>
#include "split_protocol.h"
int main(void) {
    uint32_t a, b;
    char name[STENO_SQUID_LAYER_NAME_CHARS + 1];
    steno_squid_layer_name_encode("PloverHID", &a, &b);
    assert(b & STENO_SQUID_LAYER_NAME_PROTOCOL);
    steno_squid_layer_name_decode(a, b, name);
    assert(!strcmp(name, "PloverHID"));
    steno_squid_layer_name_encode("abcdefghijk", &a, &b);
    steno_squid_layer_name_decode(a, b, name);
    assert(!strcmp(name, "abcdefghi"));
    steno_squid_layer_name_encode("a\001b", &a, &b);
    steno_squid_layer_name_decode(a, b, name);
    assert(!strcmp(name, "a?b"));
    steno_squid_layer_name_decode(1, 0, name);
    assert(!name[0]);
}
'''

def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / 'wire.c'
        source.write_text(CODE, encoding='utf-8')
        for language, flags in [('c', ['-std=c11']), ('c++', ['-std=c++17', '-x', 'c++'])]:
            exe = root / 'test'
            subprocess.run([*compiler(language), *flags, '-Wall', '-Wextra', '-Werror',
                            '-I', str(ROOT / 'src/steno_squid'), str(source), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)
    print('PASS: shared layer-name encoding, bounds and printable ASCII')


if __name__ == '__main__':
    main()
