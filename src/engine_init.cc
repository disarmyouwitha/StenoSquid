#include "zmk_javelin_steno/zmk_platform_shim.h"

#include "console.h"
#include "button_script_manager.h"
#include "container/list.h"
#include "dictionary/dictionary_list.h"
#include "engine.h"
#include "orthography.h"
#include "english_orthography.h"
#ifdef CONFIG_ZMK_JAVELIN_STENO_MACOS_US_LAYOUT
#include "host_layout.h"
#include "platform/macos_us_layout.h"
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
#include "platform/windows_us_layout.h"
#endif
#endif
#include "processor/all_up.h"
#include "processor/first_up.h"
#include "processor/jeff_modifiers.h"
#include "processor/processor.h"
#include "processor/repeat.h"
#include "steno_key_state.h"
#include "static_allocate.h"
#include "stroke.h"
#if defined(CONFIG_ZMK_STENO_SQUID_PRACTICE) || defined(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
#include "steno_squid/split_status.h"
#endif
#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
#include <zephyr/random/random.h>
#include <new>
#include <strings.h>
#include <zephyr/sys/atomic.h>
#endif

#include "lapwing_dictionary.h"
#include "lapwing_flash.h"
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
#include "user_dictionary_store.h"
#endif

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/logging/log.h>
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
#include <zephyr/settings/settings.h>
#include <errno.h>
#endif

LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

static JavelinStaticAllocate<StenoCompiledOrthography> compiledOrthography;
static JavelinStaticAllocate<StenoDictionaryList> dictionaryList;
static JavelinStaticAllocate<StenoJeffModifiers> jeffModifiers;
static JavelinStaticAllocate<StenoRepeat> stenoRepeat;
static JavelinStaticAllocate<StenoAllUp> allUp;
static StenoProcessor *processor = nullptr;

static bool initialized = false;

// Held across every engine access. It keeps the external dictionary flash
// powered, and reports false while the flash is asleep, unprovisioned or still
// being verified.
class JavelinEngineLock {
public:
  JavelinEngineLock() : held(lapwing_flash_acquire()) {}
  ~JavelinEngineLock() {
    if (held) {
      lapwing_flash_release();
    }
  }
  explicit operator bool() const { return held; }

private:
  bool held;
};

// The saved dictionary selection is one ZMK settings byte, kept independently of
// the base images and personal dictionary snapshots. A fresh keyboard starts
// with Lapwing. personal=1, Lapwing=2, Plover=4, then the Lapwing extras from
// bit 3 up.
#define EXTRAS_SELECTION_SHIFT 3
#define EXTRAS_SELECTION_BIT(index) (uint8_t(1) << (EXTRAS_SELECTION_SHIFT + (index)))
// Commands, numbers and proper nouns start on. UK additions start off.
#define EXTRAS_SELECTION_DEFAULT                                                                   \
  (EXTRAS_SELECTION_BIT(EXTRAS_COMMANDS) | EXTRAS_SELECTION_BIT(EXTRAS_NUMBERS) |                  \
   EXTRAS_SELECTION_BIT(EXTRAS_PROPER_NOUNS))
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
static uint8_t dictionary_selection = 3 | EXTRAS_SELECTION_DEFAULT;
static bool dictionary_enabled(uint8_t bit, bool) { return dictionary_selection & bit; }
#else
// Without the browser tool nothing can change the selection.
static bool dictionary_enabled(uint8_t, bool fallback) { return fallback; }
#endif

#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
#include "steno_squid/practice_progress.h"
#include "steno_squid/practice_words.h"
#include "steno_squid/practice_multi.h"
#include "steno_squid/practice_build.h"
#include "steno_squid/practice_view.h"
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
    if (processor) {
      // Cancel partial strokes and repeats at the practice boundary.
      practice_pending_releases |= processor->GetCurrentKeyState();
      stenoRepeat.value.Process(StenoKeyState(), StenoAction::CANCEL_ALL);
      new (processor) StenoProcessor(stenoRepeat.value);
    }
    practice_pending_releases |= practice.keys;
    steno_squid_practice_end();
    practice = {};
    practice.active = active && processor;
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

static void practice_dictionary_changed() {
  k_mutex_lock(&practice_mutex, K_FOREVER);
  if (practice.active) {
    practice_refresh_dictionary();
    practice_publish();
  }
  k_mutex_unlock(&practice_mutex);
}
#endif

#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
#ifdef CONFIG_ZMK_JAVELIN_STENO_MACOS_US_LAYOUT
// These indexes are stored in ZMK settings.
static constexpr uint8_t HOST_PROFILE_MAC = 0;
static constexpr uint8_t HOST_PROFILE_WINDOWS = 1;
static constexpr const char *host_profiles[] = {"Mac U.S.", "Windows U.S. (plain text)"};
static uint8_t host_profile = HOST_PROFILE_WINDOWS;

static void apply_host_profile() {
  HostLayouts::SetActiveLayout(host_profile == HOST_PROFILE_MAC ? zmk_javelin_macos_us_layout()
                                                               : zmk_javelin_windows_us_layout());
}

static void get_host_parameter(void *, const char *line) {
  const char *name = strchr(line, ' ');
  if (name && strcmp(name + 1, "available_host_layouts") == 0) {
    Console::Printf("[\"%s\",\"%s\"]\n\n", host_profiles[0], host_profiles[1]);
  } else if (name && strcmp(name + 1, "host_layout") == 0) {
    Console::Printf("%s\n\n", host_profiles[host_profile]);
  } else {
    Console::Printf("ERR Unknown parameter\n\n");
  }
}

static void set_host_profile(void *, const char *line) {
  const char *name = strchr(line, ' ');
  for (uint8_t profile = 0; name && profile < ARRAY_SIZE(host_profiles); ++profile) {
    if (strcmp(name + 1, host_profiles[profile]) != 0) {
      continue;
    }
    if (profile != host_profile) {
      if (settings_save_one("javelin/host_profile", &profile, sizeof(profile)) != 0) {
        Console::Printf("ERR Unable to save host layout\n\n");
        return;
      }
      host_profile = profile;
      apply_host_profile();
    }
    Console::SendOk();
    return;
  }
  Console::Printf("ERR Unknown host layout\n\n");
}
#endif

static int javelin_settings_set(const char *name, size_t length,
                                settings_read_cb read, void *argument) {
#ifdef CONFIG_ZMK_JAVELIN_STENO_MACOS_US_LAYOUT
  if (strcmp(name, "host_profile") == 0) {
    uint8_t profile;
    if (length != sizeof(profile) || read(argument, &profile, sizeof(profile)) != sizeof(profile) ||
        profile >= ARRAY_SIZE(host_profiles)) {
      return -EINVAL;
    }
    host_profile = profile;
    return 0;
  }
#endif
  if (strcmp(name, "dictionaries") != 0) {
    return -ENOENT;
  }
  uint8_t selection;
  if (length != sizeof(selection) || read(argument, &selection, sizeof(selection)) != sizeof(selection) ||
      (selection & 0x80)) {
    return -EINVAL;
  }
  dictionary_selection = selection;
  return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(javelin, "javelin", nullptr, javelin_settings_set,
                               nullptr, nullptr);

static void select_dictionary(void *, const char *line) {
  const char *name = strchr(line, ' ');
  if (!name) {
    Console::Printf("ERR No dictionary specified\n\n");
    return;
  }
  ++name;
  uint8_t bit = 0;
  if (strcmp(name, "user_dictionary") == 0) {
    bit = 1;
  } else if (strcmp(name, "Lapwing Base") == 0) {
    bit = 2;
  } else if (strcmp(name, "Plover Base") == 0 && plover_dictionary()) {
    bit = 4;
  }
  for (size_t i = 0; !bit && i < EXTRAS_DICTIONARY_COUNT; i++) {
    if (extras_dictionary(i) && strcmp(name, extras_dictionary_name(i)) == 0) {
      bit = EXTRAS_SELECTION_BIT(i);
    }
  }
  if (!bit) {
    Console::Printf("ERR Unknown dictionary\n\n");
    return;
  }
  uint8_t selected = dictionary_selection;
  if (strncmp(line, "enable_dictionary ", 18) == 0) {
    selected |= bit;
  } else if (strncmp(line, "disable_dictionary ", 19) == 0) {
    selected &= ~bit;
  } else {
    selected ^= bit;
  }
  // Persist before acknowledging or changing the active engine. On failure the
  // previous selection remains active and the browser receives an error.
  if (selected != dictionary_selection &&
      settings_save_one("javelin/dictionaries", &selected, sizeof(selected)) != 0) {
    Console::Printf("ERR Unable to save dictionary selection\n\n");
    return;
  }
  dictionary_selection = selected;
  BufferWriter events;
  ConsoleWriter::Push(&events);
  if (selected & bit) {
    StenoEngine::container.value.EnableDictionary(name);
  } else {
    StenoEngine::container.value.DisableDictionary(name);
  }
  ConsoleWriter::Pop();
#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
  practice_dictionary_changed();
#endif
  Console::SendOk();
  events.WriteBufferTo(ConsoleWriter::GetActiveWriter());
}
#endif

static bool load_dictionary_collection() {
  List<StenoDictionaryListEntry> entries;
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
  auto *personal = steno_squid_user_dictionary_open();
  if (personal) {
    entries.Add(StenoDictionaryListEntry(personal, dictionary_selection & 1));
  }
#endif
  // The Lapwing extras rank above the bases, in the order Plover gives them.
  for (size_t i = 0; i < EXTRAS_DICTIONARY_COUNT; i++) {
    if (auto *extra = extras_dictionary(i)) {
      entries.Add(StenoDictionaryListEntry(
          extra, dictionary_enabled(EXTRAS_SELECTION_BIT(i), i != EXTRAS_UK_ADDITIONS)));
    }
  }
  entries.Add(StenoDictionaryListEntry(lapwing_dictionary(), dictionary_enabled(2, true)));
  if (auto *plover = plover_dictionary()) {
    // Personal overrides remain first. Fresh settings default to Lapwing.
    entries.Add(StenoDictionaryListEntry(plover, dictionary_enabled(4, false)));
  }

  if (entries.IsEmpty()) {
    LOG_ERR("Steno dict collection empty");
    return false;
  }

  new (dictionaryList)
      StenoDictionaryList(static_cast<List<StenoDictionaryListEntry> &&>(entries));

  printk("[JAV] initializing orthography\n");
  new (compiledOrthography) StenoCompiledOrthography(english_orthography);
#ifdef CONFIG_ZMK_JAVELIN_STENO_MACOS_US_LAYOUT
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
  apply_host_profile();
#else
  HostLayouts::SetActiveLayout(zmk_javelin_macos_us_layout());
#endif
#endif

  printk("[JAV] constructing engine\n");
  new (StenoEngine::container) StenoEngine(
      dictionaryList.value,
      nullptr,
      compiledOrthography.value,
      StenoStroke(StrokeMask::STAR),  // undo stroke = *
      nullptr);

  // Register console commands
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
#ifdef CONFIG_ZMK_JAVELIN_STENO_MACOS_US_LAYOUT
  Console::instance.RegisterCommand("get_parameter", "Read host layout parameters",
                                    get_host_parameter, nullptr);
  Console::instance.RegisterCommand("set_host_layout", "Set and save the host layout",
                                    set_host_profile, nullptr);
#endif
  Console::instance.RegisterCommand("enable_dictionary", "Enable and save selection",
                                    select_dictionary, nullptr);
  Console::instance.RegisterCommand("disable_dictionary", "Disable and save selection",
                                    select_dictionary, nullptr);
  Console::instance.RegisterCommand("toggle_dictionary", "Toggle and save selection",
                                    select_dictionary, nullptr);
  if (personal) {
    steno_squid_user_dictionary_register(Console::instance, dictionaryList.value);
  }
#endif
  StenoEngine::container.value.AddConsoleCommands(Console::instance);

  LOG_INF("Steno engine initialized");
  return true;
}

extern "C" {

void zmk_javelin_steno_init(void) {
  const JavelinEngineLock lock;
  if (!lock || initialized) {
    return;
  }
  if (!lapwing_dictionary()) {
    LOG_WRN("External Lapwing dictionary is unavailable");
    return;
  }
  initialized = true;
  printk("[JAV] lazy init start; thread=%s\n", k_thread_name_get(k_current_get()));

  // Construct the manager queried by the emitter even though ZMK handles keys.
  alignas(4) static const uint8_t empty_button_script[64] = {0x4a, 0x53, 0x53, 0x34};
  ButtonScriptManager::Initialize(empty_button_script);

  if (!load_dictionary_collection()) {
    LOG_WRN("Steno engine not started");
    return;
  }

  // Processor pipeline: Repeat → AllUp → JeffModifiers → Engine
  new (jeffModifiers) StenoJeffModifiers(StenoEngine::container.value);
  new (allUp) StenoAllUp(jeffModifiers.value);
  new (stenoRepeat) StenoRepeat(allUp.value);

  static StenoProcessor processorInstance(stenoRepeat.value);
  processor = &processorInstance;

  printk("[JAV] processor pipeline ready\n");
  LOG_INF("Steno processor pipeline ready");
}

void zmk_javelin_steno_process_key(int steno_key_index, bool is_press) {
  // Steno keys whose press reached the engine. A key held while the flash
  // becomes ready must not deliver a release without its press. Key events
  // all arrive on the system work queue, so this needs no lock.
  static uint64_t delivered_presses;
  const uint64_t bit =
      steno_key_index >= 0 && steno_key_index < 64 ? uint64_t(1) << steno_key_index : 0;

  const JavelinEngineLock lock;
  const bool ready = lock && processor;
  if (is_press) {
    if (!ready) {
      return;
    }
    delivered_presses |= bit;
  } else {
    const bool was_delivered = !bit || (delivered_presses & bit);
    delivered_presses &= ~bit;
    if (!ready || !was_delivered) {
      return;
    }
  }
#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
  if (practice_process_key(steno_key_index, is_press)) {
    return;
  }
#endif
  LOG_DBG("[JAV] key %d %s", steno_key_index, is_press ? "down" : "up");
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_TAPE
  // Catch the engine's stroke events for the right display's stroke tape.
  steno_squid_stroke_feed_begin();
#endif
  processor->Process(static_cast<StenoKey>(steno_key_index), is_press);
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_TAPE
  steno_squid_stroke_feed_end();
#endif
}

} // extern "C"

#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
bool steno_squid_javelin_console_line(const char *line, size_t size, IWriter &writer) {
  const JavelinEngineLock lock;
  if (!lock) {
    // The flash is asleep, unprovisioned or still verifying.
    return false;
  }
  zmk_javelin_steno_init();
  ConsoleWriter::Push(&writer);
  Console::instance.HandleInput(line, size, ConnectionId::SERIAL_CONSOLE);
  ConsoleWriter::Pop();
  return true;
}
#endif
