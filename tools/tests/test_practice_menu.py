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
#include "practice_types.h"
#include "practice_model.h"
#include "practice_progress.h"
#include "practice_view.h"
#include "stroke_feed.h"
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
uint64_t steno_squid_practice_progress() { return 0; }
unsigned steno_squid_practice_review_word() { return progress_model.run.review; }
void steno_squid_words_begin() { steno_squid_model_begin(&progress_model, sys_rand32_get); }
bool steno_squid_trouble_begin() { return steno_squid_model_trouble_begin(&progress_model,sys_rand32_get); }
void steno_squid_build_begin() { steno_squid_model_build_begin(&progress_model,sys_rand32_get); }
void steno_squid_multi_begin() { steno_squid_model_multi_begin(&progress_model,sys_rand32_get); }
void steno_squid_speed_timeout() { steno_squid_model_timeout(&progress_model); }
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
struct steno_squid_practice_view practice_view;
struct steno_squid_practice_receiver practice_receiver;
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
bool first_correct = true;
static bool practice_first_stroke_correct(const StenoStroke &) { return first_correct; }
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
  // Build-Up credits only complete ladders; mistakes retain the current step.
  practice.drill=STENO_SQUID_PRACTICE_BUILD;practice_switch_drill(0);tap(StenoKey::A);
  const unsigned build_xp=progress_model.record.xp;
  struct steno_squid_practice_view build_view;
  steno_squid_practice_view_snapshot(&build_view);
  assert(!strcmp(build_view.prompt,"K"));
  answer_correct=false;tap(StenoKey::KL);
  assert(practice.build_stage==0 && practice.round==0 && progress_model.record.xp==build_xp);
  answer_correct=true;tap(StenoKey::KL);
  assert(practice.feedback==STENO_SQUID_PRACTICE_CORRECT && progress_model.run.position==0);
  now+=250;practice_advance(nullptr);
  assert(practice.build_stage==1 && practice.round==0);
  steno_squid_practice_view_snapshot(&build_view);assert(!strcmp(build_view.prompt,"KA"));
  answer_correct=false;tap(StenoKey::KL);assert(practice.build_stage==1);
  answer_correct=true;tap(StenoKey::KL);now+=250;practice_advance(nullptr);
  assert(practice.build_stage==2 && progress_model.record.xp==build_xp);
  steno_squid_practice_view_snapshot(&build_view);assert(!strcmp(build_view.prompt,"KA-T"));
  tap(StenoKey::KL);
  assert(progress_model.record.xp==build_xp+2 && progress_model.run.position==1);
  steno_squid_practice_view_snapshot(&build_view);assert(!strcmp(build_view.prompt,"CAT"));
  now+=250;practice_advance(nullptr);assert(practice.round==0);
  now+=350;practice_advance(nullptr);assert(practice.round==1 && practice.build_stage==0);
  // Multi rejects a wrong first stroke immediately, then keeps a good prefix.
  practice.drill=STENO_SQUID_PRACTICE_MULTI;
  practice_switch_drill(0);tap(StenoKey::A);
  const unsigned before_multi=lookups;
  first_correct=false;tap(StenoKey::KL);
  assert(practice.multi_count==0 && lookups==before_multi && practice.feedback==STENO_SQUID_PRACTICE_INCORRECT);
  assert(progress_model.run.errors==1 && progress_model.record.xp>0);
  first_correct=true;tap(StenoKey::KL);
  assert(practice.multi_count==1 && lookups==before_multi && practice.round==0);
  assert(practice_screen&STENO_SQUID_PRACTICE_PARTIAL);
  struct steno_squid_practice_view partial;
  steno_squid_practice_view_snapshot(&partial);
  assert(!strcmp(partial.outline,steno_squid_multi[practice_prompt()].outline));
  answer_correct=false;tap(StenoKey::PL);
  assert(practice.multi_count==0 && lookups==before_multi+1 && practice.feedback==STENO_SQUID_PRACTICE_INCORRECT);
  assert(!(practice_screen&STENO_SQUID_PRACTICE_PARTIAL));
  assert(practice_screen&STENO_SQUID_PRACTICE_SECOND_ERROR);
  answer_correct=true;tap(StenoKey::KL);tap(StenoKey::PL);
  assert(practice.feedback==STENO_SQUID_PRACTICE_CORRECT);
  now+=250;practice_advance(nullptr);
  assert(practice.round==1 && practice.multi_count==0);
  tap(StenoKey::KL); // Switching clears the partial outline.
  tap(StenoKey::STAR1);tap(StenoKey::STAR1);
  assert(practice.drill==STENO_SQUID_PRACTICE_COLUMNS && practice.multi_count==0 && practice.menu);
  // Columns still uses full released chords, including extra-key errors.
  tap(StenoKey::A);assert(!practice.menu);
  practice.hand_prompt=7;
  steno_squid_practice_view_snapshot(&partial);
  assert(!strcmp(partial.prompt,"DZ"));
  assert(!strcmp(steno_squid_practice_outline(practice.drill,7),"-DZ"));
  answer_correct=false;tap(StenoKey::KL);
  assert(practice.feedback==STENO_SQUID_PRACTICE_INCORRECT && practice.round==0);
  answer_correct=true;
  // Speed advances immediately and a delayed timer cannot credit a late chord.
  practice.drill=STENO_SQUID_PRACTICE_SPEED;practice_switch_drill(0);tap(StenoKey::A);
  const auto deadline=practice.deadline;
  steno_squid_practice_view_snapshot(&partial);
  assert(steno_squid_practice_seconds(partial.progress)==30);
  tap(StenoKey::KL);assert(practice.round==1 && practice.feedback==STENO_SQUID_PRACTICE_PROMPT);
  now=deadline-1;
  tap(StenoKey::KL);assert(practice.round==2);
  now=deadline;tap(StenoKey::KL);
  assert(practice.round==2 && practice.timed_out && practice.feedback==STENO_SQUID_PRACTICE_DONE);
  assert(progress_model.run.finished);
  // A key held across expiry is consumed; releasing a vowel cannot dismiss results.
  tap(StenoKey::A);tap(StenoKey::A); // Menu then a new round.
  practice_process_key(int(StenoKey::E),true);
  now=practice.deadline;practice_clock(nullptr);
  practice_process_key(int(StenoKey::E),false);
  assert(!practice.menu && practice.timed_out);
  tap(StenoKey::A);tap(StenoKey::A);
  const unsigned xp=progress_model.record.xp;
  for(unsigned i=0;i<10;i++)tap(StenoKey::KL);
  assert(practice.round==10 && !practice.timed_out && practice.feedback==STENO_SQUID_PRACTICE_DONE);
  assert(progress_model.record.xp==xp+120);
  now=practice.deadline+1000;practice_clock(nullptr);
  assert(!practice.timed_out); // Winning cannot turn into timeout later.
  // Exercise actual engine -> text encoder -> display receiver, atomically.
  struct steno_squid_practice_view sent;
  practice.drill=STENO_SQUID_PRACTICE_WORDS;
  practice.menu=false;
  practice.round=0;
  practice.feedback=STENO_SQUID_PRACTICE_PROMPT;
  progress_model.run.deck[0]=299;
  practice_publish();
  steno_squid_practice_view_snapshot(&sent);
  assert(!strcmp(sent.prompt,steno_squid_words[299].word));
  uint8_t bytes[STENO_SQUID_PRACTICE_FRAME_MAX];
  const unsigned size=steno_squid_practice_encode(&sent,bytes);
  atomic_set(&practice_screen,0);
  for(unsigned i=0;i<(size+4)/5;i++) {
    zmk_behavior_binding packet{};
    steno_squid_practice_fragment(bytes,size,1,i,&packet.param1,&packet.param2);
    assert(status_received(&packet,{})==0);
    if(i+1<(size+4)/5)assert(practice_screen==0);
  }
  assert(practice_view.state==sent.state && practice_screen==sent.state);
  assert(!strcmp(practice_view.prompt,steno_squid_words[299].word));
  zmk_behavior_binding host_packet{1,STENO_SQUID_HOST_STATUS_PROTOCOL};
  assert(status_received(&host_packet,{})==0 && host_disconnected);
  host_packet.param1=0;
  assert(status_received(&host_packet,{})==0 && !host_disconnected);
  zmk_behavior_binding layer_packet{0,STENO_SQUID_LAYER_NAME_PROTOCOL|STENO_SQUID_PRACTICE_VIEW_PROTOCOL};
  assert(status_received(&layer_packet,{})==0 && mode==MODE_LAYER_NAME);
  practice.active = false;
  assert(!practice_process_key(int(StenoKey::KL), true));
  puts("Practice menu, navigation, start, feedback and replay checks passed");
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('engine', type=Path)
    args = parser.parse_args()
    source = (ROOT / 'src/steno_squid/practice_engine.cc').read_text()
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
