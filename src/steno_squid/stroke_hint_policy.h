#pragma once

#include <stdint.h>

class StenoSquidStrokeHintPolicy {
public:
    // The 200 WPM board fixture emits a chord every 245 ms, and bursts are
    // faster. Keep suggestions for slower input and for the first chord
    // after a pause. Unsigned subtraction also handles uptime wraparound.
    bool Allow(uint32_t now) const {
        return !have_stroke || uint32_t(now - last_stroke) >= 375;
    }

    void Record(uint32_t now) {
        last_stroke = now;
        have_stroke = true;
    }

private:
    uint32_t last_stroke = 0;
    bool have_stroke = false;
};
