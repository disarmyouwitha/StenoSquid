#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <zephyr/toolchain.h>

#include "split_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif
void steno_squid_split_status_refresh(void);
void steno_squid_practice_set_active(bool active);
/* The practice key: on the steno layer it turns practice on or off. Anywhere
 * else it selects the steno layer with practice on.
 */
void steno_squid_practice_toggle(void);

struct steno_squid_tape;
/* Left half: send the last stroke, its translation and the typing speed to the
 * right display. See stroke_feed.h for the messages.
 */
void steno_squid_tape_publish(const struct steno_squid_tape *tape);
/* Wrap one key event given to the engine: the engine's stroke events are
 * caught between the two calls and published.
 */
void steno_squid_stroke_feed_begin(void);
void steno_squid_stroke_feed_end(void);
#ifdef __cplusplus
}
#endif

enum steno_squid_mode {
    STENO_SQUID_MODE_UNKNOWN,
    STENO_SQUID_MODE_TYPING,
    STENO_SQUID_MODE_SYMBOLS,
    STENO_SQUID_MODE_JAVELIN,
    STENO_SQUID_MODE_PLOVER,
    /* A steno or practice layer is selected, but the dictionaries are not usable. */
    STENO_SQUID_MODE_LOADING,
    STENO_SQUID_MODE_NO_DICTIONARY,
    STENO_SQUID_MODE_INSTALL,
    STENO_SQUID_MODE_REPAIR,
    STENO_SQUID_MODE_IMPORT,
    STENO_SQUID_MODE_VERIFY,
    STENO_SQUID_MODE_BUSY,
    STENO_SQUID_MODE_SAVING,
    STENO_SQUID_MODE_COUNT,
};


static inline bool steno_squid_upload_mode(unsigned mode) {
    return mode >= STENO_SQUID_MODE_INSTALL && mode <= STENO_SQUID_MODE_VERIFY;
}

/* A zero timeout clears the upload label. Timeout recovers from a USB disconnect. */
#if defined(CONFIG_ZMK_STENO_SQUID_SPLIT_STATUS)
#ifdef __cplusplus
extern "C"
#endif
void steno_squid_upload_status(enum steno_squid_mode mode, uint32_t timeout_ms);
#else
static inline void steno_squid_upload_status(enum steno_squid_mode mode, uint32_t timeout_ms) {
    (void)mode;
    (void)timeout_ms;
}
#endif

#if defined(CONFIG_ZMK_STENO_SQUID_NATIVE_STORE) && defined(CONFIG_ZMK_STENO_SQUID_SPLIT_STATUS)
#ifdef __cplusplus
extern "C"
#endif
void steno_squid_split_maintenance(bool active);
#else
static inline void steno_squid_split_maintenance(bool active) { (void)active; }
#endif
