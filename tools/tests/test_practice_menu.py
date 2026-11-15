#!/usr/bin/env python3
"""Exercise the actual Practice input/state code with native scheduler doubles."""
import argparse
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def function(source, name):
    start = source.index(name)
    start = source.rfind('\n', 0, start) + 1
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


PREFIX = r'''
#include <cerrno>
#include <cassert>
#include <cstdio>
#include "steno_key_state.h"
#include "split_status.h"
#include "practice_model.h"
#include "practice_progress.h"
// The display receiver is C; allow its integer-to-enum calls in this C++ harness.
unsigned steno_squid_practice_prompt_count(unsigned drill) {
  return steno_squid_practice_prompt_count(static_cast<steno_squid_practice_drill>(drill));
}
unsigned steno_squid_practice_rounds(unsigned drill) {
  return steno_squid_practice_rounds(static_cast<steno_squid_practice_drill>(drill));
}
#define IS_ENABLED(option) option
#define CONFIG_ZMK_STENO_SQUID_STROKE_TAPE 0
#define K_MUTEX_DEFINE(n) int n
#define K_FOREVER 0
#define K_TIMEOUT_ABS_MS(n) n
#define K_WORK_DELAYABLE_DEFINE(n,f) int n
using atomic_val_t = uint32_t;
using atomic_t = atomic_val_t;
atomic_val_t atomic_get(const atomic_t *p) { return *p; }
atomic_val_t atomic_set(atomic_t *p, atomic_val_t v) {
  const auto old = *p;
  *p = v;
  return old;
}
void k_mutex_lock(int *, int) {}
void k_mutex_unlock(int *) {}
struct k_work {};
int64_t now = 0;
int64_t k_uptime_get() { return now; }
void k_work_cancel_delayable(int *) {}
void k_work_reschedule(int *, int64_t) {}
uint32_t sys_rand32_get() { return 3; }
static steno_squid_practice_model progress_model;
void steno_squid_words_begin() { steno_squid_model_begin(&progress_model, sys_rand32_get); }
bool steno_squid_trouble_begin() { return steno_squid_model_trouble_begin(&progress_model,sys_rand32_get); }
void steno_squid_hands_begin() { steno_squid_model_hands_begin(&progress_model); }
void steno_squid_hands_answer(unsigned round, bool correct, uint32_t ms) { steno_squid_model_hands_answer(&progress_model,round,correct,ms); }
unsigned steno_squid_words_prompt(unsigned round) { return progress_model.run.deck[round < 10 ? round : 9]; }
void steno_squid_words_answer(unsigned round, bool correct, uint32_t ms) { steno_squid_model_answer(&progress_model,round,correct,ms); }
void steno_squid_practice_finish() { steno_squid_model_finish(&progress_model); }
void steno_squid_practice_checkpoint() {}
void steno_squid_practice_end() { progress_model.run.active=false; }
struct k_spinlock {};
using k_spinlock_key_t = int;
int k_spin_lock(k_spinlock *) { return 0; }
void k_spin_unlock(k_spinlock *, int) {}
k_spinlock progress_lock;
uint64_t practice_progress;
extern "C" void steno_squid_split_status_refresh() {}
struct zmk_behavior_binding { uint32_t param1, param2; };
struct zmk_behavior_binding_event {};
#define ZMK_BEHAVIOR_OPAQUE 0
#define MODE_LAYER_NAME 100
atomic_t layer_name_low, layer_name_high, mode, host_disconnected;
atomic_val_t atomic_clear(atomic_t *p) { return atomic_set(p, 0); }
void clear_tape() {}
void queue_refresh() {}
int lookups;
bool answer_correct = true;
static bool practice_correct(const StenoStroke &) { ++lookups; return answer_correct; }
static void practice_refresh_dictionary();
'''

CASES = r'''
void tap(StenoKey key) {
  assert(practice_process_key(int(key), true));
  assert(practice_process_key(int(key), false));
}
int main() {
  practice.active = practice.menu = true;
  practice_publish();
  assert(practice_screen & STENO_SQUID_PRACTICE_MENU);
  tap(StenoKey::KL);  // Menu does not score strokes.
  assert(lookups == 0 && practice.menu);
  tap(StenoKey::STAR1);
  assert(practice.drill == STENO_SQUID_PRACTICE_TROUBLE && practice.menu);
  tap(StenoKey::A);
  assert(!practice.menu && progress_model.run.active && practice.drill==STENO_SQUID_PRACTICE_WORDS);
  assert(lookups==0); // Try Words starts a Words round without scoring the start key.
  tap(StenoKey::STAR1); // Back to Trouble.
  assert(practice.menu && practice.drill==STENO_SQUID_PRACTICE_TROUBLE);
  progress_model.record.words[42].misses=1;
  tap(StenoKey::A);
  assert(!practice.menu && practice_prompt()==42);
  tap(StenoKey::STAR3); // Trouble wraps forward to Words.
  assert(practice.drill == STENO_SQUID_PRACTICE_WORDS && practice.menu);
  tap(StenoKey::STAR4);
  assert(practice.drill == STENO_SQUID_PRACTICE_LEFT);
  tap(StenoKey::A);  // Start is consumed, not scored.
  assert(!practice.menu && lookups == 0 && practice.round == 0);
  assert(!(practice_screen & STENO_SQUID_PRACTICE_MENU));
  tap(StenoKey::KL);
  assert(lookups == 1 && practice.feedback == STENO_SQUID_PRACTICE_CORRECT);
  tap(StenoKey::KL);  // A chord during feedback is ignored.
  assert(lookups == 1);
  now = 250;
  practice_advance(nullptr);
  assert(practice.round == 1 && practice.feedback == STENO_SQUID_PRACTICE_PROMPT);
  tap(StenoKey::STAR3);  // Switching during a run returns to selection.
  assert(practice.menu && practice.round == 0 && practice.streak == 0);
  // A multi-key chord containing A must not accidentally start a run.
  practice_process_key(int(StenoKey::A), true);
  practice_process_key(int(StenoKey::KL), true);
  practice_process_key(int(StenoKey::A), false);
  practice_process_key(int(StenoKey::KL), false);
  assert(practice.menu && lookups == 1);
  for (auto key : {StenoKey::O, StenoKey::E, StenoKey::U}) {
    tap(key);
    assert(!practice.menu);
    practice.feedback = STENO_SQUID_PRACTICE_DONE;
    practice.round = steno_squid_practice_rounds(practice.drill);
    tap(key);  // Keep celebration until an explicit return to menu.
    assert(practice.menu && practice.round == 0);
  }
  // Menu packets, extended word IDs and malformed packets remain distinct.
  zmk_behavior_binding packet{practice_screen, STENO_SQUID_PRACTICE_PROTOCOL};
  assert(status_received(&packet, {}) == 0);
  const auto snapshot = practice_screen;
  zmk_behavior_binding host_packet{1, STENO_SQUID_HOST_STATUS_PROTOCOL};
  assert(status_received(&host_packet, {}) == 0 && host_disconnected);
  assert(practice_screen == snapshot);
  host_packet.param1 = 0;
  assert(status_received(&host_packet, {}) == 0 && !host_disconnected);
  packet.param1 |= 1u << STENO_SQUID_PRACTICE_ROUND_SHIFT;
  assert(status_received(&packet, {}) == -EINVAL);
  packet = {STENO_SQUID_PRACTICE_ACTIVE | steno_squid_practice_pack_prompt(99), STENO_SQUID_PRACTICE_PROTOCOL};
  assert(status_received(&packet, {}) == 0);
  assert(steno_squid_practice_prompt_value(practice_screen)==99);
  packet.param1 = STENO_SQUID_PRACTICE_ACTIVE | steno_squid_practice_pack_prompt(100);
  assert(status_received(&packet, {}) == -EINVAL);
  for(unsigned id=0;id<100;id++) {
    packet.param1=STENO_SQUID_PRACTICE_ACTIVE|steno_squid_practice_pack_prompt(id);
    assert(status_received(&packet,{})==0);
    assert(steno_squid_practice_prompt_value(practice_screen)==id);
  }
  const uint64_t stats=120 | (uint64_t(8)<<20) | (uint64_t(3)<<24) | (uint64_t(27)<<30) |
                       (uint64_t(104)<<39) | (uint64_t(99)<<46);
  packet={uint32_t(stats),STENO_SQUID_PRACTICE_PROGRESS_PROTOCOL | (uint32_t(stats>>32)<<8)};
  assert(status_received(&packet,{})==0 && practice_progress==stats);
  packet.param2 |= 0x20000000; // Valid no-trouble flag in payload bit 53.
  assert(status_received(&packet,{})==0 && steno_squid_practice_no_trouble(practice_progress));
  packet.param2 |= 0x40000000;
  assert(status_received(&packet,{})==-EINVAL);
  practice.active = false;
  assert(!practice_process_key(int(StenoKey::KL), true));
  puts("Practice menu, navigation, start, feedback and replay checks passed");
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('engine', type=Path)
    args = parser.parse_args()
    source = (ROOT / 'src/engine_init.cc').read_text()
    start = source.index('K_MUTEX_DEFINE(practice_mutex);')
    end = source.index('static const StenoDictionary *practice_fallback_dictionary()', start)
    code = PREFIX + source[start:end]
    code += '\nstatic void practice_refresh_dictionary() { practice.no_dictionary = false; }\n'
    code += function(source, 'static void practice_switch_drill(') + '\n'
    code += function(source, 'static bool practice_process_key(') + '\n'
    receiver = (ROOT / 'src/steno_squid/split_status_display.c').read_text()
    code += function(receiver, 'static int status_received(') + '\n'
    code += CASES
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / 'zephyr').mkdir()
        (directory / 'zephyr/toolchain.h').write_text('#define BUILD_ASSERT(x, ...) static_assert(x)\n')
        (directory / 'test.cc').write_text('#include <initializer_list>\n' + code)
        subprocess.run(['clang++', '-std=c++20', '-I' + str(directory),
                        '-I' + str(args.engine.resolve()), '-I' + str(ROOT / 'src/steno_squid'),
                        str(directory / 'test.cc'), str(args.engine / 'steno_key_state.cc'),
                        '-o', str(directory / 'test')], check=True)
        subprocess.run([str(directory / 'test')], check=True)


if __name__ == '__main__':
    main()
