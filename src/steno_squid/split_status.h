#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <zephyr/toolchain.h>

#define STENO_SQUID_STATUS_BEHAVIOR "zrst"
#define STENO_SQUID_STATUS_PROTOCOL 1
#define STENO_SQUID_HOST_STATUS_PROTOCOL 7
/* A renamed layer uses the high bit of param2 as its tag. The remaining
 * 63 bits hold nine 7-bit characters, first character in the lowest bits.
 */
#define STENO_SQUID_LAYER_NAME_PROTOCOL (1u << 31)
#define STENO_SQUID_LAYER_NAME_CHARS 9

#ifdef __cplusplus
extern "C" {
#endif
void steno_squid_split_status_refresh(void);
void steno_squid_practice_set_active(bool active);
/* The practice key: on the steno layer it turns practice on or off. Anywhere
 * else it selects the steno layer with practice on.
 */
void steno_squid_practice_toggle(void);
uint32_t steno_squid_practice_snapshot(void);

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
    STENO_SQUID_MODE_COUNT,
};
