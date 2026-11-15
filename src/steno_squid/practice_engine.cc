// The Practice drills of the right nice!view, run on the left half's steno
// keys. While a drill is active its chords never reach the Javelin engine: this
// state machine scores them against the drill's prompt and publishes the screen
// state for the split status link. engine_init.cc calls steno_squid_practice_
// process_key() for every steno key, under the engine lock.
#include "practice_engine.h"

#include "engine.h"
#include "javelin_engine_lock.h"
#include "lapwing_dictionary.h"
#include "practice_build.h"
#include "practice_multi.h"
#include "practice_progress.h"
#include "practice_types.h"
#include "practice_view.h"
#include "practice_words.h"
#include "split_status.h"
#include "steno_key_state.h"
#include "stroke.h"
#include "zmk_javelin_steno/zmk_platform_shim.h"

#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/atomic.h>

K_MUTEX_DEFINE(practice_mutex);
static atomic_t practice_screen;
static StenoKeyState practice_pending_releases;
static struct {
  bool active;
  bool menu;
  bool ignore_chord;
  bool no_dictionary;
  uint8_t round;
  uint8_t hand_prompt;
  uint8_t streak;
  uint8_t best;
  uint8_t multi_count;
  uint8_t build_stage;
  bool timed_out;
  bool multi_second_error;
  StenoStroke multi_strokes[2];
  int64_t deadline;
  int64_t advance_at;
  int64_t prompt_started_at;
  steno_squid_practice_drill drill;
  steno_squid_practice_feedback feedback;
  StenoKeyState keys;
  StenoKeyState chord;
} practice;

static unsigned practice_prompt() {
  return steno_squid_practice_word_drill(practice.drill) || (practice.drill == STENO_SQUID_PRACTICE_MULTI || practice.drill == STENO_SQUID_PRACTICE_BUILD)
             ? steno_squid_words_prompt(practice.round) : practice.hand_prompt;
}

// Draws the next hand-drill prompt. After the first, it never repeats the
// one just shown, which would look as if nothing had happened.
static void practice_draw_hand_prompt(bool avoid_previous) {
  const unsigned count = steno_squid_practice_prompt_count(practice.drill);
  if (avoid_previous && count > 1) {
    unsigned next = sys_rand32_get() % (count - 1);
    if (next >= practice.hand_prompt) {
      ++next;
    }
    practice.hand_prompt = next;
  } else {
    practice.hand_prompt = sys_rand32_get() % count;
  }
}

static void practice_publish() {
  uint32_t snapshot = 0;
  if (practice.active) {
    snapshot = STENO_SQUID_PRACTICE_ACTIVE |
               (practice.menu ? STENO_SQUID_PRACTICE_MENU : 0) |
               (practice.timed_out ? STENO_SQUID_PRACTICE_TIMEOUT : 0) |
               (practice.multi_second_error ? STENO_SQUID_PRACTICE_SECOND_ERROR : 0) |
               (practice.multi_count == 1 ? STENO_SQUID_PRACTICE_PARTIAL : 0) |
               (uint32_t(practice.build_stage) << STENO_SQUID_PRACTICE_BUILD_SHIFT) |
               (practice.no_dictionary ? STENO_SQUID_PRACTICE_NO_DICTIONARY : 0) |
               (uint32_t(practice.feedback) << STENO_SQUID_PRACTICE_FEEDBACK_SHIFT) |
               (uint32_t(practice.round) << STENO_SQUID_PRACTICE_ROUND_SHIFT) |
               (uint32_t(practice.streak) << STENO_SQUID_PRACTICE_STREAK_SHIFT) |
               (uint32_t(practice.best) << STENO_SQUID_PRACTICE_BEST_SHIFT) |
               (uint32_t(practice.drill) << STENO_SQUID_PRACTICE_DRILL_SHIFT);
  }
  atomic_set(&practice_screen, snapshot);
  steno_squid_split_status_refresh();
}

static void practice_clock(struct k_work *);
K_WORK_DELAYABLE_DEFINE(practice_clock_work, practice_clock);

/* Called with practice_mutex held, including on every key event. Checking
 * the deadline here prevents a delayed work queue from accepting late input. */
static bool practice_speed_expired() {
  if (!practice.active || practice.menu || practice.drill != STENO_SQUID_PRACTICE_SPEED ||
      practice.feedback == STENO_SQUID_PRACTICE_DONE || k_uptime_get() < practice.deadline) return false;
  practice.timed_out = true;
  practice.feedback = STENO_SQUID_PRACTICE_DONE;
  practice_pending_releases |= practice.keys;
  practice.keys.Reset();
  practice.chord.Reset();
  steno_squid_speed_timeout();
  practice_publish();
  return true;
}

static void practice_clock(struct k_work *) {
  k_mutex_lock(&practice_mutex, K_FOREVER);
  if (!practice_speed_expired() && practice.active && !practice.menu &&
      practice.drill == STENO_SQUID_PRACTICE_SPEED && practice.feedback != STENO_SQUID_PRACTICE_DONE) {
    practice_publish();
    const int64_t now = k_uptime_get();
    const int64_t next = practice.deadline < now + 1000 ? practice.deadline : now + 1000;
    k_work_reschedule(&practice_clock_work, K_TIMEOUT_ABS_MS(next));
  }
  k_mutex_unlock(&practice_mutex);
}

void steno_squid_practice_view_snapshot(struct steno_squid_practice_view *view) {
  k_mutex_lock(&practice_mutex, K_FOREVER);
  memset(view, 0, sizeof(*view));
  view->state = atomic_get(&practice_screen);
  view->progress = steno_squid_practice_progress();
  if (practice.active && !practice.menu && practice.drill == STENO_SQUID_PRACTICE_SPEED &&
      practice.feedback != STENO_SQUID_PRACTICE_DONE) {
    const int64_t left = practice.deadline - k_uptime_get();
    const unsigned seconds = left > 0 ? (left + 999) / 1000 : 0;
    view->progress |= uint64_t(seconds > STENO_SQUID_SPEED_SECONDS ? STENO_SQUID_SPEED_SECONDS : seconds) << 46;
  }
  if (practice.active && !practice.menu && practice.feedback != STENO_SQUID_PRACTICE_DONE) {
    const unsigned id = practice_prompt();
    const bool words = steno_squid_practice_word_drill(practice.drill);
    if (practice.drill == STENO_SQUID_PRACTICE_MULTI) {
      const char *outline = steno_squid_multi[id].outline;
      snprintf(view->prompt, sizeof(view->prompt), "%s", steno_squid_multi[id].word);
      snprintf(view->outline, sizeof(view->outline), "%s", outline);
    } else if (practice.drill == STENO_SQUID_PRACTICE_BUILD) {
      const bool reveal = practice.build_stage == STENO_SQUID_BUILD_STEPS - 1 &&
                          practice.feedback == STENO_SQUID_PRACTICE_CORRECT;
      if (reveal) snprintf(view->prompt, sizeof(view->prompt), "%s", steno_squid_build[id].word);
      else steno_squid_build_format(view->prompt, sizeof(view->prompt), steno_squid_build[id].strokes[practice.build_stage]);
      if (reveal) steno_squid_build_format(view->outline, sizeof(view->outline), steno_squid_build[id].strokes[practice.build_stage]);
      else snprintf(view->outline, sizeof(view->outline), "Step %u/%u", practice.build_stage + 1, STENO_SQUID_BUILD_STEPS);
    } else {
      const char *outline = words ? steno_squid_words[id].outline : steno_squid_practice_outline(practice.drill, id);
      const char *shown = practice.drill == STENO_SQUID_PRACTICE_COLUMNS && outline[0] == '-' ? outline + 1 : outline;
      snprintf(view->prompt, sizeof(view->prompt), "%s", words ? steno_squid_words[id].word : shown);
      snprintf(view->outline, sizeof(view->outline), "%s", words ? outline :
          practice.drill == STENO_SQUID_PRACTICE_COLUMNS ? "Chord keys" : "Tap key");
    }
  }
  const unsigned review = steno_squid_practice_review_word();
  if (practice.feedback == STENO_SQUID_PRACTICE_DONE && review < STENO_SQUID_WORD_COUNT) {
    if (practice.drill == STENO_SQUID_PRACTICE_BUILD && review < STENO_SQUID_BUILD_COUNT) {
      snprintf(view->review, sizeof(view->review), "%s", steno_squid_build[review].word);
    } else if (practice.drill == STENO_SQUID_PRACTICE_MULTI && review < STENO_SQUID_MULTI_COUNT) {
      snprintf(view->review, sizeof(view->review), "%s", steno_squid_multi[review].word);
    } else if (steno_squid_practice_word_drill(practice.drill)) {
      snprintf(view->review, sizeof(view->review), "%s", steno_squid_words[review].word);
    }
  }
  k_mutex_unlock(&practice_mutex);
}

static void practice_advance(struct k_work *) {
  k_mutex_lock(&practice_mutex, K_FOREVER);
  if (practice.active && practice.feedback == STENO_SQUID_PRACTICE_CORRECT &&
      k_uptime_get() >= practice.advance_at) {
    if (practice.drill == STENO_SQUID_PRACTICE_BUILD && practice.build_stage + 1 < STENO_SQUID_BUILD_STEPS) {
      ++practice.build_stage;
      practice.feedback = STENO_SQUID_PRACTICE_PROMPT;
      practice_publish();
      k_mutex_unlock(&practice_mutex);
      return;
    }
    practice.build_stage = 0;
    ++practice.round;
    practice.feedback = practice.round == steno_squid_practice_rounds(practice.drill)
                            ? STENO_SQUID_PRACTICE_DONE
                            : STENO_SQUID_PRACTICE_PROMPT;
    if (practice.feedback == STENO_SQUID_PRACTICE_PROMPT && !steno_squid_practice_word_drill(practice.drill) && practice.drill != STENO_SQUID_PRACTICE_MULTI && practice.drill != STENO_SQUID_PRACTICE_BUILD) {
      practice_draw_hand_prompt(true);
    }
    practice.prompt_started_at = k_uptime_get();
    if (practice.feedback == STENO_SQUID_PRACTICE_DONE) {
      steno_squid_practice_finish();
    }
    practice_publish();
  }
  k_mutex_unlock(&practice_mutex);
}

K_WORK_DELAYABLE_DEFINE(practice_advance_work, practice_advance);

static const StenoDictionary *practice_fallback_dictionary() {
  // WORDS is a Lapwing drill. Use the verified base locally even when its
  // checkbox is off; never change the engine's saved dictionary selection.
  return lapwing_dictionary();
}

static void practice_refresh_dictionary() {
  practice.no_dictionary =
      (steno_squid_practice_word_drill(practice.drill) || practice.drill == STENO_SQUID_PRACTICE_MULTI) &&
      StenoEngine::container.value.GetDictionary().GetMaximumOutlineLength() == 0 &&
      !practice_fallback_dictionary();
}

static void practice_switch_drill(int direction) {
  steno_squid_practice_end();
  k_work_cancel_delayable(&practice_advance_work);
  k_work_cancel_delayable(&practice_clock_work);
  const int count = STENO_SQUID_PRACTICE_DRILL_COUNT;
  const auto drill = static_cast<steno_squid_practice_drill>((practice.drill + direction + count) % count);
  practice = {};
  practice.active = true;
  practice.menu = true;
  practice.drill = drill;
  if (!steno_squid_practice_word_drill(drill) && drill != STENO_SQUID_PRACTICE_MULTI && drill != STENO_SQUID_PRACTICE_BUILD) {
    practice_draw_hand_prompt(false);
  }
  practice_refresh_dictionary();
  practice_publish();
}

/* The first chord follows the displayed learning outline. Both failed strokes
 * restart at the first chord; a separate flag identifies a second-chord error. */
static bool practice_first_stroke_correct(const StenoStroke &stroke) {
  const char *outline = steno_squid_multi[practice_prompt()].outline;
  const char *slash = strchr(outline, '/');
  char first[STENO_SQUID_PRACTICE_OUTLINE_SIZE];
  snprintf(first, sizeof(first), "%.*s", int(slash - outline), outline);
  StenoStroke expected;
  expected.Set(first);
  return stroke == expected;
}

static bool practice_correct(const StenoStroke &stroke) {
  if (practice.drill == STENO_SQUID_PRACTICE_BUILD) {
    StenoStroke expected;
    expected.Set(steno_squid_build[practice_prompt()].strokes[practice.build_stage]);
    return stroke == expected;
  }
  if (!steno_squid_practice_word_drill(practice.drill) && practice.drill != STENO_SQUID_PRACTICE_MULTI) {
    StenoStroke expected;
    expected.Set(steno_squid_practice_outline(practice.drill, practice_prompt()));
    return stroke == expected;
  }
  const bool multi = practice.drill == STENO_SQUID_PRACTICE_MULTI;
  const StenoDictionaryLookup lookup(multi ? practice.multi_strokes : &stroke, multi ? 2 : 1);
  const char *expected = multi ? steno_squid_multi[practice_prompt()].word : steno_squid_words[practice_prompt()].word;
  auto result = StenoEngine::container.value.GetDictionary().Lookup(lookup);
  bool correct = result.IsValid() && strcasecmp(result.GetText(), expected) == 0;
  result.Destroy();
  if (!correct) {
    if (const auto *base = practice_fallback_dictionary()) {
      result = base->Lookup(lookup);
      correct = result.IsValid() && strcasecmp(result.GetText(), expected) == 0;
      result.Destroy();
    }
  }
  return correct;
}

extern "C" uint32_t steno_squid_practice_snapshot(void) {
  return uint32_t(atomic_get(&practice_screen));
}

extern "C" void steno_squid_practice_set_active(bool active) {
  if (active) {
    zmk_javelin_steno_init();
  } else {
    // Leaving a layer that never started practice needs no engine access,
    // so ordinary layer changes do not touch the dictionary flash.
    k_mutex_lock(&practice_mutex, K_FOREVER);
    const bool was_active = practice.active;
    k_mutex_unlock(&practice_mutex);
    if (!was_active) {
      return;
    }
  }
  const JavelinEngineLock engine_lock;
  if (!engine_lock) {
    return;
  }
  k_mutex_lock(&practice_mutex, K_FOREVER);
  if (active != practice.active) {
    k_work_cancel_delayable(&practice_advance_work);
    k_work_cancel_delayable(&practice_clock_work);
    // Cancel partial strokes and repeats at the practice boundary.
    StenoKeyState held;
    const bool engine_running = steno_squid_engine_cancel_strokes(held);
    if (engine_running) {
      practice_pending_releases |= held;
    }
    practice_pending_releases |= practice.keys;
    steno_squid_practice_end();
    practice = {};
    practice.active = active && engine_running;
    practice.menu = practice.active;
    if (practice.active) {
      practice_refresh_dictionary();
    }
    practice_publish();
  }
  k_mutex_unlock(&practice_mutex);
}

// Called under the engine lock, so dictionary edits and flash sleep cannot
// race a lookup. Practice chords never enter the host-output processor.
static bool practice_process_key(int index, bool pressed) {
  k_mutex_lock(&practice_mutex, K_FOREVER);
  practice_speed_expired();
  StenoKeyState key;
  key.Process(static_cast<StenoKey>(index), true);
  if ((practice_pending_releases & key).IsNotEmpty()) {
    practice_pending_releases &= ~key;
    if (!pressed) {
      k_mutex_unlock(&practice_mutex);
      return true;
    }
  }
  if (!practice.active) {
    k_mutex_unlock(&practice_mutex);
    return false;
  }
  if (pressed && practice.keys.IsEmpty()) {
    practice.ignore_chord = practice.feedback == STENO_SQUID_PRACTICE_CORRECT ||
                            practice.feedback == STENO_SQUID_PRACTICE_DONE;
  }
  practice.keys.Process(static_cast<StenoKey>(index), pressed);
  practice.chord |= practice.keys;
  if (!pressed && practice.keys.IsEmpty() && practice.chord.IsNotEmpty()) {
    const auto steno_key = static_cast<StenoKey>(index);
    if (practice.chord == key && steno_key >= StenoKey::STAR1 && steno_key <= StenoKey::STAR4) {
      practice_switch_drill(steno_key <= StenoKey::STAR2 ? -1 : 1);
    } else if (practice.menu || practice.feedback == STENO_SQUID_PRACTICE_DONE) {
      // Consume the start key; the first scored chord comes after release.
      if (practice.chord == key &&
          (steno_key == StenoKey::A || steno_key == StenoKey::O ||
           steno_key == StenoKey::E || steno_key == StenoKey::U)) {
        if (practice.menu) {
          if (practice.drill == STENO_SQUID_PRACTICE_TROUBLE && !steno_squid_trouble_begin()) {
            practice.drill = STENO_SQUID_PRACTICE_WORDS;
          }
          practice.menu = false;
          if (practice.drill == STENO_SQUID_PRACTICE_BUILD) steno_squid_build_begin();
          else if (practice.drill == STENO_SQUID_PRACTICE_MULTI) steno_squid_multi_begin();
          else if (practice.drill == STENO_SQUID_PRACTICE_WORDS || practice.drill == STENO_SQUID_PRACTICE_SPEED) steno_squid_words_begin();
          else if (!steno_squid_practice_word_drill(practice.drill)) steno_squid_hands_begin();
          if (practice.drill == STENO_SQUID_PRACTICE_SPEED) {
            practice.deadline = k_uptime_get() + STENO_SQUID_SPEED_SECONDS * 1000;
            k_work_reschedule(&practice_clock_work, K_TIMEOUT_ABS_MS(k_uptime_get() + 1000));
          }
          practice.prompt_started_at = k_uptime_get();
          practice_refresh_dictionary();
          practice_publish();
        } else {
          practice_switch_drill(0);
        }
      }
    } else if (!practice.ignore_chord && practice.feedback != STENO_SQUID_PRACTICE_CORRECT &&
               practice.feedback != STENO_SQUID_PRACTICE_DONE) {
      practice_refresh_dictionary();
      if (practice.no_dictionary) {
        practice.multi_count = 0;
        practice.feedback = STENO_SQUID_PRACTICE_PROMPT;
      } else {
        const StenoStroke stroke = practice.chord.ToStroke();
        const bool first_error = practice.drill == STENO_SQUID_PRACTICE_MULTI &&
            practice.multi_count == 0 && !practice_first_stroke_correct(stroke);
        practice.multi_second_error = false;
        if (practice.drill == STENO_SQUID_PRACTICE_MULTI && !first_error) {
          practice.multi_strokes[practice.multi_count++] = stroke;
        }
        if (practice.drill == STENO_SQUID_PRACTICE_MULTI && !first_error && practice.multi_count == 1) {
          practice.feedback = STENO_SQUID_PRACTICE_PROMPT;
        } else {
          const bool correct = !first_error && practice_correct(stroke);
          practice.multi_second_error = practice.drill == STENO_SQUID_PRACTICE_MULTI && !correct && !first_error;
          practice.multi_count = 0;
          const int64_t elapsed = k_uptime_get() - practice.prompt_started_at;
          const uint32_t elapsed_ms = elapsed > UINT32_MAX ? UINT32_MAX : (uint32_t)elapsed;
          if (steno_squid_practice_word_drill(practice.drill)) {
            steno_squid_words_answer(practice.round, correct, elapsed_ms);
          } else if (!correct || practice.drill != STENO_SQUID_PRACTICE_BUILD ||
                     practice.build_stage + 1 == STENO_SQUID_BUILD_STEPS) {
            steno_squid_hands_answer(practice.round, correct, elapsed_ms);
          }
          if (correct) {
            if (practice.drill != STENO_SQUID_PRACTICE_BUILD || practice.build_stage + 1 == STENO_SQUID_BUILD_STEPS) {
              ++practice.streak;
              if (practice.streak > practice.best) practice.best = practice.streak;
            }
            if (practice.drill == STENO_SQUID_PRACTICE_SPEED) {
              ++practice.round;
              practice.feedback = practice.round == STENO_SQUID_SPEED_ROUNDS ?
                  STENO_SQUID_PRACTICE_DONE : STENO_SQUID_PRACTICE_PROMPT;
              practice.prompt_started_at = k_uptime_get();
              if (practice.feedback == STENO_SQUID_PRACTICE_DONE) {
                k_work_cancel_delayable(&practice_clock_work);
                steno_squid_practice_finish();
              }
            } else {
              practice.feedback = STENO_SQUID_PRACTICE_CORRECT;
              const unsigned pause = practice.drill == STENO_SQUID_PRACTICE_BUILD &&
                  practice.build_stage + 1 == STENO_SQUID_BUILD_STEPS ? 600 : 250;
              practice.advance_at = k_uptime_get() + pause;
              k_work_reschedule(&practice_advance_work, K_TIMEOUT_ABS_MS(practice.advance_at));
            }
          } else {
            practice.streak = 0;
            practice.feedback = STENO_SQUID_PRACTICE_INCORRECT;
          }
        }
      }
      practice_publish();
    }
    practice.chord.Reset();
  }
  k_mutex_unlock(&practice_mutex);
  return true;
}

bool steno_squid_practice_process_key(int index, bool pressed) {
  return practice_process_key(index, pressed);
}

void steno_squid_practice_dictionary_changed() {
  k_mutex_lock(&practice_mutex, K_FOREVER);
  if (practice.active) {
    practice_refresh_dictionary();
    practice_publish();
  }
  k_mutex_unlock(&practice_mutex);
}
