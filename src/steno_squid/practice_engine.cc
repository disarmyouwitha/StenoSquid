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

// Private state machine shared verbatim with native scheduler tests.
static bool practice_first_stroke_correct(const StenoStroke &stroke);
static bool practice_correct(const StenoStroke &stroke);
static void practice_refresh_dictionary();
#include "practice_state.h"

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

#include "practice_scoring.h"

extern "C" void steno_squid_practice_set_active(bool active) {
  if (active) {
    zmk_javelin_steno_prepare();
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

void steno_squid_practice_discard_chord() {
  k_mutex_lock(&practice_mutex, K_FOREVER);
  const bool changed = practice.keys.IsNotEmpty() || practice.chord.IsNotEmpty() || practice.multi_count;
  practice.keys = {};
  practice.chord = {};
  practice.multi_count = 0;
  practice_pending_releases = {};
  if (changed && practice.active) practice_publish();
  k_mutex_unlock(&practice_mutex);
}
