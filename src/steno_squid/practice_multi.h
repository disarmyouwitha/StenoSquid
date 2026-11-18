#pragma once
#include <zephyr/toolchain.h>

/* Two-stroke learning outlines verified against the pinned Lapwing base.
 * These drills earn XP without adding per-word records to the saved pool. */
#define STENO_SQUID_MULTI_COUNT 20u
static const struct {
  const char *word;
  const char *outline;
} steno_squid_multi[] = {
    {"HELLO", "HE/HROE"},      {"HAPPY", "HA/PEU"},
    {"AGAIN", "A/TKPWEPB"},    {"BEFORE", "PWE/TPOR"},
    {"AFTER", "AF/TER"},       {"REALLY", "RAOE/HREU"},
    {"LITTLE", "HREU/T-L"},    {"ANOTHER", "A/TPHOER"},
    {"FAMILY", "TPAPL/HREU"},  {"MOTHER", "PHO/THER"},
    {"FATHER", "TPAU/THER"},   {"PRACTICE", "PRABG/TEUS"},
    {"DINNER", "TKEU/TPHER"},  {"ANSWER", "APB/SER"},
    {"STORY", "STOR/KWREU"},   {"PERSON", "PER/SOPB"},
    {"ANIMAL", "APB/PHAL"},    {"REMEMBER", "RE/PHEPL"},
    {"PROBLEM", "PROB/HREPL"}, {"MUSIC", "PHAOU/SEUBG"},
};
BUILD_ASSERT(sizeof(steno_squid_multi) / sizeof(steno_squid_multi[0]) == STENO_SQUID_MULTI_COUNT);
