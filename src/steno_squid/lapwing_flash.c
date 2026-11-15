#include "lapwing_flash_internal.h"
#include "diag.h"

#include <zephyr/sys/printk.h>

BUILD_ASSERT(LAPWING_BYTES % sizeof(uint32_t) == 0);
BUILD_ASSERT(LAPWING_BYTES <= BASE_DICTIONARY_LIMIT);
#if PLOVER_BYTES > 0
BUILD_ASSERT(LAPWING_BYTES <= PLOVER_FLASH_OFFSET);
BUILD_ASSERT(PLOVER_BYTES % sizeof(uint32_t) == 0);
BUILD_ASSERT(PLOVER_FLASH_OFFSET + PLOVER_BYTES <= BASE_DICTIONARY_LIMIT);
#endif
#if EXTRAS_BYTES > 0
BUILD_ASSERT(LAPWING_BYTES <= EXTRAS_FLASH_OFFSET);
BUILD_ASSERT(PLOVER_BYTES == 0 || PLOVER_FLASH_OFFSET + PLOVER_BYTES <= EXTRAS_FLASH_OFFSET);
BUILD_ASSERT(EXTRAS_BYTES % sizeof(uint32_t) == 0);
BUILD_ASSERT(EXTRAS_FLASH_OFFSET + EXTRAS_BYTES <= BASE_DICTIONARY_LIMIT);
#endif

struct base_image lf_images[IMAGE_COUNT] = {
    {"LAPWING", 0, LAPWING_BYTES, LAPWING_CRC, 'L', '?', 'E', 'V', 'I'},
#if PLOVER_BYTES > 0
    {"PLOVER", PLOVER_FLASH_OFFSET, PLOVER_BYTES, PLOVER_CRC, 'P', 'p', 'e', 'v', 'i'},
#endif
#if EXTRAS_BYTES > 0
    {"EXTRAS", EXTRAS_FLASH_OFFSET, EXTRAS_BYTES, EXTRAS_CRC, 'X', 'x', 'K', 'k', 'n'},
#endif
};

/* A repair erases a base in 4 KiB sectors, the last one whole. Each region
 * starts and ends on a sector boundary, so an erase stays inside its base.
 */
BUILD_ASSERT(PLOVER_FLASH_OFFSET % SECTOR_BYTES == 0);
BUILD_ASSERT(EXTRAS_FLASH_OFFSET % SECTOR_BYTES == 0);
BUILD_ASSERT(BASE_DICTIONARY_LIMIT % SECTOR_BYTES == 0);

atomic_t lf_mapped_ready;
atomic_t lf_checked;
bool lf_trusted;
#if IDLE_OFF
atomic_t lf_idle_off;
atomic_t lf_held;
atomic_t lf_last_use;

void lf_note_use(void) {
    atomic_set(&lf_last_use, (atomic_val_t)k_uptime_get_32());
}
#endif

/* Called whenever the dictionaries become usable or a bring-up gives up.
 * Runs on the flash worker with the flash lock held: only submit work.
 */
__weak void lapwing_flash_ready_changed(void) {
}

uintptr_t lapwing_mapped_address(void) {
    return atomic_get(&lf_mapped_ready) ? XIP_BASE : 0;
}

enum lapwing_flash_state lapwing_flash_status(void) {
#if IDLE_OFF
    /* An idle flash comes back on first use, so it still counts as ready. */
    if (atomic_get(&lf_idle_off)) {
        return LAPWING_FLASH_READY;
    }
#endif
    if (atomic_get(&lf_mapped_ready)) {
        return LAPWING_FLASH_READY;
    }
    return atomic_get(&lf_checked) ? LAPWING_FLASH_UNAVAILABLE : LAPWING_FLASH_LOADING;
}

void lapwing_flash_hold(bool hold) {
#if IDLE_OFF
    const bool was_held = atomic_set(&lf_held, hold) != 0;

    if (hold) {
        /* Start an idle flash without waiting for the first stroke. */
        lf_note_use();
        if (!atomic_get(&lf_mapped_ready) && atomic_get(&lf_idle_off)) {
            lf_request_restart();
        }
    } else if (was_held) {
        /* The idle period counts from leaving the layer. */
        lf_note_use();
    }
#else
    ARG_UNUSED(hold);
#endif
}

#ifdef CONFIG_ZMK_JAVELIN_STENO_HID_PROVISIONING
bool lf_provisioning_ready;
bool lf_provisioning_recheck;
#endif

static void lapwing_worker(void *a, void *b, void *c) {
    ARG_UNUSED(a);
    ARG_UNUSED(b);
    ARG_UNUSED(c);

    nrfx_qspi_config_t config = NRFX_QSPI_DEFAULT_CONFIG(
        NRF_QSPI_PIN_NOT_CONNECTED, NRF_QSPI_PIN_NOT_CONNECTED, NRF_QSPI_PIN_NOT_CONNECTED,
        NRF_QSPI_PIN_NOT_CONNECTED, NRF_QSPI_PIN_NOT_CONNECTED, NRF_QSPI_PIN_NOT_CONNECTED);
    config.skip_gpio_cfg = true;
    config.skip_psel_cfg = true;
    /* 32 MHz / divider. The enum is the divider minus one. */
    BUILD_ASSERT(NRF_QSPI_FREQ_DIV1 == 0 && NRF_QSPI_FREQ_DIV16 == 15);
    config.phy_if.sck_freq =
        (nrf_qspi_frequency_t)(CONFIG_ZMK_JAVELIN_STENO_QSPI_SCK_DIVIDER - 1);

    diag_note(DIAG_BOOT);
    diag_log("Flash worker started\n");

    bool mapping_failed = false;
#ifdef CONFIG_ZMK_SLEEP
    bool qspi_ok = false;
#else
    bool qspi_ok = lf_bring_up_flash(&config, &mapping_failed);
#endif

    for (;;) {
#ifdef CONFIG_ZMK_SLEEP
        if (atomic_get(&lf_restart_requested)) {
            k_mutex_lock(&flash_lock, K_FOREVER);
#ifdef CONFIG_ZMK_JAVELIN_STENO_HID_PROVISIONING
            if (lf_provisioning_recheck) {
                lf_provisioning_recheck = false;
                /* Each image already passed a raw CRC. Check the mapped bytes
                 * and native lookups before publishing the new mapping. */
                mapping_failed = !lf_enable_verified_mapping(&config, false);
                lf_provisioning_ready = qspi_ok && !mapping_failed;
                if (!mapping_failed) {
                    lf_store_trust();
                }
                atomic_set(&lf_checked, 1);
                lapwing_flash_ready_changed();
            }
#endif
            /* Several callers may ask for one wake. Only a powered-down flash
             * is brought up; a request that arrives late is already served.
             */
            if (atomic_cas(&lf_restart_requested, 1, 0) && lf_suspended) {
                qspi_ok = lf_bring_up_flash(&config, &mapping_failed);
#ifdef CONFIG_ZMK_JAVELIN_STENO_HID_PROVISIONING
                lf_provisioning_ready = qspi_ok && !mapping_failed;
#endif
                lf_suspended = false;
#if IDLE_OFF
                lf_note_use();
                atomic_clear(&lf_idle_off);
#endif
#if SERIAL_PROVISIONING
                diag_wake_ready(atomic_get(&lf_mapped_ready) != 0, lf_last_check_quick,
                                lf_last_check_ms);
#endif
            }
            k_mutex_unlock(&flash_lock);
        }
#if IDLE_OFF
        if (atomic_get(&lf_mapped_ready) && !atomic_get(&lf_held) &&
            k_uptime_get_32() - (uint32_t)atomic_get(&lf_last_use) >= IDLE_OFF_MS) {
            k_mutex_lock(&flash_lock, K_FOREVER);
            /* Recheck under the lock: a lookup may have just used the flash. */
            const bool initialized = nrfx_qspi_init_check();
            if (atomic_get(&lf_mapped_ready) && !lf_suspended && !atomic_get(&lf_held) &&
                k_uptime_get_32() - (uint32_t)atomic_get(&lf_last_use) >= IDLE_OFF_MS &&
                lf_flash_is_idle(initialized)) {
                if (lf_power_down_flash(initialized) == 0) {
                    atomic_set(&lf_idle_off, 1);
                    diag_note(DIAG_IDLE_DOWN);
                    diag_log("External flash powered down while idle\n");
                } else {
                    lf_request_restart();
                }
            }
            k_mutex_unlock(&flash_lock);
        }
#endif
#endif
#if SERIAL_PROVISIONING
        diag_tick();
        if (lf_serial_poll(&config, &qspi_ok, &mapping_failed)) {
            continue;
        }
        k_msleep(10);
#elif defined(CONFIG_ZMK_SLEEP)
        /* Nothing to poll. Sleep until something asks for the mapping again,
         * waking now and then only while an idle power-down is still due.
         */
        ARG_UNUSED(qspi_ok);
#if IDLE_OFF
        k_sem_take(&lf_restart_wake, atomic_get(&lf_mapped_ready)
                                      ? K_MSEC(MAX(IDLE_OFF_MS / 4U, 1000U)) : K_FOREVER);
#else
        k_sem_take(&lf_restart_wake, K_FOREVER);
#endif
#else
        ARG_UNUSED(qspi_ok);
        return;
#endif
    }
}

/* The stack leaves room for the settings read and write above. */
K_THREAD_DEFINE(lapwing_flash, 3072, lapwing_worker, NULL, NULL, NULL, 5, 0, 200);
