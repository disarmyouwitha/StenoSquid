#pragma once

// Layer transition implementation; also exercised against pinned ZMK keymap code.
static void apply_layer(void) {
    const bool steno = steno_squid_active_layer() == JAVELIN_LAYER_STENO;
    ARG_UNUSED(steno);
    static bool known, previous_steno, previous_practice, previous_ready, previous_mapped;
    const bool ready = zmk_javelin_steno_ready();
    const bool mapped = dictionary_flash_mapped_address() != 0;
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
    const bool requested = steno && atomic_get(&practice_wanted);
#else
    const bool requested = false;
#endif
    if (known && steno == previous_steno && requested == previous_practice && ready == previous_ready && mapped == previous_mapped) {
        refresh_display();
        return;
    }
    known = true;
    previous_steno = steno;
    previous_practice = requested;
    previous_ready = ready;
    previous_mapped = mapped;

    /* Keep the dictionaries powered and ready while this layer is selected. */
    dictionary_flash_hold(steno);
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
    /* Leaving the steno layer ends practice. */
    if (!steno) {
        atomic_clear(&practice_wanted);
    }
    const bool want_practice = steno && atomic_get(&practice_wanted);
    /* Starting practice takes the engine lock, which waits for an idle flash
     * to power up. This runs on the thread that handles keys, so do not wait
     * here: dictionary_flash_hold() above has started the wake, and the ready
     * hook calls apply_layer() again once the dictionaries are mapped.
     */
    if (want_practice && !dictionary_flash_mapped_address()) {
        refresh_display();
        return;
    }
    steno_squid_practice_set_active(want_practice);
#endif
    refresh_display();
}

#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
void steno_squid_practice_toggle(void) {
    if (steno_squid_active_layer() == JAVELIN_LAYER_STENO) {
        atomic_set(&practice_wanted, !atomic_get(&practice_wanted));
    } else {
        /* The layer change clears the flag on its way, so set it afterwards. */
        /* Match &to: clear locked layers as well. A locked Plover layer
         * otherwise stays above Javelin and prevents Practice from starting.
         */
        zmk_keymap_layer_to(JAVELIN_LAYER_STENO, true);
        atomic_set(&practice_wanted, 1);
    }
    apply_layer();
}
#endif

