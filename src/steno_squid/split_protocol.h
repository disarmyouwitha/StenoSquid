#pragma once
#include <stdint.h>
#include <stddef.h>
#define STENO_SQUID_STATUS_BEHAVIOR "zrst"
#define STENO_SQUID_STATUS_PROTOCOL 1
#define STENO_SQUID_HOST_STATUS_PROTOCOL 7
/* A renamed layer uses the high bit of param2 as its tag. The remaining
 * 63 bits hold nine 7-bit characters, first character in the lowest bits.
 */
#define STENO_SQUID_LAYER_NAME_PROTOCOL (1u << 31)
#define STENO_SQUID_LAYER_NAME_CHARS 9

static inline void steno_squid_layer_name_encode(const char *name, uint32_t *p1, uint32_t *p2) {
    uint64_t packed = 0;
    for (unsigned i = 0; i < STENO_SQUID_LAYER_NAME_CHARS && name[i]; ++i) {
        const uint8_t ch = name[i];
        packed |= (uint64_t)(ch >= 0x20 && ch <= 0x7e ? ch : '?') << (7 * i);
    }
    *p1 = (uint32_t)packed;
    *p2 = (uint32_t)(packed >> 32) | STENO_SQUID_LAYER_NAME_PROTOCOL;
}
static inline void steno_squid_layer_name_decode(uint32_t p1, uint32_t p2, char *name) {
    const uint64_t packed = (uint64_t)(p2 & ~STENO_SQUID_LAYER_NAME_PROTOCOL) << 32 | p1;
    for (unsigned i = 0; i < STENO_SQUID_LAYER_NAME_CHARS; ++i) {
        const uint8_t ch = (packed >> (7 * i)) & 0x7f;
        name[i] = ch >= 0x20 && ch <= 0x7e ? ch : 0;
    }
    name[STENO_SQUID_LAYER_NAME_CHARS] = 0;
}
