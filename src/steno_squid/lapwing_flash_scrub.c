#include "lapwing_flash_internal.h"
#include "xip_load.h"
#include <zephyr/sys/crc.h>

#define SCRUB_INTERVAL_MS (24 * 60 * 60 * 1000LL)
static int64_t scrub_at;
static size_t scrub_image;
static uint32_t scrub_offset, scrub_crc;
static bool scrub_scheduled;

/* Called after a full successful check, or before any base image is changed. */
void lf_scrub_verified(void) {
    scrub_image = 0;
    scrub_offset = scrub_crc = 0;
    scrub_at = k_uptime_get() + SCRUB_INTERVAL_MS;
    scrub_scheduled = true;
}
void lf_scrub_invalidated(void) {
    scrub_image = 0;
    scrub_offset = scrub_crc = 0;
    scrub_scheduled = false;
}
/* All calls hold flash_lock. Scrubbing never powers or holds the flash on. */
int32_t lf_scrub_wait_ms(void) {
    if (!atomic_get(&lf_mapped_ready) || !lf_trusted || lf_suspended) return -1;
    if (!scrub_scheduled) {
        scrub_at = k_uptime_get() + 60000;
        scrub_scheduled = true;
    }
    const int64_t remaining = scrub_at - k_uptime_get();
    return remaining > 0 ? (int32_t)remaining : 0;
}
void lf_scrub_step(void) {
    if (lf_scrub_wait_ms() != 0) return;
    const struct base_image *image = &lf_images[scrub_image];
    const uint32_t size = MIN(CHUNK_BYTES, image->bytes - scrub_offset);
    copy_xip_bytes(lf_readback, (const void *)(XIP_BASE + image->offset + scrub_offset), size);
    scrub_crc = crc32_ieee_update(scrub_crc, lf_readback, size);
    scrub_offset += size;
    if (scrub_offset == image->bytes) {
        if (scrub_crc != image->crc) {
            const size_t failed_image = scrub_image;
            (void)lf_clear_trust();
            lf_images[failed_image].verified = false;
            atomic_clear(&lf_mapped_ready);
            atomic_set(&lf_checked, 1);
            lapwing_flash_ready_changed();
            return;
        }
        scrub_crc = scrub_offset = 0;
        if (++scrub_image == IMAGE_COUNT) {
            lf_scrub_verified();
            return;
        }
    }
    scrub_at = k_uptime_get() + 8;
}
