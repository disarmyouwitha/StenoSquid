#include "lapwing_flash_internal.h"

#include <zephyr/sys/printk.h>
#if IS_ENABLED(CONFIG_SETTINGS)
#include <zephyr/settings/settings.h>
#endif

/* One value that changes with any of the expected images. Two records are
 * tied to it, so a quick check is only taken for the images this firmware
 * expects:
 * - GPREGRET2 survives System OFF and resets, and clears when power is lost.
 *   Deep sleep leaves a marker, so the wake that follows can skip the full
 *   CRC passes.
 * - A full CRC pass over every base leaves a record in the keyboard's
 *   settings. While it is there, a cold boot runs the quick check too. The
 *   bases only change through provisioning, which removes the record
 *   before it writes.
 * Anything else verifies in full.
 */
#define IMAGES_HASH (LAPWING_CRC ^ LAPWING_BYTES ^ (PLOVER_CRC * 31U) ^ PLOVER_BYTES ^ \
                     (EXTRAS_CRC * 131U) ^ (EXTRAS_BYTES * 7U))
#define IMAGE_FINGERPRINT (IMAGES_HASH ? IMAGES_HASH : 0xa5a5a5a5U)

uint8_t lf_trust_marker(void) {
    uint32_t value = IMAGES_HASH;

    value ^= value >> 16;
    value ^= value >> 8;
    value &= 0xff;
    return value ? (uint8_t)value : 0xa5;
}

#define TRUST_SETTING "lapwing/verified"
static bool trust_invalidated;

#if IS_ENABLED(CONFIG_SETTINGS)
static int read_trust(const char *key, size_t len, settings_read_cb read_cb, void *cb_arg,
                      void *param) {
    ARG_UNUSED(key);
    uint32_t value = 0;

    if (len == sizeof(value) && read_cb(cb_arg, &value, sizeof(value)) == sizeof(value)) {
        *(uint32_t *)param = value;
    }
    return 0;
}

bool lf_stored_trust(void) {
    uint32_t value = 0;

    /* ZMK has set settings up by now. Before that, nothing is read and the
     * answer is no, which only costs a full check.
     */
    return !trust_invalidated && settings_load_subtree_direct(TRUST_SETTING, read_trust, &value) == 0 &&
           value == IMAGE_FINGERPRINT;
}

void lf_store_trust(void) {
    const uint32_t value = IMAGE_FINGERPRINT;
    trust_invalidated = false;

    if (!lf_stored_trust() && settings_save_one(TRUST_SETTING, &value, sizeof(value)) != 0) {
        printk("Could not record the verified dictionaries\n");
    }
}

bool lf_clear_trust(void) {
    trust_invalidated = true;
    lf_trusted = false;
    NRF_POWER->GPREGRET2 = 0;
    return settings_delete(TRUST_SETTING) == 0;
}
#else
bool lf_stored_trust(void) {
    return false;
}
void lf_store_trust(void) {
}
bool lf_clear_trust(void) {
    return true;
}
#endif

bool lf_take_wake_trust(void) {
    NRF_POWER->GPREGRET2 = 0;
    // The retained byte is only a hint; confirm the full image fingerprint.
    return !trust_invalidated && (lf_trusted || lf_stored_trust());
}

void lf_forget_wake_trust(void) {
    NRF_POWER->GPREGRET2 = 0;
}
void lf_leave_wake_trust(void) {
    NRF_POWER->GPREGRET2 = lf_trusted ? lf_trust_marker() : 0;
}
