#include "../../src/steno_squid/practice_view.h"
#include "../../src/steno_squid/practice_words.h"
#include <assert.h>
#include <stdio.h>
static int deliver(struct steno_squid_practice_receiver *rx,
                   const uint8_t *bytes, unsigned n, uint8_t seq, unsigned part,
                   struct steno_squid_practice_view *out) {
  uint32_t a, b;
  steno_squid_practice_fragment(bytes, n, seq, part, &a, &b);
  return steno_squid_practice_receive(rx, a, b, out);
}
int main(void) {
  struct steno_squid_practice_view in = {.state = STENO_SQUID_PRACTICE_ACTIVE,
                                         .progress = 1234};
  strcpy(in.prompt, "HELLO");
  strcpy(in.outline, "HE/HROE");
  strcpy(in.review, "PEOPLE");
  uint8_t bytes[STENO_SQUID_PRACTICE_FRAME_MAX];
  unsigned n = steno_squid_practice_encode(&in, bytes), parts = (n + 4) / 5;
  struct steno_squid_practice_receiver rx = {0};
  struct steno_squid_practice_view out = {0};
  // Reverse-order fragments and duplicates commit exactly one coherent view.
  for (unsigned i = parts; i-- > 0;) {
    assert(deliver(&rx, bytes, n, 250, i, &out) == (i == 0));
    if (i)
      assert(out.state == 0);
  }
  assert(!memcmp(&in, &out, sizeof(in)));
  assert(deliver(&rx, bytes, n, 250, 0, &out) == 1);
  in.progress = 4567;
  strcpy(in.prompt, "NEW");
  n = steno_squid_practice_encode(&in, bytes);
  parts = (n + 4) / 5;
  assert(deliver(&rx, bytes, n, 251, 0, &out) == 0 && out.progress == 1234);
  assert(deliver(&rx, bytes, n, 250, 1, &out) ==
         0); // Old frame cannot fill a hole.
  for (unsigned i = 1; i < parts; i++)
    assert(deliver(&rx, bytes, n, 251, i, &out) == (i + 1 == parts));
  assert(out.progress == 4567 && !strcmp(out.prompt, "NEW"));
  // Sequence wraps, missing fragments, corruption, and same-sequence repair.
  for (unsigned i = 0; i < parts - 1; i++)
    assert(deliver(&rx, bytes, n, 0, i, &out) == 0);
  assert(deliver(&rx, bytes, n, 255, parts - 1, &out) == 0);
  bytes[15] ^= 1;
  assert(deliver(&rx, bytes, n, 0, parts - 1, &out) ==
         1); // Cached earlier chunk remains intact.
  assert(deliver(&rx, bytes, n, 1, 0, &out) == 0);
  for (unsigned i = 1; i < parts; i++) {
    const int result = deliver(&rx, bytes, n, 1, i, &out);
    assert(result == (i + 1 == parts ? -1 : 0));
  }
  assert(!strcmp(out.prompt, "NEW"));
  bytes[15] ^= 1;
  assert(deliver(&rx, bytes, n, 1, 3, &out) == 1);
  // Maximum text lengths, all drills, menu and results share the same codec.
  memset(in.prompt, 'W', sizeof(in.prompt) - 1);
  in.prompt[sizeof(in.prompt) - 1] = 0;
  memset(in.outline, 'K', sizeof(in.outline) - 1);
  in.outline[sizeof(in.outline) - 1] = 0;
  memset(in.review, 'R', sizeof(in.review) - 1);
  in.review[sizeof(in.review) - 1] = 0;
  for (unsigned drill = 0; drill < STENO_SQUID_PRACTICE_DRILL_COUNT; drill++) {
    for (unsigned feedback = 0; feedback < 4; feedback++) {
      const unsigned round = feedback == STENO_SQUID_PRACTICE_DONE
                                 ? steno_squid_practice_rounds(
                                       (enum steno_squid_practice_drill)drill)
                                 : 0;
      in.state = STENO_SQUID_PRACTICE_ACTIVE |
                 (drill << STENO_SQUID_PRACTICE_DRILL_SHIFT) |
                 (feedback << STENO_SQUID_PRACTICE_FEEDBACK_SHIFT) |
                 (round << STENO_SQUID_PRACTICE_ROUND_SHIFT);
      n = steno_squid_practice_encode(&in, bytes);
      assert(n == 72);
      memset(&rx, 0, sizeof(rx));
      for (unsigned i = 0; i < (n + 4) / 5; i++)
        assert(deliver(&rx, bytes, n, 1, i, &out) == (i + 1 == (n + 4) / 5));
      assert(!memcmp(&in, &out, sizeof(in)));
    }
  }
  for (unsigned id = 0; id < STENO_SQUID_WORD_COUNT; id++) {
    in.state = STENO_SQUID_PRACTICE_ACTIVE;
    snprintf(in.prompt, sizeof(in.prompt), "%s", steno_squid_words[id].word);
    snprintf(in.outline, sizeof(in.outline), "%s",
             steno_squid_words[id].outline);
    snprintf(in.review, sizeof(in.review), "%s", steno_squid_words[id].word);
    n = steno_squid_practice_encode(&in, bytes);
    memset(&rx, 0, sizeof(rx));
    for (unsigned i = 0; i < (n + 4) / 5; i++)
      assert(deliver(&rx, bytes, n, 1, i, &out) == (i + 1 == (n + 4) / 5));
    assert(!strcmp(out.prompt, steno_squid_words[id].word));
    assert(!strcmp(out.review, steno_squid_words[id].word));
  }
  uint32_t a, b;
  steno_squid_practice_fragment(bytes, n, 1, 0, &a, &b);
  assert(steno_squid_practice_receive(&rx, a, b | (1u << 31), &out) == -1);
  in.state =
      STENO_SQUID_PRACTICE_ACTIVE | 1; // Former ID bits are reserved now.
  n = steno_squid_practice_encode(&in, bytes);
  memset(&rx, 0, sizeof(rx));
  for (unsigned i = 0; i < (n + 4) / 5; i++)
    assert(deliver(&rx, bytes, n, 1, i, &out) ==
           (i + 1 == (n + 4) / 5 ? -1 : 0));
  puts("Practice text frames: atomic commit, loss/reorder/stale protection, "
       "wrap, corruption and all drills passed");
}
