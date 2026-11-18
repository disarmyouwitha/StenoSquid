#include "dictionary_flash_internal.h"
#include "dictionary_benchmark.h"
#include "flash_diagnostics.h"
#include "w25q_quad.h"
#include "xip_load.h"

#include <string.h>
#include <zephyr/drivers/pinctrl.h>
#include <zephyr/sys/crc.h>

#define JSC4_MAGIC 0x3443534aU

uint8_t lf_readback[CHUNK_BYTES] __aligned(4);
/* The last chip ID read, for the status command. */
uint8_t lf_last_jedec[3];
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
uint32_t steno_squid_benchmark_jedec(void) {
    return (uint32_t)lf_last_jedec[0] << 16 | (uint32_t)lf_last_jedec[1] << 8 | lf_last_jedec[2];
}
#endif
#if SERIAL_CONSOLE
bool lf_last_check_quick;
uint32_t lf_last_check_ms;
#endif

bool lf_all_verified(void) {
    for (size_t i = 0; i < ARRAY_SIZE(lf_images); i++) {
        if (!lf_images[i].verified) {
            return false;
        }
    }
    return true;
}

static bool start_qspi(nrfx_qspi_config_t *config) {
    if (!gpio_is_ready_dt(&lf_power) || gpio_pin_configure_dt(&lf_power, GPIO_OUTPUT_ACTIVE) != 0) {
        squid_flash_diag_log("QSPI start failed: flash power pin\n");
        return false;
    }
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
    atomic_set(&lf_powered, 1);
#endif
    k_msleep(CONFIG_ZMK_JAVELIN_STENO_FLASH_SETTLE_MS);
    if (lf_pins(PINCTRL_STATE_DEFAULT) != 0 ||
        nrfx_qspi_init(config, NULL, NULL) != NRFX_SUCCESS ||
        nrfx_qspi_activate(true) != NRFX_SUCCESS) {
        squid_flash_diag_log("QSPI start failed: pins, init or activate\n");
        return false;
    }

    const nrf_qspi_cinstr_conf_t read_id = {
        .opcode = 0x9f, .length = NRF_QSPI_CINSTR_LEN_4B, .io2_level = true, .io3_level = true,
    };
    uint8_t id[3];
    if (nrfx_qspi_cinstr_xfer(&read_id, NULL, id) != NRFX_SUCCESS) {
        squid_flash_diag_log("QSPI start failed: JEDEC read\n");
        return false;
    }
    memcpy(lf_last_jedec, id, sizeof(lf_last_jedec));
    squid_flash_diag_log("Lapwing JEDEC ID: %02x %02x %02x\n", id[0], id[1], id[2]);
    return id[0] == 0xef && id[1] == 0x40 && id[2] == 0x18;
}

/* CRC of one base, a chunk at a time. Raw reads use the QSPI EasyDMA path.
 * Mapped reads go through the memory window one word at a time, each load
 * followed by a DSB (nRF52840 erratum 216). Yields after every chunk. Only a
 * raw read can fail; failed_at then holds the offset of the chunk that did.
 */
bool lf_image_crc(const struct base_image *image, bool mapped, uint32_t *crc_out,
                  uint32_t *failed_at) {
    const volatile uint32_t *window = (const volatile uint32_t *)(XIP_BASE + image->offset);
    uint32_t crc = 0;

    for (uint32_t offset = 0; offset < image->bytes; offset += CHUNK_BYTES) {
        const size_t size = MIN(CHUNK_BYTES, image->bytes - offset);

        if (mapped) {
            copy_xip_bytes(lf_readback, (const void *)(window + offset / sizeof(uint32_t)), size);
        } else if (nrfx_qspi_read(lf_readback, size, image->offset + offset) != NRFX_SUCCESS) {
            if (failed_at) {
                *failed_at = offset;
            }
            return false;
        }
        crc = crc32_ieee_update(crc, lf_readback, size);
        k_yield();
    }
    *crc_out = crc;
    return true;
}

bool lf_read_image_crc(const struct base_image *image) {
    uint32_t crc;

    return lf_image_crc(image, false, &crc, NULL) && crc == image->crc;
}

/* Four data lines for every read that follows, raw or mapped. */
static bool enable_quad_reads(nrfx_qspi_config_t *config) {
    if (w25q128_enable_volatile_quad() != NRFX_SUCCESS) {
        return false;
    }
    config->prot_if.readoc = NRF_QSPI_READOC_READ4O;
    return nrfx_qspi_reconfigure(config) == NRFX_SUCCESS &&
           nrfx_qspi_activate(true) == NRFX_SUCCESS;
}

bool lf_enable_verified_mapping(nrfx_qspi_config_t *config) {
    // Startup and provisioning have already checked each image once through DMA.
    // Native lookups exercise mapped reads without another full-image CRC pass.
    if (config->prot_if.readoc != NRF_QSPI_READOC_READ4O && !enable_quad_reads(config)) return false;
    if (!lapwing_verify_native_dictionary(XIP_BASE)) {
        return false;
    }
#if PLOVER_BYTES > 0
    if (!plover_verify_native_dictionary(XIP_BASE + PLOVER_FLASH_OFFSET)) {
        return false;
    }
#endif
#if EXTRAS_BYTES > 0
    if (!extras_verify_native_dictionary(XIP_BASE + EXTRAS_FLASH_OFFSET)) {
        return false;
    }
#endif

    // The personal-dictionary store serializes its QSPI operations with mapped lookups.
    lf_trusted = true;
    atomic_set(&lf_mapped_ready, 1);
    return true;
}

/* Set when the last prepare_flash failed before it could read the images:
 * the chip did not answer, or four-line reads did not switch on. Those can
 * pass on a second try. Blank or mismatched images cannot, so they are not
 * retried.
 */
static bool transient_fault;

static bool prepare_flash(nrfx_qspi_config_t *config, bool *mapping_failed, bool quick) {
    *mapping_failed = false;
    transient_fault = false;
    config->prot_if.readoc = NRF_QSPI_READOC_FASTREAD;
    if (!start_qspi(config)) {
        transient_fault = true;
        return false;
    }
    /* The raw CRC pass reads every byte of both bases. On one data line it
     * was most of the cold-boot wait, so switch to four lines first.
     */
    if (!enable_quad_reads(config)) {
        for (size_t i = 0; i < ARRAY_SIZE(lf_images); i++) {
            lf_images[i].verified = false;
        }
        *mapping_failed = true;
        transient_fault = true;
        return true;
    }
    for (size_t i = 0; i < ARRAY_SIZE(lf_images); i++) {
        struct base_image *image = &lf_images[i];
        uint32_t magic = 0;
        image->verified =
            nrfx_qspi_read(&magic, sizeof(magic), image->offset) == NRFX_SUCCESS &&
            magic == JSC4_MAGIC && (quick || lf_read_image_crc(image));
    }
    if (lf_all_verified()) {
        *mapping_failed = !lf_enable_verified_mapping(config);
    }
    return true;
}

#define BRING_UP_ATTEMPTS 3

/* Takes the rail down for half a second, so the next attempt starts from a
 * chip that was off. With the optional 1 uF and 100 k bleed resistor on the
 * Steno Squid, the rail takes about 0.3 s to fall below 0.3 V. Holds
 * flash_lock through its caller.
 */
static void power_cycle_flash(void) {
    const bool initialized = nrfx_qspi_init_check();

    if (initialized) {
        (void)squid_qspi_reactivate();
    }
    (void)lf_power_down_flash(initialized);
    squid_flash_diag_note(DIAG_POWER_CYCLE);
    k_msleep(500);
}

/* A start-up that fails before it reads the images is tried again, a few
 * times, with a power cycle between. Otherwise one glitch, such as a rail
 * still settling after a wake, would leave Javelin without dictionaries until
 * the next reboot.
 */
static bool prepare_flash_with_retry(nrfx_qspi_config_t *config, bool *mapping_failed,
                                     bool quick) {
    bool qspi_ok = false;

    for (int attempt = 1; attempt <= BRING_UP_ATTEMPTS; attempt++) {
        qspi_ok = prepare_flash(config, mapping_failed, quick);
        if (atomic_get(&lf_mapped_ready) || !transient_fault || attempt == BRING_UP_ATTEMPTS) {
            break;
        }
        squid_flash_diag_note(DIAG_RETRY);
        squid_flash_diag_log("External flash did not start (attempt %d of %d); trying again\n", attempt,
                 BRING_UP_ATTEMPTS);
        power_cycle_flash();
    }
    return qspi_ok;
}

/* Start-up checks the chip ID, image headers and native lookups. A full CRC
 * pass runs when no check is on record for these images, such as the first
 * boot after provisioning, and when the quick check fails.
 */
bool lf_bring_up_flash(nrfx_qspi_config_t *config, bool *mapping_failed) {
    const int64_t started = k_uptime_get();
    bool quick = lf_take_wake_trust();

    atomic_clear(&lf_checked);
    bool qspi_ok = prepare_flash_with_retry(config, mapping_failed, quick);

    if (quick && (!qspi_ok || *mapping_failed || !atomic_get(&lf_mapped_ready))) {
        squid_flash_diag_note(DIAG_QUICK_FALLBACK);
        squid_flash_diag_log("Quick flash check failed; verifying in full\n");
        quick = false;
        lf_trusted = false;
        if (nrfx_qspi_init_check()) {
            (void)squid_qspi_reactivate();
            nrfx_qspi_uninit();
        }
        qspi_ok = prepare_flash_with_retry(config, mapping_failed, false);
    }
    if (!quick) {
        if (atomic_get(&lf_mapped_ready)) {
            lf_store_trust();
        } else {
            lf_clear_trust();
        }
    }
    squid_flash_diag_note(quick ? DIAG_QUICK_CHECK : DIAG_FULL_CHECK);
    if (!atomic_get(&lf_mapped_ready)) {
        squid_flash_diag_note(DIAG_UNAVAILABLE);
    }
    squid_flash_diag_log("External flash %s after %s check in %d ms\n",
             atomic_get(&lf_mapped_ready) ? "mapped" : "unavailable", quick ? "quick" : "full",
             (int)(k_uptime_get() - started));
    lf_suspended = false;
#if IDLE_OFF
    lf_note_use();
    atomic_clear(&lf_idle_off);
#endif
#ifdef CONFIG_ZMK_JAVELIN_STENO_HID_PROVISIONING
    lf_provisioning_ready = qspi_ok;
#endif
    atomic_set(&lf_checked, 1);
    k_condvar_broadcast(&lf_restart_done);
    dictionary_flash_ready_changed();
#if SERIAL_CONSOLE
    lf_last_check_quick = quick;
    lf_last_check_ms = (uint32_t)(k_uptime_get() - started);
#endif
    return qspi_ok;
}
