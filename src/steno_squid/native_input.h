#pragma once
#include <stdbool.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
bool steno_squid_native_input_offer(int key, bool pressed, uint32_t when);
bool steno_squid_native_input_retry(int key, bool pressed, uint32_t when);
void steno_squid_native_input_pause(void);
void steno_squid_native_input_resume(void);
void steno_squid_native_input_cancel(void);
bool steno_squid_native_input_pending(void);
int64_t steno_squid_native_input_time(void);
uint32_t steno_squid_native_input_overflows(void);
uint32_t steno_squid_native_input_highwater(void);
#ifdef __cplusplus
}
class NativeInputPause {
  public:
    NativeInputPause() { steno_squid_native_input_pause(); }
    ~NativeInputPause() { steno_squid_native_input_resume(); }
    NativeInputPause(const NativeInputPause &) = delete;
    NativeInputPause &operator=(const NativeInputPause &) = delete;
};
#endif
