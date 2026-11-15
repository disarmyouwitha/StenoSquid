#pragma once

#include <zmk/keymap.h>

/* The highest active layer, as the layer ID the keymap gave it. ZMK Studio can
 * rename or reorder layers without changing it.
 */
static inline zmk_keymap_layer_id_t steno_squid_active_layer(void) {
    return zmk_keymap_layer_index_to_id(zmk_keymap_highest_layer_active());
}
