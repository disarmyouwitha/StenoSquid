#pragma once
#include <stdint.h>

/* Only the key-delivery thread uses this guard. Contention discards the
 * interrupted chord, never a release by itself or part of the next chord. */
struct StenoSquidChordDelivery {
    uint64_t held = 0;
    bool interrupted = false;
    bool fresh_press = false;
    void Event(int index, bool pressed) {
        fresh_press = pressed && !held;
        const uint64_t bit = index >= 0 && index < 64 ? uint64_t(1) << index : 0;
        if (pressed) held |= bit;
        else held &= ~bit;
    }
    void Drop() { interrupted = true; }
    bool Recover() {
        // The caller cancels the engine's old chord before calling Recover.
        interrupted = held && !fresh_press;
        return fresh_press;
    }
};
