#include "clock.h"
#include <zephyr/kernel.h>

uint32_t Clock::GetMilliseconds() { return k_uptime_get_32(); }

uint32_t Clock::GetMicroseconds() {
  return (uint32_t)k_ticks_to_us_floor64(k_uptime_ticks());
}

void Clock::Sleep(uint32_t milliseconds) { k_msleep(milliseconds); }
