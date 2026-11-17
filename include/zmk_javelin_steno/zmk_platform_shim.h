#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

int zmk_javelin_emit_keycode(uint16_t usage_page, uint32_t keycode, bool pressed);
void zmk_javelin_steno_init(void);
void zmk_javelin_steno_process_key(int steno_key_index, bool is_press);

#ifdef __cplusplus
}

#endif
