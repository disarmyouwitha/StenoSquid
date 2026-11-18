#pragma once

#include "steno_key_state.h"

#include <stdbool.h>

// Practice state machine for the left half's steno keys. Every function runs
// with the Javelin engine lock held.

// Scores or consumes a steno key while a drill is active. False means the key
// belongs to the engine.
bool steno_squid_practice_process_key(int index, bool pressed);

// The active dictionaries changed; refresh the "no dictionary" state.
void steno_squid_practice_dictionary_changed();

// Provided by engine_init.cc. Cancels partial strokes and repeats at the
// practice boundary and returns, in held, the steno keys that were down.
// False when the engine is not running.
bool steno_squid_engine_cancel_strokes(StenoKeyState &held);

// Drop an interrupted chord without scoring it or emitting partial text.
void steno_squid_practice_discard_chord();
