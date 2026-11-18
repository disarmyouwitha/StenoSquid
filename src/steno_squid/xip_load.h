#ifndef STENO_SQUID_XIP_LOAD_H
#define STENO_SQUID_XIP_LOAD_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* Word-only XIP reads and a DSB per load: nRF52840 anomalies 118 and 216. */
static inline uint32_t xip_load_word(const void *address) {
    const uint32_t value = *(const volatile uint32_t *)address;
    __asm__ volatile("dsb" ::: "memory");
    return value;
}
static inline uint8_t xip_load_byte(const void *address) {
    const uintptr_t offset = (uintptr_t)address;
    return (uint8_t)(xip_load_word((const void *)(offset & ~(uintptr_t)3)) >> (8 * (offset & 3)));
}
static inline uint32_t xip_load_u24(const void *address) {
    const uint8_t *bytes = (const uint8_t *)address;
    return xip_load_byte(bytes) | ((uint32_t)xip_load_byte(bytes + 1) << 8) |
           ((uint32_t)xip_load_byte(bytes + 2) << 16);
}
static inline void copy_xip_bytes(void *destination, const void *address, size_t length) {
    uint8_t *out = (uint8_t *)destination;
    const uint8_t *in = (const uint8_t *)address;
    while (length && ((uintptr_t)in & 3)) {
        *out++ = xip_load_byte(in++);
        --length;
    }
    while (length >= 4) {
        const uint32_t word = xip_load_word(in);
        memcpy(out, &word, 4);
        out += 4; in += 4; length -= 4;
    }
    while (length--) *out++ = xip_load_byte(in++);
}
#endif
