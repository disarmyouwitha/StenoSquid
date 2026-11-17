#include "lapwing_flash_internal.h"
#include "diag.h"
#include "split_status.h"
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
#include "battery_log.h"
#endif

#if SERIAL_PROVISIONING
#include <string.h>
#include <zephyr/sys/crc.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/printk.h>

const struct device *const lf_console = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
static uint8_t incoming[CHUNK_BYTES] __aligned(4);
static const struct base_image *repairing;


static void reply(const struct base_image *image, const char *message, uint32_t value) {
    char line[64];

    snprintk(line, sizeof(line), "%s %s %u\r\n", image->name, message, value);
    for (const char *character = line; *character; character++) {
        uart_poll_out(lf_console, *character);
    }
}

static bool receive_chunk(size_t size) {
    const int64_t deadline = k_uptime_get() + 5000;
    size_t received = 0;

    while (received < size && k_uptime_get() < deadline) {
        if (uart_poll_in(lf_console, incoming + received) == 0) {
            received++;
        } else {
            k_msleep(1);
        }
    }
    return received == size;
}

static bool wait_until_ready(void) {
    const int64_t deadline = k_uptime_get() + 2000;
    nrfx_err_t rc;

    do {
        rc = nrfx_qspi_mem_busy_check();
        if (rc != NRFX_ERROR_BUSY) {
            return rc == NRFX_SUCCESS;
        }
        k_msleep(1);
    } while (k_uptime_get() < deadline);
    return false;
}

/* Hold flash_lock. Takes the dictionaries out of use, then erases every
 * sector of one base so it can be programmed again. Leaves the other base and
 * the personal dictionary alone. Safe to repeat after an interruption.
 */
static bool erase_image(struct base_image *image) {
    if (!lf_clear_trust()) {
        reply(image, "TRUST_CLEAR_ERROR", 0);
        return false;
    }
    atomic_clear(&lf_mapped_ready);
    lf_trusted = false;
    NRF_POWER->GPREGRET2 = 0;
    image->verified = false;
    if (!reactivate_after_xip()) {
        return false;
    }

    const uint32_t end = ROUND_UP(image->offset + image->bytes, SECTOR_BYTES);

    for (uint32_t address = image->offset; address < end; address += SECTOR_BYTES) {
        if (nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_4KB, address) != NRFX_SUCCESS ||
            !wait_until_ready()) {
            return false;
        }
        /* Report every sixteenth sector, so the host sees progress. */
        const uint32_t done = address + SECTOR_BYTES - image->offset;

        if (done % (16U * SECTOR_BYTES) == 0) {
            reply(image, "ERASE", done);
        }
    }
    return true;
}

/* Hold flash_lock. Reads every byte of one base and compares its CRC with the
 * one this firmware expects. Writes nothing and changes no state.
 */
static void verify_image(const struct base_image *image) {
    uint32_t crc = 0;
    uint32_t failed_at = 0;
    bool read_ok = reactivate_after_xip();
    /* The dictionaries are out of use while this reads. A Javelin key pressed
     * meanwhile is dropped at once, instead of blocking the system work queue
     * on the flash lock until the check ends.
     */
    const bool was_mapped = atomic_cas(&lf_mapped_ready, 1, 0);

    read_ok = read_ok && lf_image_crc(image, false, &crc, &failed_at);
    if (was_mapped) {
        atomic_set(&lf_mapped_ready, 1);
    }
    if (!read_ok) {
        reply(image, "VERIFY_READ_FAILED", failed_at);
    } else {
        reply(image, crc == image->crc ? "VERIFY_OK" : "VERIFY_MISMATCH", crc);
    }
}

static bool transfer_image(const struct base_image *image, bool program) {
    uint32_t crc = 0;

    reply(image, program ? "WRITE" : "PREFLIGHT", image->bytes);
    for (uint32_t offset = 0; offset < image->bytes; offset += CHUNK_BYTES) {
        const size_t size = MIN(CHUNK_BYTES, image->bytes - offset);

        if (!receive_chunk(size) || nrfx_qspi_read(lf_readback, size, image->offset + offset) != NRFX_SUCCESS) {
            reply(image, "TRANSFER_ERROR", offset);
            return false;
        }
        crc = crc32_ieee_update(crc, incoming, size);
        // NOR writes may clear bits. A conflicting image needs a deliberate erase.
        for (size_t index = 0; index < size; index++) {
            if ((lf_readback[index] & incoming[index]) != incoming[index]) {
                reply(image, "FLASH_CONFLICT", offset + index);
                return false;
            }
        }
        if (program) {
            for (size_t index = 0; index < size; index += PAGE_BYTES) {
                const size_t page = MIN(PAGE_BYTES, size - index);

                if (memcmp(lf_readback + index, incoming + index, page) != 0 &&
                    (nrfx_qspi_write(incoming + index, page, image->offset + offset + index) != NRFX_SUCCESS ||
                     !wait_until_ready())) {
                    reply(image, "PROGRAM_ERROR", offset + index);
                    return false;
                }
            }
            if (nrfx_qspi_read(lf_readback, size, image->offset + offset) != NRFX_SUCCESS ||
                memcmp(lf_readback, incoming, size) != 0) {
                reply(image, "READBACK_ERROR", offset);
                return false;
            }
        }
        reply(image, "ACK", offset + size);
    }
    if (crc != image->crc) {
        reply(image, "TRANSFER_CRC_ERROR", crc);
        return false;
    }
    return true;
}

/* Writes one image. The saved verification record goes first: from here the
 * flash holds an image the record no longer describes.
 */
static bool transfer_and_program(const struct base_image *image) {
    if (!lf_clear_trust()) {
        reply(image, "TRUST_CLEAR_ERROR", 0);
        return false;
    }
    lf_trusted = false;
    NRF_POWER->GPREGRET2 = 0;
    return transfer_image(image, false) && transfer_image(image, true);
}

static void handle_command(uint8_t command, nrfx_qspi_config_t *config, bool qspi_ok,
                           bool *mapping_failed) {
    for (size_t i = 0; i < ARRAY_SIZE(lf_images); i++) {
        struct base_image *image = &lf_images[i];
        if (command == image->info) {
            reply(image, "SIZE", image->bytes);
            reply(image, "CRC", image->crc);
            continue;
        }
        if (command == image->verify) {
            if (!qspi_ok) {
                reply(image, "QSPI_ERROR", 0);
            } else {
                steno_squid_upload_status(STENO_SQUID_MODE_VERIFY, 120000);
                verify_image(image);
                steno_squid_upload_status(STENO_SQUID_MODE_VERIFY, 0);
            }
            continue;
        }
        if (command == image->erase) {
            if (!qspi_ok) {
                reply(image, "QSPI_ERROR", 0);
            } else {
                steno_squid_upload_status(STENO_SQUID_MODE_REPAIR, 600000);
                if (erase_image(image)) {
                    repairing = image;
                    steno_squid_upload_status(STENO_SQUID_MODE_REPAIR, 30000);
                    diag_note(DIAG_ERASE);
                    *mapping_failed = false;
                    reply(image, "ERASED", image->bytes);
                } else {
                    repairing = NULL;
                    steno_squid_upload_status(STENO_SQUID_MODE_REPAIR, 0);
                    reply(image, "ERASE_ERROR", 0);
                }
            }
            lapwing_flash_ready_changed();
            continue;
        }
        if (command != image->command && command != image->query) {
            continue;
        }
        if (!qspi_ok || *mapping_failed) {
            reply(image, "QSPI_ERROR", 0);
        } else if (lapwing_mapped_address()) {
            reply(image, "MAPPED", image->bytes);
        } else if (image->verified) {
            reply(image, "VERIFIED", image->bytes);
        } else if (command == image->query) {
            reply(image, "UNPROVISIONED", image->bytes);
        } else {
            const enum steno_squid_mode mode =
                image == repairing ? STENO_SQUID_MODE_REPAIR : STENO_SQUID_MODE_INSTALL;
            repairing = NULL;
            steno_squid_upload_status(mode, 600000);
            if (!transfer_and_program(image)) {
                steno_squid_upload_status(mode, 0);
                continue;
            }
            diag_note(DIAG_PROGRAM);
            image->verified = lf_read_image_crc(image);
            if (!image->verified) {
                reply(image, "READBACK_ERROR", 0);
            } else if (lf_all_verified()) {
                *mapping_failed = !lf_enable_verified_mapping(config, false);
                if (!*mapping_failed) {
                    lf_store_trust();
                }
                reply(image, *mapping_failed ? "MAPPING_ERROR" : "MAPPED", image->bytes);
                lapwing_flash_ready_changed();
            } else {
                reply(image, "VERIFIED", image->bytes);
            }
            steno_squid_upload_status(mode, 0);
        }
    }
}

bool lf_serial_poll(nrfx_qspi_config_t *config, bool *qspi_ok, bool *mapping_failed) {
    uint8_t command;

    if (uart_poll_in(lf_console, &command) != 0) {
        return false;
    }
    if (command == 'W') {
        reply(&lf_images[0], lf_last_check_quick ? "CHECK_QUICK" : "CHECK_FULL",
              lf_last_check_ms);
        return true;
    }
#ifdef CONFIG_ZMK_JAVELIN_DIAG
    if (command == 'S') {
        lf_print_flash_status();
        return true;
    }
    if (command == 'D') {
        diag_dump_log(lf_console);
        return true;
    }
    if (command == 'Z') {
        diag_clear(lf_console);
        return true;
    }
#ifdef CONFIG_ZMK_SLEEP
    if (command == 'R' || command == 'T') {
        k_mutex_lock(&flash_lock, K_FOREVER);
        lf_run_bench(command, config, qspi_ok, mapping_failed);
        k_mutex_unlock(&flash_lock);
        return true;
    }
#endif
#endif
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
    if (command == 'B') {
        steno_squid_battery_log_export(lf_console);
        return true;
    }
#endif
#ifdef CONFIG_ZMK_SLEEP
    k_mutex_lock(&flash_lock, K_FOREVER);
#if IDLE_OFF
    /* A provisioning command needs the flash, so end an idle power-down. */
    if (lf_suspended && atomic_get(&lf_idle_off)) {
        atomic_clear(&lf_restart_requested);
        *qspi_ok = lf_bring_up_flash(config, mapping_failed);
        lf_suspended = false;
        atomic_clear(&lf_idle_off);
    }
    lf_note_use();
#endif
    if (!lf_suspended) {
        handle_command(command, config, *qspi_ok, mapping_failed);
    }
    k_mutex_unlock(&flash_lock);
#else
    handle_command(command, config, *qspi_ok, mapping_failed);
#endif
    return false;
}

#endif /* SERIAL_PROVISIONING */
