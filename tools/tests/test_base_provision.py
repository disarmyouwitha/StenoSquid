#!/usr/bin/env python3
"""Exercise the wired provisioning state machine against an in-memory NOR chip."""
from hostlib import compiler as host_compiler
from pathlib import Path
import argparse
import os
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

#define SECTOR_BYTES 8
#define NRF_QSPI_ERASE_LEN_4KB 0
#define ROUND_UP(value, alignment) (((value) + (alignment) - 1) & ~((alignment) - 1))
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
enum dictionary_flash_state { DICTIONARY_FLASH_LOADING, DICTIONARY_FLASH_READY, DICTIONARY_FLASH_UNAVAILABLE };
struct base_image { const char *name; uint32_t offset, bytes, crc; bool verified; };
static struct base_image lf_images[] = {{"LAPWING", 0, 12, 0, false},
                                        {"PLOVER", 16, 4, 0, false}};
static uint8_t nor[32], lf_readback[CHUNK_BYTES];
static struct { uint32_t GPREGRET2; } power;
#define NRF_POWER (&power)
static bool lf_provisioning_ready = true, lf_provisioning_recheck, lf_suspended;
static bool lf_mapped_ready, lf_checked = true, lf_trusted;
static bool trust_can_clear = true;
static int flash_lock, writes, erases, restarts;
static int64_t now;

static inline int64_t k_uptime_get(void) { return now; }
static inline void k_msleep(int ms) { now += ms; }
static inline void k_mutex_lock(int *lock, int timeout) {
    assert(!*lock && timeout == K_FOREVER);
    *lock = 1;
}
static inline void k_mutex_unlock(int *lock) { assert(*lock); *lock = 0; }
static inline bool atomic_get(bool *value) { return *value; }
static inline bool atomic_clear(bool *value) {
    bool previous = *value;
    *value = false;
    return previous;
}
static inline void atomic_set(bool *value, bool next) { *value = next; }
static inline bool dictionary_flash_acquire(void) {
    if (!lf_mapped_ready) return false;
    k_mutex_lock(&flash_lock, K_FOREVER);
    return true;
}
static inline void dictionary_flash_ready_changed(void) {}
static inline bool squid_qspi_reactivate(void) { return true; }
static inline void lf_request_restart(void) { assert(flash_lock); restarts++; }
static inline bool lf_clear_trust(void) { lf_trusted = false; power.GPREGRET2 = 0; return trust_can_clear; }
static inline enum dictionary_flash_state dictionary_flash_status(void) {
    return lf_mapped_ready ? DICTIONARY_FLASH_READY
                           : lf_checked ? DICTIONARY_FLASH_UNAVAILABLE : DICTIONARY_FLASH_LOADING;
}
static inline nrfx_err_t squid_qspi_wait_ready(uint32_t timeout) { (void)timeout; return NRFX_SUCCESS; }
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
static inline int nrfx_qspi_erase(int kind, uint32_t offset) {
    assert(kind == NRF_QSPI_ERASE_LEN_4KB && flash_lock && !lf_trusted && !power.GPREGRET2);
    assert(offset % SECTOR_BYTES == 0 && offset + SECTOR_BYTES <= sizeof(nor));
    memset(nor + offset, 0xff, SECTOR_BYTES);
    erases++;
    return NRFX_SUCCESS;
}
static inline int nrfx_qspi_mem_busy_check(void) { return NRFX_SUCCESS; }
/* The stub needs only an incremental checksum for transfer-state tests. */
static inline uint32_t crc32_ieee_update(uint32_t crc, const uint8_t *data, size_t size) {
    crc = ~crc;
    for (size_t i = 0; i < size; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320U : 0);
    }
    return ~crc;
}
static inline bool lf_image_crc(const struct base_image *img, bool mapped,
                                uint32_t *crc, uint32_t *failed_at) {
    assert(flash_lock && !mapped && !failed_at && !lf_mapped_ready);
    *crc = crc32_ieee_update(0, nor + img->offset, img->bytes);
    return true;
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
#include "dictionary_flash_provision.c"
static void expect(const char *command,const char *prefix) {
    char reply[160];lf_provision_command(command,reply,sizeof(reply));
    if(strncmp(reply,prefix,strlen(prefix))) { fprintf(stderr,"%s => %s; wanted %s\n",command,reply,prefix);assert(false); }
}
int main(void) {
    const uint8_t lapwing[]={1,2,3,4,5,6,7,8,9,10,11,12},plover[]={13,14,15,16};
    const char *first="data 0 AQIDBAUGBwg=\n", *last="data 8 CQoLDA==\n";
    assert(crc32_ieee_update(0,(const uint8_t *)"123456789",9)==0xcbf43926U);
    lf_images[0].crc=crc32_ieee_update(0,lapwing,sizeof(lapwing));
    lf_images[1].crc=crc32_ieee_update(0,plover,sizeof(plover));
    memset(nor,0xff,sizeof(nor));nor[24]=0x55;
    expect("status\n","2 INSTALL");expect("verify LAPWING\n","MISMATCH ");
    expect("begin PERSONAL\n","ERR");
    trust_can_clear=false;expect("begin LAPWING\n","ERR Could not clear");assert(!writes && !erases);
    trust_can_clear=true;expect("begin LAPWING\n","OK 0");
    expect("data 4294967296 AQIDBAUGBwg=\n","ERR");assert(!writes);
    expect("begin LAPWING\n","OK 0");expect(first,"OK 8");assert(writes==2);
    expect(first,"OK 8");assert(writes==2); // Lost ACK: no duplicate write or CRC update.
    expect("begin LAPWING\n","OK 8"); // Resume the active transfer.
    now+=TRANSFER_TIMEOUT_MS;expect(last,"ERR No active transfer");
    expect("begin LAPWING\n","OK 0");expect(first,"OK 8");assert(writes==2);
    expect("data 8 /////w==\n","ERR Image checksum mismatch");assert(writes==2);
    expect("begin LAPWING\n","OK 0");expect(first,"OK 8");expect(last,"OK 12");
    assert(writes==3);expect("commit\n","OK");assert(lf_images[0].verified && !restarts);
    expect("begin LAPWING\n","ERR");
    nor[16]=0;expect("begin PLOVER\n","OK 0");expect("data 0 DQ4PEA==\n","ERR Base region holds different data");
    assert(!erases && !memcmp(nor,lapwing,sizeof(lapwing)));
    expect("repair PLOVER\n","OK 0");assert(erases==1 && nor[16]==0xff && nor[24]==0x55);
    expect("data 0 DQ4PEA==\n","OK 4");expect("commit\n","OK");
    assert(restarts==1 && lf_provisioning_recheck && !lf_checked);
    expect("status\n","2 LOADING");
    lf_mapped_ready=true;lf_provisioning_recheck=false;lf_checked=true;
    expect("verify LAPWING\n","OK ");expect("repair LAPWING\n","ERR Base repair requires");
    assert(lf_mapped_ready && erases==1); // Healthy repair is refused.
    nor[0]=0;expect("repair LAPWING\n","OK 0");
    assert(!lf_mapped_ready && !lf_images[0].verified && erases==3 && nor[24]==0x55);
    expect(first,"OK 8");expect(last,"OK 12");expect("commit\n","OK");
    assert(!memcmp(nor,lapwing,sizeof(lapwing)) && !memcmp(nor+16,plover,sizeof(plover)));
    lf_provisioning_recheck=false;lf_mapped_ready=false;lf_images[0].verified=false;
    expect("begin LAPWING\n","OK 0");expect("data 0 !!!!!!!!\n","ERR Invalid chunk");
    puts("PASS: single-pass base64 provisioning, duplicate ACK/restart, bounds, CRC, repair and healthy-base protection");
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zephyr', type=Path, default=Path(os.environ.get('STENO_SQUID_TEST_ZEPHYR', str(ROOT / 'zephyr'))))
    args = parser.parse_args()
    if not (args.zephyr / 'lib/utils/base64.c').is_file():
        parser.error('Pass --zephyr with the pinned Zephyr checkout (or use run_all.py)')
    with tempfile.TemporaryDirectory(prefix="base-provision-test-") as temp:
        root = Path(temp)
        for name in ("dictionary_flash_provision.c", "dictionary_flash_provision.h"):
            shutil.copy(SOURCE / name, root)
        (root / "dictionary_flash_internal.h").write_text(STUB, encoding="utf-8")
        (root / "split_status.h").write_text("""
enum steno_squid_mode {
    STENO_SQUID_MODE_INSTALL = 1, STENO_SQUID_MODE_REPAIR, STENO_SQUID_MODE_VERIFY
};
static enum steno_squid_mode upload_mode;
static inline void steno_squid_upload_status(enum steno_squid_mode mode, uint32_t timeout) {
    upload_mode = timeout ? mode : 0;
}
""", encoding="utf-8")
        for name in ("w25q_quad.h", "zephyr/sys/crc.h", "zephyr/sys/printk.h"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        base64_header = root / 'zephyr/sys/base64.h'
        shutil.copy(args.zephyr / 'include/zephyr/sys/base64.h', base64_header)
        (root / 'zephyr/types.h').write_text('#include <stdint.h>\n', encoding='utf-8')
        (root / "test.c").write_text(HARNESS, encoding="utf-8")
        subprocess.run([*host_compiler('c'), "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", temp,
                        str(root / "test.c"), str(args.zephyr / "lib/utils/base64.c"), "-o", str(root / "test")], check=True)
        subprocess.run([str(root / "test")], check=True)



if __name__ == "__main__":
    main()
