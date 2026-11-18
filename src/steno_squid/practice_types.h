#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <zephyr/toolchain.h>

#define STENO_SQUID_WORD_ROUNDS 10u
#define STENO_SQUID_HAND_ROUNDS 30u
#define STENO_SQUID_SPEED_ROUNDS 10u
#define STENO_SQUID_SPEED_SECONDS 30u
// Three building chords, followed by the complete chord with its word visible.
#define STENO_SQUID_BUILD_REPEAT_STAGE 3u

/* Practice snapshot bits, as packed by the left half and read by the display. */
#define STENO_SQUID_PRACTICE_ACTIVE (1u << 31)
#define STENO_SQUID_PRACTICE_TIMEOUT (1u << 30)
#define STENO_SQUID_PRACTICE_PARTIAL 1u
#define STENO_SQUID_PRACTICE_BUILD_SHIFT 1
#define STENO_SQUID_PRACTICE_BUILD_MASK (3u << STENO_SQUID_PRACTICE_BUILD_SHIFT)
#define STENO_SQUID_PRACTICE_FEEDBACK_SHIFT 4
#define STENO_SQUID_PRACTICE_ROUND_SHIFT 6
#define STENO_SQUID_PRACTICE_STREAK_SHIFT 12
#define STENO_SQUID_PRACTICE_BEST_SHIFT 18
#define STENO_SQUID_PRACTICE_COUNTER_MASK 63u
#define STENO_SQUID_PRACTICE_NO_DICTIONARY (1u << 24)
#define STENO_SQUID_PRACTICE_DRILL_SHIFT 25
#define STENO_SQUID_PRACTICE_DRILL_MASK (15u << STENO_SQUID_PRACTICE_DRILL_SHIFT)
#define STENO_SQUID_PRACTICE_MENU (1u << 29)
BUILD_ASSERT(STENO_SQUID_HAND_ROUNDS <= STENO_SQUID_PRACTICE_COUNTER_MASK,
             "A practice counter field cannot hold the round count");

enum steno_squid_practice_drill {
    STENO_SQUID_PRACTICE_WORDS,
    STENO_SQUID_PRACTICE_LEFT,
    STENO_SQUID_PRACTICE_RIGHT,
    STENO_SQUID_PRACTICE_COLUMNS,
    STENO_SQUID_PRACTICE_BUILD,
    STENO_SQUID_PRACTICE_MULTI,
    STENO_SQUID_PRACTICE_SPEED,
    STENO_SQUID_PRACTICE_TROUBLE,
    STENO_SQUID_PRACTICE_DRILL_COUNT,
};

BUILD_ASSERT(STENO_SQUID_PRACTICE_DRILL_COUNT <= 16, "The drill field has four bits");

static inline bool steno_squid_practice_word_drill(enum steno_squid_practice_drill drill) {
    return drill == STENO_SQUID_PRACTICE_WORDS || drill == STENO_SQUID_PRACTICE_TROUBLE || drill == STENO_SQUID_PRACTICE_SPEED;
}

/* Drills that show a word or outline from the word pools: the word, speed and
 * trouble drills, the multi-stroke drill and the build drill.
 */
static inline bool steno_squid_practice_is_word_like(enum steno_squid_practice_drill drill) {
    return steno_squid_practice_word_drill(drill) || drill == STENO_SQUID_PRACTICE_MULTI ||
           drill == STENO_SQUID_PRACTICE_BUILD;
}

enum steno_squid_practice_feedback {
    STENO_SQUID_PRACTICE_PROMPT,
    STENO_SQUID_PRACTICE_CORRECT,
    STENO_SQUID_PRACTICE_INCORRECT,
    STENO_SQUID_PRACTICE_DONE,
};

enum {
    PRACTICE_FIRST_TRY_SHIFT = 20, PRACTICE_ERRORS_SHIFT = 24,
    PRACTICE_AVERAGE_SHIFT = 30, PRACTICE_EARNED_SHIFT = 39,
    PRACTICE_SECONDS_SHIFT = 46, PRACTICE_NO_TROUBLE_SHIFT = 53,
};
static inline uint64_t steno_squid_practice_pack_progress(uint32_t xp, unsigned first_try,
        unsigned errors, unsigned average, unsigned earned, bool no_trouble) {
    return (uint64_t)xp | ((uint64_t)first_try << PRACTICE_FIRST_TRY_SHIFT) |
        ((uint64_t)errors << PRACTICE_ERRORS_SHIFT) |
        ((uint64_t)(average > 511 ? 511 : average) << PRACTICE_AVERAGE_SHIFT) |
        ((uint64_t)earned << PRACTICE_EARNED_SHIFT) |
        ((uint64_t)no_trouble << PRACTICE_NO_TROUBLE_SHIFT);
}

static inline unsigned steno_squid_practice_first_try(uint64_t progress) { return (progress >> PRACTICE_FIRST_TRY_SHIFT) & 15u; }
static inline unsigned steno_squid_practice_errors(uint64_t progress) { return (progress >> PRACTICE_ERRORS_SHIFT) & 63u; }
static inline unsigned steno_squid_practice_average(uint64_t progress) { return (progress >> PRACTICE_AVERAGE_SHIFT) & 511u; }
static inline unsigned steno_squid_practice_xp_earned(uint64_t progress) { return (progress >> PRACTICE_EARNED_SHIFT) & 127u; }
static inline bool steno_squid_practice_no_trouble(uint64_t progress) { return (progress >> PRACTICE_NO_TROUBLE_SHIFT) & 1u; }
static inline unsigned steno_squid_practice_seconds(uint64_t progress) { return (progress >> PRACTICE_SECONDS_SHIFT) & 31u; }
static inline uint32_t steno_squid_practice_xp(uint64_t progress) { return progress & 0xfffffu; }

/* The hand-drill prompts. The tables live inside the function so that only a
 * translation unit that calls it carries them.
 */
static inline const char *const *steno_squid_practice_prompts(enum steno_squid_practice_drill drill,
                                                              unsigned *count) {
    static const char *const left[] = {
        "S", "T", "K", "P", "W", "H", "R", "A", "O",
    };
    static const char *const right[] = {
        "-F", "-R", "-P", "-B", "-L", "-G", "-T", "-S", "-D", "-Z", "E", "U",
    };
    static const char *const columns[] = {
        "TK", "PW", "HR", "-FR", "-PB", "-LG", "-TS", "-DZ", "AO", "EU",
    };
    if (drill == STENO_SQUID_PRACTICE_LEFT) {
        *count = sizeof(left) / sizeof(left[0]);
        return left;
    }
    if (drill == STENO_SQUID_PRACTICE_RIGHT) {
        *count = sizeof(right) / sizeof(right[0]);
        return right;
    }
    if (drill == STENO_SQUID_PRACTICE_COLUMNS) {
        *count = sizeof(columns) / sizeof(columns[0]);
        return columns;
    }
    *count = 0;
    return 0;
}

static inline unsigned steno_squid_practice_prompt_count(enum steno_squid_practice_drill drill) {
    unsigned count;
    steno_squid_practice_prompts(drill, &count);
    return count;
}

static inline const char *steno_squid_practice_outline(enum steno_squid_practice_drill drill,
                                                       unsigned prompt) {
    unsigned count;
    const char *const *table = steno_squid_practice_prompts(drill, &count);
    return table ? table[prompt] : "?";
}

static inline unsigned steno_squid_practice_rounds(enum steno_squid_practice_drill drill) {
    return steno_squid_practice_is_word_like(drill)
               ? STENO_SQUID_WORD_ROUNDS : STENO_SQUID_HAND_ROUNDS;
}

static inline unsigned steno_squid_practice_feedback_value(uint32_t snapshot) {
    return (snapshot >> STENO_SQUID_PRACTICE_FEEDBACK_SHIFT) & 3u;
}

static inline unsigned steno_squid_practice_counter(uint32_t snapshot, unsigned shift) {
    return (snapshot >> shift) & STENO_SQUID_PRACTICE_COUNTER_MASK;
}

/* Level 1 starts at zero; each next level costs level * 100 XP. */
static inline unsigned steno_squid_pop_level(uint32_t xp, unsigned *into,
                                             unsigned *needed) {
  unsigned level = 1;
  while (xp >= level * 100u) {
    xp -= level * 100u;
    level++;
  }
  if (into)
    *into = xp;
  if (needed)
    *needed = level * 100u;
  return level;
}
