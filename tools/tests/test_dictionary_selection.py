#!/usr/bin/env python3
"""Exercise the current host layout settings reader."""
from hostlib import compiler as host_compiler
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    harness = r'''
#include <cassert>
#include <cstdint>
#include <cerrno>
#include <cstring>
#include <sys/types.h>
#define CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS 1
#define CONFIG_ZMK_STENO_SQUID_STROKE_HINT 1
#define ARRAY_SIZE(a) (sizeof(a) / sizeof(*(a)))
using settings_read_cb = ssize_t (*)(void *, void *, size_t);
static constexpr const char *host_profiles[] = {"Mac U.S.", "Windows U.S."};
static uint8_t host_profile = 1;
static uint8_t stroke_hints = 1;
#include "engine_settings.h"
#include "stroke_hint_policy.h"
static ssize_t read(void *arg, void *out, size_t n) {
    memcpy(out, arg, n);
    return n;
}
static ssize_t fail(void *, void *, size_t) { return -EIO; }
int main() {
    uint8_t mac = 0, windows = 1, invalid = 2;
    assert(javelin_settings_set("host_profile", 1, read, &mac) == 0 && host_profile == mac);
    assert(javelin_settings_set("host_profile", 1, read, &windows) == 0 && host_profile == windows);
    assert(javelin_settings_set("host_profile", 0, read, &mac) == -EINVAL);
    assert(javelin_settings_set("host_profile", 1, read, &invalid) == -EINVAL);
    assert(javelin_settings_set("host_profile", 1, fail, &mac) == -EINVAL);
    assert(javelin_settings_set("dictionaries", 1, read, &mac) == -ENOENT);
    assert(host_profile == windows);
    assert(stroke_hints == 1);
    assert(javelin_settings_set("stroke_hints", 1, read, &mac) == 0 && stroke_hints == 0);
    assert(javelin_settings_set("stroke_hints", 1, read, &invalid) == -EINVAL && stroke_hints == 0);
    assert(javelin_settings_set("stroke_hints", 1, fail, &windows) == -EINVAL && stroke_hints == 0);
    assert(javelin_settings_set("stroke_hints", 1, read, &windows) == 0 && stroke_hints == 1);
    StenoSquidStrokeHintPolicy policy;
    assert(policy.Allow(1000));
    policy.Record(1000);
    assert(!policy.Allow(1245));
    assert(!policy.Allow(1164));
    assert(policy.Allow(1375));
    policy.Record(UINT32_MAX - 100);
    assert(!policy.Allow(144));
    assert(policy.Allow(274));
}
'''
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)
        (path / 'test.cc').write_text(harness, encoding="utf-8")
        subprocess.run([*host_compiler('c++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-I', str(ROOT / 'src/steno_squid'),
                        str(path / 'test.cc'), '-o', str(path / 'test')], check=True)
        subprocess.run([str(path / 'test')], check=True)
    print('PASS: host layout, hint settings, typing cadence and invalid settings')


if __name__ == '__main__':
    main()
