#pragma once

#include "lapwing_flash.h"

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
