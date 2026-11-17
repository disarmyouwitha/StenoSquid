#pragma once
/* A complete Practice display snapshot. Only the central owns word IDs and
 * pools. Text, feedback and results commit together after reassembly/checksum.
 * Each split command carries five bytes, an eight-bit sequence and an index.
 * At most fifteen fragments; the final one supplies the frame length.
 */
#include "practice_types.h"
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define STENO_SQUID_PRACTICE_VIEW_PROTOCOL 14u
#define STENO_SQUID_PRACTICE_TEXT_SIZE 16u
#define STENO_SQUID_PRACTICE_OUTLINE_SIZE 24u
#define STENO_SQUID_PRACTICE_FRAME_MAX 72u
#define STENO_SQUID_PRACTICE_FRAGMENT_BYTES 5u
#define STENO_SQUID_PRACTICE_FRAME_PARTS 15u

struct steno_squid_practice_view {
  uint32_t state;
  uint64_t progress;
  char prompt[STENO_SQUID_PRACTICE_TEXT_SIZE];
  char outline[STENO_SQUID_PRACTICE_OUTLINE_SIZE];
  char review[STENO_SQUID_PRACTICE_TEXT_SIZE];
};
struct steno_squid_practice_receiver {
  uint8_t bytes[STENO_SQUID_PRACTICE_FRAME_PARTS *
                STENO_SQUID_PRACTICE_FRAGMENT_BYTES];
  uint16_t received;
  uint8_t sequence, parts;
  bool seen;
  bool have_text;
  uint8_t text_sequence;
};
static inline uint32_t steno_squid_practice_checksum(const uint8_t *bytes,
                                                     unsigned length) {
  uint32_t hash = 2166136261u;
  for (unsigned i = 0; i < length; i++)
    hash = (hash ^ bytes[i]) * 16777619u;
  return hash;
}
static inline void steno_squid_practice_put(uint8_t *out, uint64_t value,
                                            unsigned count) {
  for (unsigned i = 0; i < count; i++)
    out[i] = value >> (i * 8);
}
static inline uint64_t steno_squid_practice_get(const uint8_t *bytes,
                                                unsigned count) {
  uint64_t value = 0;
  for (unsigned i = 0; i < count; i++)
    value |= (uint64_t)bytes[i] << (i * 8);
  return value;
}
static inline unsigned
steno_squid_practice_encode(const struct steno_squid_practice_view *view,
                            uint8_t *bytes) {
  memset(bytes, 0, STENO_SQUID_PRACTICE_FRAME_MAX);
  steno_squid_practice_put(bytes, view->state, 4);
  steno_squid_practice_put(bytes + 4, view->progress, 8);
  const char *texts[] = {view->prompt, view->outline, view->review};
  const unsigned sizes[] = {sizeof(view->prompt), sizeof(view->outline),
                            sizeof(view->review)};
  unsigned pos = 15;
  for (unsigned field = 0; field < 3; field++) {
    unsigned n = 0;
    while (n + 1 < sizes[field] && texts[field][n])
      n++;
    bytes[12 + field] = n;
    memcpy(bytes + pos, texts[field], n);
    pos += n;
  }
  steno_squid_practice_put(bytes + pos,
                           steno_squid_practice_checksum(bytes, pos), 4);
  return pos + 4;
}
/* A compact state update refers to the last complete text frame. If that
 * text frame was lost, the receiver ignores this update until a full resync. */
static inline uint8_t steno_squid_practice_text_key(const struct steno_squid_practice_view *view) {
  uint32_t hash = 2166136261u;
  const char *texts[] = {view->prompt, view->outline, view->review};
  for (unsigned field = 0; field < 3; ++field) {
    for (const char *p = texts[field]; *p; ++p) hash = (hash ^ (uint8_t)*p) * 16777619u;
    hash *= 16777619u;
  }
  return (uint8_t)(hash ^ (hash >> 8) ^ (hash >> 16) ^ (hash >> 24));
}
static inline unsigned steno_squid_practice_encode_state(const struct steno_squid_practice_view *view,
        uint8_t *bytes, uint8_t text_sequence) {
  memset(bytes, 0, STENO_SQUID_PRACTICE_FRAME_MAX);
  steno_squid_practice_put(bytes, view->state, 4);
  steno_squid_practice_put(bytes + 4, view->progress, 8);
  bytes[12] = 0xff;
  bytes[13] = text_sequence;
  bytes[14] = steno_squid_practice_text_key(view);
  steno_squid_practice_put(bytes + 15, steno_squid_practice_checksum(bytes, 15), 4);
  return 19;
}

static inline void
steno_squid_practice_fragment(const uint8_t *bytes, unsigned length,
                              uint8_t sequence, unsigned index,
                              uint32_t *param1, uint32_t *param2) {
  uint8_t chunk[5] = {0};
  for (unsigned i = 0; i < 5 && index * 5 + i < length; i++)
    chunk[i] = bytes[index * 5 + i];
  *param1 = steno_squid_practice_get(chunk, 4);
  *param2 = STENO_SQUID_PRACTICE_VIEW_PROTOCOL | ((uint32_t)sequence << 8) |
            (index << 16) | ((uint32_t)chunk[4] << 20) |
            ((index + 1 == (length + 4) / 5) ? 1u << 28 : 0);
}
/* 1 complete, 0 incomplete/stale, -1 malformed. The caller's view is unchanged
 * until every fragment from the same sequence passes validation. */
static inline int
steno_squid_practice_receive(struct steno_squid_practice_receiver *rx,
                             uint32_t p1, uint32_t p2,
                             struct steno_squid_practice_view *view) {
  if ((p2 & 0xffu) != STENO_SQUID_PRACTICE_VIEW_PROTOCOL || (p2 & 0xe0000000u))
    return -1;
  const uint8_t seq = p2 >> 8;
  const unsigned index = (p2 >> 16) & 15u;
  if (index >= STENO_SQUID_PRACTICE_FRAME_PARTS)
    return -1;
  if (rx->seen && seq != rx->sequence && (uint8_t)(seq - rx->sequence) >= 128)
    return 0;
  if (!rx->seen || seq != rx->sequence) {
    const bool have_text = rx->have_text;
    const uint8_t text_sequence = rx->text_sequence;
    memset(rx, 0, sizeof(*rx));
    rx->have_text = have_text;
    rx->text_sequence = text_sequence;
    rx->seen = true;
    rx->sequence = seq;
  }
  if (p2 & (1u << 28)) {
    if (rx->parts && rx->parts != index + 1)
      return -1;
    rx->parts = index + 1;
  }
  if (rx->parts && index >= rx->parts)
    return -1;
  steno_squid_practice_put(rx->bytes + index * 5, p1, 4);
  rx->bytes[index * 5 + 4] = (p2 >> 20) & 255;
  rx->received |= 1u << index;
  if (!rx->parts || rx->received != ((1u << rx->parts) - 1))
    return 0;
  const unsigned a = rx->bytes[12], b = rx->bytes[13], c = rx->bytes[14];
  const bool state_only = a == 0xff;
  const unsigned length = state_only ? 19 : 19 + a + b + c;
  if ((!state_only && (a >= sizeof(view->prompt) || b >= sizeof(view->outline) ||
      c >= sizeof(view->review))) || (length + 4) / 5 != rx->parts ||
      length > STENO_SQUID_PRACTICE_FRAME_MAX ||
      steno_squid_practice_get(rx->bytes + length - 4, 4) !=
          steno_squid_practice_checksum(rx->bytes, length - 4))
    return -1;
  struct steno_squid_practice_view next;
  if (state_only) {
    if (!rx->have_text || rx->text_sequence != b || steno_squid_practice_text_key(view) != c) return 0;
    next = *view;
  } else memset(&next, 0, sizeof(next));
  next.state = steno_squid_practice_get(rx->bytes, 4);
  next.progress = steno_squid_practice_get(rx->bytes + 4, 8);
  const unsigned drill = (next.state & STENO_SQUID_PRACTICE_DRILL_MASK) >>
                         STENO_SQUID_PRACTICE_DRILL_SHIFT;
  const unsigned round = steno_squid_practice_counter(
      next.state, STENO_SQUID_PRACTICE_ROUND_SHIFT);
  const unsigned total =
      steno_squid_practice_rounds((enum steno_squid_practice_drill)drill);
  const unsigned feedback = steno_squid_practice_feedback_value(next.state);
  const bool menu = next.state & STENO_SQUID_PRACTICE_MENU;
  const bool timeout = next.state & STENO_SQUID_PRACTICE_TIMEOUT;
  const bool partial = next.state & STENO_SQUID_PRACTICE_PARTIAL;
  const bool speed = drill == STENO_SQUID_PRACTICE_SPEED;
  const unsigned build_stage = (next.state & STENO_SQUID_PRACTICE_BUILD_MASK) >>
                               STENO_SQUID_PRACTICE_BUILD_SHIFT;
  if (!(next.state & STENO_SQUID_PRACTICE_ACTIVE) || (next.state & (1u << 3)) ||
      drill >= STENO_SQUID_PRACTICE_DRILL_COUNT || round > total ||
      build_stage > 2 ||
      (build_stage && (drill != STENO_SQUID_PRACTICE_BUILD || menu ||
                       feedback == STENO_SQUID_PRACTICE_DONE)) ||
      (menu && (round || feedback != STENO_SQUID_PRACTICE_PROMPT)) ||
      (timeout &&
       (!speed || feedback != STENO_SQUID_PRACTICE_DONE || round >= total)) ||
      (!timeout &&
       ((round == total) != (feedback == STENO_SQUID_PRACTICE_DONE))) ||
      (partial && (menu || drill != STENO_SQUID_PRACTICE_MULTI ||
                   feedback != STENO_SQUID_PRACTICE_PROMPT)) ||
      steno_squid_practice_seconds(next.progress) > STENO_SQUID_SPEED_SECONDS ||
      (steno_squid_practice_seconds(next.progress) &&
       (!speed || menu || feedback == STENO_SQUID_PRACTICE_DONE)) ||
      steno_squid_practice_counter(next.state,
                                   STENO_SQUID_PRACTICE_STREAK_SHIFT) > total ||
      steno_squid_practice_counter(next.state,
                                   STENO_SQUID_PRACTICE_BEST_SHIFT) > total ||
      steno_squid_practice_first_try(next.progress) > 10 ||
      steno_squid_practice_xp_earned(next.progress) > 120 ||
      (next.progress & ~((1ull << 54) - 1)))
    return -1;
  if (!state_only) {
    for (unsigned i = 15; i < length - 4; i++)
      if (rx->bytes[i] < 32 || rx->bytes[i] > 126) return -1;
    memcpy(next.prompt, rx->bytes + 15, a);
    memcpy(next.outline, rx->bytes + 15 + a, b);
    memcpy(next.review, rx->bytes + 15 + a + b, c);
  }
  if (!menu && feedback != STENO_SQUID_PRACTICE_DONE && (!next.prompt[0] || !next.outline[0])) return -1;
  if (!state_only) {
    rx->text_sequence = rx->sequence;
    rx->have_text = true;
  }
  *view = next;
  return 1;
}
#ifdef __cplusplus
extern "C" {
#endif
void steno_squid_practice_view_snapshot(struct steno_squid_practice_view *view);
#ifdef __cplusplus
}
#endif
