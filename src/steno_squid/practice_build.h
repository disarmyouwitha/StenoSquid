#pragma once

/* Each ladder adds physical keys; the final outline is a verified Lapwing
 * word. Intermediate chords are raw key targets, not translations. */
#define STENO_SQUID_BUILD_COUNT 10u
#define STENO_SQUID_BUILD_STEPS 3u
static const struct {
  const char *word;
  const char *strokes[3];
} steno_squid_build[] = {
    {"CAT", {"K", "KA", "KAT"}},    {"DOG", {"TK", "TKO", "TKOG"}},
    {"BAT", {"PW", "PWA", "PWAT"}}, {"MAP", {"PH", "PHA", "PHAP"}},
    {"SUN", {"S", "SU", "SUPB"}},   {"PEN", {"P", "PE", "PEPB"}},
    {"TEN", {"T", "TE", "TEPB"}},   {"RED", {"R", "RE", "RED"}},
    {"TOP", {"T", "TO", "TOP"}},    {"FAN", {"TP", "TPA", "TPAPB"}},
};
