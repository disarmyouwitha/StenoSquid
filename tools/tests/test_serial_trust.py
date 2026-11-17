#!/usr/bin/env python3
"""Fault-inject trust invalidation in the real diagnostic write helpers."""
from pathlib import Path
import subprocess
import tempfile
from test_base_provision import STUB, SOURCE


def function(source, name):
    start = source.index('static bool ' + name + '(')
    end = source.index('\n}', start) + 2
    return source[start:end]


def main():
    source = (SOURCE / 'lapwing_flash_serial.c').read_text(encoding="utf-8")
    helpers = '\n'.join(function(source, name) for name in
                        ('wait_until_ready', 'erase_image', 'transfer_and_program'))
    harness = r'''
static unsigned transfers, failures;
static void reply(const struct base_image *image, const char *text, uint32_t value) {
    assert(image == &lf_images[0] && !strcmp(text, "TRUST_CLEAR_ERROR") && !value);
    failures++;
}
static bool transfer_image(const struct base_image *image, bool write) {
    assert(image == &lf_images[0] && !lf_trusted && !NRF_POWER->GPREGRET2);
    transfers++;
    return true;
}
HELPERS
int main(void) {
    flash_lock = 1;
    lf_mapped_ready = lf_trusted = lf_images[0].verified = true;
    NRF_POWER->GPREGRET2 = 123;
    trust_can_clear = false;
    assert(!erase_image(&lf_images[0]));
    assert(!transfer_and_program(&lf_images[0]));
    assert(!erases && !writes && !transfers && failures == 2);
    assert(lf_mapped_ready && lf_trusted && lf_images[0].verified && NRF_POWER->GPREGRET2 == 123);
    trust_can_clear = true;
    assert(erase_image(&lf_images[0]));
    assert(erases == 2 && !lf_trusted && !lf_images[0].verified && !lf_mapped_ready);
    assert(transfer_and_program(&lf_images[0]) && transfers == 2);
}
'''.replace('HELPERS', helpers)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)
        (path / 'test.c').write_text(STUB.replace('#pragma once', '') + harness, encoding="utf-8")
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-function', '-Wno-unused-variable', '-Wno-unused-parameter',
                        str(path / 'test.c'), '-o', str(path / 'test')], check=True)
        subprocess.run([str(path / 'test')], check=True)
    print('PASS: failed trust invalidation prevents diagnostic erase and program')


if __name__ == '__main__':
    main()
