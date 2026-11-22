// Catches what the Javelin engine prints for each stroke and hands the last
// stroke, its translation and words per minute to the split status sender.
//
// The engine reports strokes as console events: a paper tape event for every
// stroke and, if enabled, a suggestion event when a shorter outline exists. No
// host reads those events on this keyboard, so they are enabled only for the
// duration of one key event, with a writer that keeps the text.
#include "console.h"
#include "wpm_tracker.h"
#include "writer.h"
#include "split_status.h"
#include "stroke_feed.h"
#include "stroke_hint_policy.h"
#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
#include "native_input.h"
#endif

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

// Per-stroke timing is a diagnostic: it exists only in the logging build.
#if defined(CONFIG_ZMK_STENO_SQUID_STROKE_HINT) && defined(CONFIG_ZMK_JAVELIN_DIAG) && !defined(CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK)
#define STENO_SQUID_TAPE_TIMING 1
#endif

namespace {

class TapeWriter final : public IWriter {
public:
  void Write(const char *data, size_t length) override {
    // Text past the end is dropped. A stroke's own paper tape block comes
    // first, so the part that matters is kept.
    if (length > sizeof(buffer) - count) {
      length = sizeof(buffer) - count;
    }
    memcpy(buffer + count, data, length);
    count += length;
  }

  char buffer[1024];
  size_t count = 0;
};

TapeWriter tapeWriter;
bool paperTapeWasEnabled;
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_HINT
bool suggestionWasEnabled;
bool hintBudgetActive;
bool hintBudgetStarted;
int64_t hintDeadline;
StenoSquidStrokeHintPolicy hintPolicy;
uint32_t keyTime;

bool InputWaiting() {
#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
  return steno_squid_native_input_waiting();
#else
  return false;
#endif
}
#endif
#ifdef STENO_SQUID_TAPE_TIMING
uint32_t startCycles;
#endif
bool active;

void Publish(const steno_squid_tape_event &event) {
  static uint8_t sequence;
  sequence = (sequence + 1) & 3u;

  steno_squid_tape tape = {};
  tape.valid = true;
  tape.seq = sequence;
  tape.mask = event.mask;
  if (event.has_text) {
    char text[STENO_SQUID_TAPE_RAW_MAX + 1];
    steno_squid_tape_clean_text(event.raw, text, sizeof(text));
    tape.truncated = strlen(text) > STENO_SQUID_TAPE_TEXT_MAX;
    memcpy(tape.text, text, MIN(strlen(text), STENO_SQUID_TAPE_TEXT_MAX));
  } else if (event.undo) {
    strcpy(tape.text, "undo");
  }

  // Count what this stroke typed, then read the rate. The tracker leaves the
  // current second out, so a first stroke reads zero until the next second.
  if (event.has_text) {
    const unsigned typed = steno_squid_tape_typed_chars(event.raw);
    if (typed) {
      WpmTracker::instance.Tally(typed);
    }
  }
  const int wpm = WpmTracker::instance.Get10sWpm();
  tape.wpm = wpm > 255 ? 255 : wpm < 0 ? 0 : wpm;

  if (event.hint) {
    tape.hint_mask = event.hint_mask;
    tape.hint_count = event.hint_count;
  }
  steno_squid_tape_publish(&tape);
}

} // namespace

extern "C" bool steno_squid_hint_should_yield(void) {
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_HINT
  if (hintBudgetActive) {
    const int64_t now = k_uptime_get();
    if (!hintBudgetStarted) {
      hintDeadline = now + 8;
      hintBudgetStarted = true;
    }
    return now >= hintDeadline || InputWaiting();
  }
#endif
  return false;
}

extern "C" void steno_squid_stroke_feed_begin(uint32_t when, bool hints) {
  if (active) {
    return; // Never nested. A second begin would lose the first one's flags.
  }
  active = true;
  tapeWriter.count = 0;
  paperTapeWasEnabled = Console::IsEventEnabled(ConsoleEvent::PAPER_TAPE);
  Console::EnableEvent(ConsoleEvent::PAPER_TAPE);
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_HINT
  suggestionWasEnabled = Console::IsEventEnabled(ConsoleEvent::SUGGESTION);
  keyTime = when;
  hintBudgetActive = hints && hintPolicy.Allow(when) && !InputWaiting();
  hintBudgetStarted = false;
  if (hintBudgetActive) {
    Console::EnableEvent(ConsoleEvent::SUGGESTION);
  } else {
    Console::DisableEvent(ConsoleEvent::SUGGESTION);
  }
#else
  (void)when;
  (void)hints;
#endif
#ifdef STENO_SQUID_TAPE_TIMING
  startCycles = k_cycle_get_32();
#endif
  ConsoleWriter::Push(&tapeWriter);
}

extern "C" void steno_squid_stroke_feed_end(void) {
  if (!active) {
    return;
  }
  active = false;
  ConsoleWriter::Pop();
  if (!paperTapeWasEnabled) {
    Console::DisableEvent(ConsoleEvent::PAPER_TAPE);
  }
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_HINT
  hintBudgetActive = false;
  if (suggestionWasEnabled) {
    Console::EnableEvent(ConsoleEvent::SUGGESTION);
  } else {
    Console::DisableEvent(ConsoleEvent::SUGGESTION);
  }
#endif
#ifdef STENO_SQUID_TAPE_TIMING
  const uint32_t cycles = k_cycle_get_32() - startCycles;
#endif
  if (tapeWriter.count == 0) {
    return;
  }

  steno_squid_tape_event event;
  if (!steno_squid_tape_parse(tapeWriter.buffer, tapeWriter.count, &event)) {
    return;
  }
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_HINT
  hintPolicy.Record(keyTime);
#endif
#ifdef STENO_SQUID_TAPE_TIMING
  // How long a stroke takes with suggestions on. This runs on the thread that
  // handles keys, so watch for anything over a few milliseconds.
  LOG_INF("[tape] stroke handled in %u us%s", (unsigned)k_cyc_to_us_floor32(cycles),
          event.hint ? ", with a hint" : "");
#endif
  Publish(event);
}
