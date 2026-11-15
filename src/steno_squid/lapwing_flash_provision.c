#include "lapwing_flash_internal.h"
#include "lapwing_flash_provision.h"
#include "w25q_quad.h"
#include "split_status.h"

#include <string.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/printk.h>

#define TRANSFER_TIMEOUT_MS 30000

static struct {
    struct base_image *image;
    uint32_t offset;
    uint32_t crc;
    int64_t deadline;
    bool writing;
    bool repair;
} transfer;

static uint8_t incoming[CHUNK_BYTES] __aligned(4);

static bool wait_until_ready(void) {
    const int64_t deadline = k_uptime_get() + 2000;
    nrfx_err_t rc;

    while ((rc = nrfx_qspi_mem_busy_check()) == NRFX_ERROR_BUSY && k_uptime_get() < deadline) {
        k_msleep(1);
    }
    return rc == NRFX_SUCCESS;
}

static bool erase_base(const struct base_image *image) {
    /* The image table is fixed. Every base starts on a sector boundary. */
    const uint32_t end = ROUND_UP(image->offset + image->bytes, SECTOR_BYTES);

    for (uint32_t address = image->offset; address < end; address += SECTOR_BYTES) {
        if (nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_4KB, address) != NRFX_SUCCESS ||
            !wait_until_ready()) {
            return false;
        }
    }
    return true;
}

static const char *transfer_data(uint32_t offset, const char *hex, size_t length) {
    const struct base_image *image = transfer.image;
    const size_t size = MIN(CHUNK_BYTES, image->bytes - transfer.offset);
    if (offset != transfer.offset || size == 0 || length != size * 2 ||
        hex2bin(hex, length, incoming, sizeof(incoming)) != size) {
        return "ERR Invalid chunk";
    }

    /* Repair preflight checks the replacement image before erasing. Ordinary
     * installation also checks that every write is possible without erasing. */
    if (!transfer.repair || transfer.writing) {
        if (nrfx_qspi_read(lf_readback, size, image->offset + offset) != NRFX_SUCCESS) {
            return "ERR Flash read failed";
        }
        for (size_t i = 0; i < size; i++) {
            if ((lf_readback[i] & incoming[i]) != incoming[i]) {
                return "ERR Base region holds different data. No sectors were erased";
            }
        }
    }

    const uint32_t crc = crc32_ieee_update(transfer.crc, incoming, size);
    const bool complete = offset + size == image->bytes;
    if (complete && crc != image->crc) {
        return "ERR Image checksum mismatch";
    }

    if (transfer.writing) {
        for (size_t i = 0; i < size; i += PAGE_BYTES) {
            const size_t page = MIN(PAGE_BYTES, size - i);

            if (memcmp(lf_readback + i, incoming + i, page) == 0) {
                continue;
            }
            if (nrfx_qspi_write(incoming + i, page, image->offset + offset + i) != NRFX_SUCCESS ||
                !wait_until_ready()) {
                return "ERR Flash write failed";
            }
        }
        if (nrfx_qspi_read(lf_readback, size, image->offset + offset) != NRFX_SUCCESS ||
            memcmp(lf_readback, incoming, size) != 0) {
            return "ERR Flash readback mismatch";
        }
    }

    if (complete && !transfer.writing) {
        /* The complete file matched and is safe to write. Clear the saved
         * quick-check record before changing any bytes. */
        if (!lf_clear_trust()) {
            return "ERR Could not clear the saved flash verification record";
        }
        lf_trusted = false;
        NRF_POWER->GPREGRET2 = 0;
        if (transfer.repair && !erase_base(image)) {
            return "ERR Base erase failed. Rerun repair for this base";
        }
        transfer.writing = true;
        transfer.offset = 0;
        transfer.crc = 0;
    } else {
        transfer.offset += size;
        transfer.crc = crc;
    }
    return "OK";
}

/* Like the serial installer, use fixed image bounds, a no-write preflight,
 * page readback, and a full CRC check. Serialized with power and XIP access. */
static void show_transfer(uint32_t timeout_ms) {
    steno_squid_upload_status(transfer.repair ? STENO_SQUID_MODE_REPAIR : STENO_SQUID_MODE_INSTALL,
                            timeout_ms);
}

static const char *handle_command(const char *command, char *reply, size_t reply_size) {
    if (strcmp(command, "status\n") == 0) {
        const enum lapwing_flash_state state = lapwing_flash_status();
        const char *label = state == LAPWING_FLASH_READY ? "READY" :
                            state == LAPWING_FLASH_LOADING ? "LOADING" :
                            lf_provisioning_ready ? "INSTALL" : "ERROR";
        size_t used = snprintk(reply, reply_size, "1 %s", label);
        for (size_t i = 0; i < ARRAY_SIZE(lf_images) && used < reply_size; i++) {
            const struct base_image *image = &lf_images[i];
            used += snprintk(reply + used, reply_size - used, "\n%s %u %u %u",
                             image->name, image->bytes, image->crc, image->verified);
        }
        return NULL;
    }
    if (strcmp(command, "abort\n") == 0) {
        transfer.image = NULL;
        show_transfer(0);
        return "OK";
    }

    if (!lf_provisioning_ready || lf_suspended || lf_provisioning_recheck ||
        atomic_get(&lf_mapped_ready)) {
        return "ERR Flash is unavailable for installation";
    }

    const bool repair = strncmp(command, "repair ", 7) == 0;
    if (repair || strncmp(command, "begin ", 6) == 0) {
        const char *name = command + (repair ? 7 : 6);
        for (size_t i = 0; i < ARRAY_SIZE(lf_images); i++) {
            struct base_image *image = &lf_images[i];
            const size_t length = strlen(image->name);
            if (strncmp(name, image->name, length) == 0 &&
                strcmp(name + length, "\n") == 0 && !image->verified &&
                reactivate_after_xip()) {
                transfer.image = image;
                transfer.offset = 0;
                transfer.crc = 0;
                transfer.writing = false;
                transfer.repair = repair;
                transfer.deadline = k_uptime_get() + TRANSFER_TIMEOUT_MS;
                show_transfer(TRANSFER_TIMEOUT_MS);
                return "OK";
            }
        }
        return "ERR Base is unknown, already verified, or unavailable";
    }
    if (!transfer.image || k_uptime_get() >= transfer.deadline) {
        transfer.image = NULL;
        show_transfer(0);
        return "ERR No active transfer. Run the installer again";
    }
    if (strncmp(command, "data ", 5) == 0) {
        uint32_t offset = 0;
        const char *hex = command + 5;
        while (*hex >= '0' && *hex <= '9' && offset <= transfer.image->bytes) {
            offset = offset * 10 + (*hex++ - '0');
        }
        if (*hex != ' ' || hex == command + 5) {
            return "ERR Invalid chunk offset";
        }
        hex++;
        /* The final repair preflight chunk can erase hundreds of sectors. */
        show_transfer(transfer.repair && !transfer.writing &&
                              transfer.image->bytes - transfer.offset <= CHUNK_BYTES
                          ? 600000 : TRANSFER_TIMEOUT_MS);
        const char *result = transfer_data(offset, hex, strlen(hex) - 1);
        if (strcmp(result, "OK") == 0) {
            transfer.deadline = k_uptime_get() + TRANSFER_TIMEOUT_MS;
            show_transfer(TRANSFER_TIMEOUT_MS);
        } else {
            transfer.image = NULL;
            show_transfer(0);
        }
        return result;
    }
    if (strcmp(command, "commit\n") == 0) {
        struct base_image *image = transfer.image;
        transfer.image = NULL;
        show_transfer(120000);
        if (!transfer.writing || transfer.offset != image->bytes || !lf_read_image_crc(image)) {
            show_transfer(0);
            return "ERR Full image verification failed";
        }
        image->verified = true;
        if (lf_all_verified()) {
            atomic_clear(&lf_checked);
            lf_provisioning_recheck = true;
            lf_request_restart();
        } else {
            show_transfer(0);
        }
        return "OK";
    }
    return "ERR Unknown provisioning command";
}

static void verify_base(const char *name, char *reply, size_t reply_size) {
    /* Acquire wakes a mapped image from idle. An unprovisioned flash can
     * still be checked via raw reads once the controller is up. */
    if (!lapwing_flash_acquire()) {
        k_mutex_lock(&flash_lock, K_FOREVER);
    }
    const struct base_image *image = NULL;
    for (size_t i = 0; i < ARRAY_SIZE(lf_images); i++) {
        const size_t length = strlen(lf_images[i].name);
        if (strncmp(name, lf_images[i].name, length) == 0 &&
            strcmp(name + length, "\n") == 0) {
            image = &lf_images[i];
            break;
        }
    }

    if (image && lf_provisioning_ready && !lf_suspended && (!transfer.image || k_uptime_get() >= transfer.deadline) &&
        reactivate_after_xip()) {
        uint32_t crc = 0;
        const bool mapped = atomic_clear(&lf_mapped_ready);
        steno_squid_upload_status(STENO_SQUID_MODE_VERIFY, 120000);
        const bool read_ok = lf_image_crc(image, false, &crc, NULL);
        if (mapped) {
            atomic_set(&lf_mapped_ready, 1);
        }
        steno_squid_upload_status(STENO_SQUID_MODE_VERIFY, 0);
        if (read_ok) {
            snprintk(reply, reply_size, "%s %u", crc == image->crc ? "OK" : "MISMATCH", crc);
        } else {
            snprintk(reply, reply_size, "ERR Flash read failed");
        }
    } else {
        snprintk(reply, reply_size, "ERR Base is unknown or flash is busy or unavailable");
    }
    k_mutex_unlock(&flash_lock);
}

void lf_provision_command(const char *command, char *reply, size_t reply_size) {
    if (strncmp(command, "verify ", 7) == 0) {
        verify_base(command + 7, reply, reply_size);
        return;
    }
    k_mutex_lock(&flash_lock, K_FOREVER);
    const char *result = handle_command(command, reply, reply_size);
    if (result) {
        snprintk(reply, reply_size, "%s", result);
    }
    k_mutex_unlock(&flash_lock);
}
