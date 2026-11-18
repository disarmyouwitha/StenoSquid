#include "clock.h"
#include <zephyr/kernel.h>
#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
#include "native_input.h"
#endif

uint32_t Clock::GetMilliseconds() {
#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
  return uint32_t(steno_squid_native_input_time());
#else
  return k_uptime_get_32();
#endif
}

uint32_t Clock::GetMicroseconds() {
  return (uint32_t)k_ticks_to_us_floor64(k_uptime_ticks());
}

void Clock::Sleep(uint32_t milliseconds) { k_msleep(milliseconds); }
