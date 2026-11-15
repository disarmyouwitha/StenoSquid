#pragma once
#include <stdint.h>

#define STENO_SQUID_WORD_ROUNDS 10u
#define STENO_SQUID_HAND_ROUNDS 50u
#define STENO_SQUID_SPEED_ROUNDS 10u
#define STENO_SQUID_SPEED_SECONDS 30u

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
