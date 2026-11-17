#include "../../src/steno_squid/practice_build.h"
#include "../../src/steno_squid/practice_view.h"
#include "../../src/steno_squid/practice_words.h"
#include "../../src/steno_squid/stroke_feed.h"
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
  for (unsigned id = 0; id < STENO_SQUID_BUILD_COUNT; id++) {
    uint32_t previous = 0;
    for (unsigned stage = 0; stage < STENO_SQUID_BUILD_STEPS; stage++) {
      uint32_t mask;
      assert(steno_squid_stroke_from_text(steno_squid_build[id].strokes[stage],
                                          &mask));
      assert(mask != previous && (mask & previous) == previous);
      char shown[16];
      uint32_t formatted_mask;
      steno_squid_build_format(shown, sizeof(shown),
                               steno_squid_build[id].strokes[stage]);
      /* The extra dash is a visual hand boundary, not dictionary syntax.
       * Removing it must reproduce the exact canonical scoring outline. */
      char canonical[16]; unsigned ncanonical=0;
      for(unsigned k=0;shown[k];k++)if(shown[k]!='-')canonical[ncanonical++]=shown[k];
      canonical[ncanonical]=0;
      assert(!strcmp(canonical,steno_squid_build[id].strokes[stage]));
      assert(steno_squid_stroke_from_text(canonical, &formatted_mask) && formatted_mask == mask);
      previous = mask;
      in.state =
          STENO_SQUID_PRACTICE_ACTIVE |
          (STENO_SQUID_PRACTICE_BUILD << STENO_SQUID_PRACTICE_DRILL_SHIFT) |
          (stage << STENO_SQUID_PRACTICE_BUILD_SHIFT);
      n = steno_squid_practice_encode(&in, bytes);
      memset(&rx, 0, sizeof(rx));
      for (unsigned i = 0; i < (n + 4) / 5; i++)
        assert(deliver(&rx, bytes, n, 1, i, &out) == (i + 1 == (n + 4) / 5));
    }
  }
  // Speed can finish short of its goal only with an explicit timeout; partial
  // outlines are accepted only in an active Multi- prompt.
  for (unsigned scenario = 0; scenario < 5; scenario++) {
    in.state = STENO_SQUID_PRACTICE_ACTIVE;
    in.progress = 0;
    if (scenario < 2) {
      in.state |=
          (STENO_SQUID_PRACTICE_SPEED << STENO_SQUID_PRACTICE_DRILL_SHIFT);
      if (scenario == 0)
        in.progress = (uint64_t)30 << 46;
      else
        in.state |=
            STENO_SQUID_PRACTICE_TIMEOUT |
            (STENO_SQUID_PRACTICE_DONE << STENO_SQUID_PRACTICE_FEEDBACK_SHIFT) |
            (3 << STENO_SQUID_PRACTICE_ROUND_SHIFT);
    } else {
      in.state |=
          (STENO_SQUID_PRACTICE_MULTI << STENO_SQUID_PRACTICE_DRILL_SHIFT) |
          STENO_SQUID_PRACTICE_PARTIAL;
      if (scenario == 3)
        in.state |= STENO_SQUID_PRACTICE_MENU;
      if (scenario == 4)
        in.state =
            STENO_SQUID_PRACTICE_ACTIVE | 0 |
            (STENO_SQUID_PRACTICE_MULTI << STENO_SQUID_PRACTICE_DRILL_SHIFT) |
            (STENO_SQUID_PRACTICE_INCORRECT
             << STENO_SQUID_PRACTICE_FEEDBACK_SHIFT);
    }
    n = steno_squid_practice_encode(&in, bytes);
    memset(&rx, 0, sizeof(rx));
    for (unsigned i = 0; i < (n + 4) / 5; i++) {
      const int expected = i + 1 == (n + 4) / 5 ? (scenario == 3 ? -1 : 1) : 0;
      assert(deliver(&rx, bytes, n, 1, i, &out) == expected);
    }
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
  // State-only frames are smaller and cannot attach to missing/different text.
  in.state = STENO_SQUID_PRACTICE_ACTIVE;
  in.progress = 100;
  strcpy(in.prompt, "CAT");strcpy(in.outline, "KAT");in.review[0] = 0;
  memset(&rx, 0, sizeof(rx));memset(&out, 0, sizeof(out));
  n = steno_squid_practice_encode_state(&in, bytes, 20);
  assert(n == 19);
  for (unsigned i=0;i<4;i++) assert(deliver(&rx,bytes,n,21,i,&out)==0);
  assert(!out.state);
  n = steno_squid_practice_encode(&in, bytes);
  for (unsigned i=0;i<(n+4)/5;i++) assert(deliver(&rx,bytes,n,22,i,&out)==(i+1==(n+4)/5));
  in.progress = 101;
  n = steno_squid_practice_encode_state(&in,bytes,22);
  for(unsigned i=0;i<4;i++) assert(deliver(&rx,bytes,n,23,i,&out)==(i==3));
  assert(out.progress==101 && !strcmp(out.prompt,"CAT") && !strcmp(out.outline,"KAT"));
  n = steno_squid_practice_encode_state(&in,bytes,21);
  for(unsigned i=0;i<4;i++) assert(deliver(&rx,bytes,n,24,i,&out)==0);
  strcpy(in.prompt,"DOG");n=steno_squid_practice_encode_state(&in,bytes,22);
  for(unsigned i=0;i<4;i++) assert(deliver(&rx,bytes,n,25,i,&out)==0);
  assert(!strcmp(out.prompt,"CAT"));
  puts("Practice text frames: atomic commit, loss/reorder/stale protection, "
       "wrap, corruption and all drills passed");
}
