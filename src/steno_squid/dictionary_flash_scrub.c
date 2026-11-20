#include "dictionary_flash_internal.h"
#include "native_store_runtime.h"

/* All calls hold flash_lock. Scrubbing never powers or holds the flash on. */
int32_t lf_scrub_wait_ms(void) {
    return steno_squid_native_scrub_wait();
}

void lf_scrub_step(void) {
    steno_squid_native_scrub_step();
}
