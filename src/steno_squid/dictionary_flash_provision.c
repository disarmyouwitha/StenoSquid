#include "dictionary_flash_internal.h"
#include "dictionary_flash_provision.h"
#include "w25q_quad.h"
#include "split_status.h"

#include <string.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/base64.h>
#include <zephyr/sys/printk.h>

#define TRANSFER_TIMEOUT_MS 30000

static struct {
    struct base_image *image;
    uint32_t offset;
    uint32_t crc;
    int64_t deadline;
    bool repair;
} transfer;

static uint8_t incoming[CHUNK_BYTES] __aligned(4);



static bool erase_base(const struct base_image *image) {
    /* The image table is fixed. Every base starts on a sector boundary. */
    const uint32_t end = ROUND_UP(image->offset + image->bytes, SECTOR_BYTES);

    for (uint32_t address = image->offset; address < end; address += SECTOR_BYTES) {
        if (nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_4KB, address) != NRFX_SUCCESS ||
            squid_qspi_wait_ready(2000) != NRFX_SUCCESS) {
            return false;
        }
    }
    return true;
}

static const char *transfer_data(uint32_t offset, const char *encoded, size_t length) {
    const struct base_image *image = transfer.image;
    size_t decoded = 0;
    if (offset >= image->bytes || offset % CHUNK_BYTES || offset > transfer.offset ||
        base64_decode(incoming, sizeof(incoming), &decoded, (const uint8_t *)encoded, length) != 0 ||
        decoded != MIN(CHUNK_BYTES, image->bytes - offset)) return "ERR Invalid chunk";
    const size_t size = decoded;
    if (nrfx_qspi_read(lf_readback, size, image->offset + offset) != NRFX_SUCCESS) return "ERR Flash read failed";
    if (offset < transfer.offset) {
        // The ACK was lost: verify the old chunk without changing CRC or writing again.
        return memcmp(lf_readback, incoming, size) == 0 ? "OK" : "ERR Retry data differs";
    }
    for (size_t i = 0; i < size; ++i) {
        if ((lf_readback[i] & incoming[i]) != incoming[i])
            return "ERR Base region holds different data. No sectors were erased";
    }
    const uint32_t crc = crc32_ieee_update(transfer.crc, incoming, size);
    if (offset + size == image->bytes && crc != image->crc) return "ERR Image checksum mismatch";
    for (size_t i = 0; i < size; i += PAGE_BYTES) {
        const size_t page = MIN(PAGE_BYTES, size - i);
        if (memcmp(lf_readback + i, incoming + i, page) == 0) continue;
        if (nrfx_qspi_write(incoming + i, page, image->offset + offset + i) != NRFX_SUCCESS ||
            squid_qspi_wait_ready(2000) != NRFX_SUCCESS) return "ERR Flash write failed";
    }
    if (nrfx_qspi_read(lf_readback, size, image->offset + offset) != NRFX_SUCCESS ||
        memcmp(lf_readback, incoming, size)) return "ERR Flash readback mismatch";
    transfer.offset += size;
    transfer.crc = crc;
    return "OK";
}

/* Fixed image bounds, device-side NOR checks, page readback and final CRC.
 * The host checks the complete pinned file before starting any write. */
static void show_transfer(uint32_t timeout_ms) {
    steno_squid_upload_status(transfer.repair ? STENO_SQUID_MODE_REPAIR : STENO_SQUID_MODE_INSTALL,
                            timeout_ms);
}

static struct base_image *find_base(const char *name) {
    for (size_t i = 0; i < ARRAY_SIZE(lf_images); i++) {
        const size_t length = strlen(lf_images[i].name);
        if (strncmp(name, lf_images[i].name, length) == 0 &&
            strcmp(name + length, "\n") == 0) {
            return &lf_images[i];
        }
    }
    return NULL;
}

static const char *handle_command(const char *command, char *reply, size_t reply_size) {
    if (strcmp(command, "status\n") == 0) {
        const enum dictionary_flash_state state = dictionary_flash_status();
        const char *label = state == DICTIONARY_FLASH_READY ? "READY" :
                            state == DICTIONARY_FLASH_LOADING ? "LOADING" :
                            lf_provisioning_ready ? "INSTALL" : "ERROR";
        size_t used = snprintk(reply, reply_size, "2 %s", label);
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

    if (!lf_provisioning_ready || lf_suspended || lf_provisioning_recheck) {
        return "ERR Flash is unavailable for installation";
    }

    const bool repair = strncmp(command, "repair ", 7) == 0;
    if (repair || strncmp(command, "begin ", 6) == 0) {
        struct base_image *image = find_base(command + (repair ? 7 : 6));
        if (image && transfer.image == image && transfer.repair == repair &&
            k_uptime_get() < transfer.deadline) {
            snprintk(reply, reply_size, "OK %u", transfer.offset);
            return NULL;
        }
        if (!image || (!repair && (image->verified || atomic_get(&lf_mapped_ready))) ||
            !squid_qspi_reactivate()) {
            return "ERR Base is unknown, already verified, or unavailable";
        }
        if (image->verified) {
            /* A quick boot check can retain a stale verified flag. Require a
             * fresh raw CRC mismatch before taking a live image offline. */
            const bool mapped = atomic_clear(&lf_mapped_ready);
            uint32_t crc = 0;
            const bool read_ok = lf_image_crc(image, false, &crc, NULL);
            if (!read_ok || crc == image->crc || !lf_clear_trust()) {
                if (mapped) {
                    atomic_set(&lf_mapped_ready, 1);
                }
                return "ERR Base repair requires a readable checksum mismatch and writable settings";
            }
            lf_trusted = false;
            image->verified = false;
            dictionary_flash_ready_changed();
        }
        if (!lf_clear_trust()) return "ERR Could not clear the saved flash verification record";
        if (repair) {
            steno_squid_upload_status(STENO_SQUID_MODE_REPAIR, 600000);
            if (!erase_base(image)) { show_transfer(0); return "ERR Base erase failed. Rerun repair for this base"; }
        }
        transfer.image = image;
        transfer.offset = 0;
        transfer.crc = 0;
        transfer.repair = repair;
        transfer.deadline = k_uptime_get() + TRANSFER_TIMEOUT_MS;
        show_transfer(TRANSFER_TIMEOUT_MS);
        snprintk(reply, reply_size, "OK 0");
        return NULL;
    }
    if (atomic_get(&lf_mapped_ready)) {
        return "ERR Flash is unavailable for installation";
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
        show_transfer(TRANSFER_TIMEOUT_MS);
        const char *result = transfer_data(offset, hex, strlen(hex) - 1);
        if (strcmp(result, "OK") == 0) {
            transfer.deadline = k_uptime_get() + TRANSFER_TIMEOUT_MS;
            show_transfer(TRANSFER_TIMEOUT_MS);
            snprintk(reply, reply_size, "OK %u", transfer.offset);
            return NULL;
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
        if (transfer.offset != image->bytes || !lf_read_image_crc(image)) {
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
    if (!dictionary_flash_acquire()) {
        k_mutex_lock(&flash_lock, K_FOREVER);
    }
    const struct base_image *image = find_base(name);

    if (image && lf_provisioning_ready && !lf_suspended && (!transfer.image || k_uptime_get() >= transfer.deadline) &&
        squid_qspi_reactivate()) {
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
    /* Every command counts as use and wakes an idle flash. A blank chip can
     * wake successfully without a mapping; acquire then returns false, so
     * take the lock and let handle_command check raw provisioning readiness.
     */
    if (!dictionary_flash_acquire()) {
        k_mutex_lock(&flash_lock, K_FOREVER);
    }
    const char *result = handle_command(command, reply, reply_size);
    if (result) {
        snprintk(reply, reply_size, "%s", result);
    }
    k_mutex_unlock(&flash_lock);
}
