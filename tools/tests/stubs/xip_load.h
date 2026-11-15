#pragma once
// Host stand-in for src/steno_squid/xip_load.h: plain loads, no erratum barriers.
#include <stddef.h>
#include <stdint.h>

static inline uint32_t xip_load_word(const void *address) {
  return *static_cast<const volatile uint32_t *>(address);
}
static inline uint8_t xip_load_byte(const void *address) {
  return *static_cast<const volatile uint8_t *>(address);
}
static inline uint32_t xip_load_u24(const void *address) {
  const uint8_t *bytes = static_cast<const uint8_t *>(address);
  return bytes[0] | (bytes[1] << 8) | (bytes[2] << 16);
}
