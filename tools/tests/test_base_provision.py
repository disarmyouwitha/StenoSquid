#!/usr/bin/env python3
"""Exercise the wired provisioning state machine against an in-memory NOR chip."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/steno_squid"

STUB = """
#pragma once
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define PAGE_BYTES 4
#define CHUNK_BYTES 8
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define __aligned(n) __attribute__((aligned(n)))
#define snprintk snprintf
#define K_FOREVER -1
#define NRFX_SUCCESS 0
#define NRFX_ERROR_BUSY 1
typedef int nrfx_err_t;
enum lapwing_flash_state { LAPWING_FLASH_LOADING, LAPWING_FLASH_READY, LAPWING_FLASH_UNAVAILABLE };
struct base_image { const char *name; uint32_t offset, bytes, crc; bool verified; };
static struct base_image lf_images[] = {{"LAPWING", 0, 12, 0, false},
                                        {"PLOVER", 16, 4, 0, false}};
static uint8_t nor[32], lf_readback[CHUNK_BYTES];
static struct { uint32_t GPREGRET2; } power;
#define NRF_POWER (&power)
static bool lf_provisioning_ready = true, lf_provisioning_recheck, lf_suspended;
static bool lf_mapped_ready, lf_checked = true, lf_trusted;
static bool trust_can_clear = true;
static int flash_lock, writes, restarts;
static int64_t now;

static inline int64_t k_uptime_get(void) { return now; }
static inline void k_msleep(int ms) { now += ms; }
static inline void k_mutex_lock(int *lock, int timeout) {
    assert(!*lock && timeout == K_FOREVER);
    *lock = 1;
}
static inline void k_mutex_unlock(int *lock) { assert(*lock); *lock = 0; }
static inline bool atomic_get(bool *value) { return *value; }
static inline void atomic_clear(bool *value) { *value = false; }
static inline bool reactivate_after_xip(void) { return true; }
static inline void lf_request_restart(void) { assert(flash_lock); restarts++; }
static inline bool lf_clear_trust(void) { return trust_can_clear; }
static inline enum lapwing_flash_state lapwing_flash_status(void) {
    return lf_mapped_ready ? LAPWING_FLASH_READY
                           : lf_checked ? LAPWING_FLASH_UNAVAILABLE : LAPWING_FLASH_LOADING;
}
static inline int nrfx_qspi_read(void *out, size_t size, uint32_t offset) {
    assert(flash_lock && offset + size <= sizeof(nor));
    memcpy(out, nor + offset, size);
    return NRFX_SUCCESS;
}
static inline int nrfx_qspi_write(const void *data, size_t size, uint32_t offset) {
    assert(flash_lock && !lf_trusted && power.GPREGRET2 == 0);
    assert(size <= PAGE_BYTES && offset % PAGE_BYTES == 0 && offset + size <= sizeof(nor));
    const uint8_t *bytes = data;
    for (size_t i = 0; i < size; i++) nor[offset + i] &= bytes[i];
    writes++;
    return NRFX_SUCCESS;
}
static inline int nrfx_qspi_mem_busy_check(void) { return NRFX_SUCCESS; }
/* The stub needs only an incremental checksum for transfer-state tests. */
static inline uint32_t crc32_ieee_update(uint32_t crc, const uint8_t *data, size_t size) {
    for (size_t i = 0; i < size; i++) crc = crc * 31 + data[i];
    return crc;
}
static inline bool lf_read_image_crc(const struct base_image *img) {
    return crc32_ieee_update(0, nor + img->offset, img->bytes) == img->crc;
}
static inline bool lf_all_verified(void) {
    return lf_images[0].verified && lf_images[1].verified;
}
static inline size_t hex2bin(const char *hex, size_t len, uint8_t *out, size_t max) {
    if (len % 2 || len / 2 > max) return 0;
    for (size_t i = 0; i < len / 2; i++) {
        unsigned int byte;
        if (sscanf(hex + 2 * i, "%2x", &byte) != 1) return 0;
        out[i] = byte;
    }
    return len / 2;
}
"""

HARNESS = r'''
#include "lapwing_flash_provision.c"

static void expect(const char *command, const char *prefix) {
    char reply[160];
    lf_provision_command(command, reply, sizeof(reply));
    if (strncmp(reply, prefix, strlen(prefix)) != 0) {
        fprintf(stderr, "%s returned %s, expected %s\n", command, reply, prefix);
        assert(false);
    }
}

int main(void) {
    const uint8_t lapwing[] = {1,2,3,4,5,6,7,8,9,10,11,12};
    const uint8_t plover[] = {13,14,15,16};
    const char *first = "data 0 0102030405060708\n";
    const char *last = "data 8 090a0b0c\n";
    lf_images[0].crc = crc32_ieee_update(0, lapwing, sizeof(lapwing));
    lf_images[1].crc = crc32_ieee_update(0, plover, sizeof(plover));
    memset(nor, 0xff, sizeof(nor));

    expect("status\n", "1 INSTALL\nLAPWING 12 ");
    expect("begin PERSONAL\n", "ERR");
    expect("begin LAPWING\n", "OK");
    expect("data 4294967296 0102030405060708\n", "ERR");
    expect(first, "OK");
    nor[11] = 0;
    expect(last, "ERR Base region holds different data");
    assert(writes == 0);
    nor[11] = 0xff;
    expect("begin LAPWING\n", "OK");
    expect(first, "OK");
    expect("data 8 ffffffff\n", "ERR Image checksum mismatch");
    assert(writes == 0);

    expect("begin LAPWING\n", "OK");
    expect(first, "OK");
    trust_can_clear = false;
    expect(last, "ERR Could not clear");
    assert(writes == 0);
    trust_can_clear = true;
    lf_trusted = true;
    power.GPREGRET2 = 42;

    expect("begin LAPWING\n", "OK");
    expect(first, "OK");
    expect(last, "OK");
    assert(writes == 0);
    expect(first, "OK");
    assert(writes == 2);
    now += TRANSFER_TIMEOUT_MS;
    expect(last, "ERR No active transfer");
    expect("begin LAPWING\n", "OK");
    expect(first, "OK");
    expect(last, "OK");
    expect(first, "OK");
    assert(writes == 2); // Matching pages are not reprogrammed.
    expect(last, "OK");
    expect("commit\n", "OK");
    assert(writes == 3 && restarts == 0 && lf_images[0].verified);
    expect("begin LAPWING\n", "ERR");

    expect("begin PLOVER\n", "OK");
    expect("data 0 0d0e0f10\n", "OK");
    expect("data 0 0d0e0f10\n", "OK");
    expect("commit\n", "OK");
    assert(restarts == 1 && lf_provisioning_recheck && !lf_checked);
    expect("status\n", "1 LOADING");
    for (size_t i = 12; i < sizeof(nor); i++)
        if (i < 16 || i >= 20) assert(nor[i] == 0xff);
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix="base-provision-test-") as temp:
        root = Path(temp)
        for name in ("lapwing_flash_provision.c", "lapwing_flash_provision.h"):
            shutil.copy(SOURCE / name, root)
        (root / "lapwing_flash_internal.h").write_text(STUB)
        for name in ("w25q_quad.h", "zephyr/sys/crc.h", "zephyr/sys/printk.h"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        (root / "test.c").write_text(HARNESS)
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", temp,
                        str(root / "test.c"), "-o", str(root / "test")], check=True)
        subprocess.run([str(root / "test")], check=True)
    print("PASS: base provisioning preflight, interrupted retry, image bounds, and final recheck")


if __name__ == "__main__":
    main()
