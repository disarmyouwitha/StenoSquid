#!/usr/bin/env python3
"""Exercise the actual settings reader, including rejection of obsolete shapes."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    source = (ROOT / 'src/engine_init.cc').read_text()
    start = source.index('static int javelin_settings_set(')
    end = source.index('\n}', start) + 2
    version = re.search(r'DICTIONARY_SELECTION_VERSION = (\d+)', source)[1]
    harness = r'''
#include <cassert>
#include <cstdint>
#include <cerrno>
#include <cstring>
#include <sys/types.h>
using settings_read_cb = ssize_t (*)(void *, void *, size_t);
static constexpr uint8_t DICTIONARY_SELECTION_VERSION = @VERSION@;
static uint8_t dictionary_selection;
READER
static ssize_t read(void *arg, void *out, size_t n) {
    memcpy(out, arg, n);
    return n;
}
static ssize_t fail(void *, void *, size_t) { return -EIO; }
int main() {
    for (uint8_t mask : {uint8_t(0), uint8_t(127)}) {
        uint8_t current[] = {@VERSION@, mask};
        assert(!javelin_settings_set("dictionaries", sizeof(current), read, current));
        assert(dictionary_selection == mask);
    }
    dictionary_selection = 3;
    uint8_t old[] = {1, 7}, unversioned[] = {7}, bad[] = {@VERSION@, 128};
    assert(javelin_settings_set("dictionaries", sizeof(old), read, old) == -EINVAL);
    assert(javelin_settings_set("dictionaries", sizeof(unversioned), read, unversioned) == -EINVAL);
    assert(javelin_settings_set("dictionaries", sizeof(bad), read, bad) == -EINVAL);
    assert(javelin_settings_set("dictionaries", 2, fail, bad) == -EINVAL);
    assert(javelin_settings_set("unknown", 2, read, bad) == -ENOENT);
    assert(dictionary_selection == 3);
}
'''.replace('@VERSION@', version).replace('READER', source[start:end])
    harness = '#include <initializer_list>\n' + harness
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)
        (path / 'test.cc').write_text(harness)
        subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        str(path / 'test.cc'), '-o', str(path / 'test')], check=True)
        subprocess.run([str(path / 'test')], check=True)
    print('PASS: current selection round trips; obsolete/corrupt records rejected')


if __name__ == '__main__':
    main()
