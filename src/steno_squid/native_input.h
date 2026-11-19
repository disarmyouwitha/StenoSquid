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
void steno_squid_native_input_resume_discard(void);
void steno_squid_native_input_cancel(void);
bool steno_squid_native_input_pending(void);
bool steno_squid_native_input_waiting(void);
int64_t steno_squid_native_input_time(void);
uint32_t steno_squid_native_input_overflows(void);
uint32_t steno_squid_native_input_highwater(void);
uint32_t steno_squid_native_input_max_delay_ms(void);
#ifdef __cplusplus
}
class NativeInputPause {
  public:
    explicit NativeInputPause(bool discard = false) : discard(discard) {
        steno_squid_native_input_pause();
    }
    ~NativeInputPause() {
        if (discard) steno_squid_native_input_resume_discard();
        else steno_squid_native_input_resume();
    }
    NativeInputPause(const NativeInputPause &) = delete;
    NativeInputPause &operator=(const NativeInputPause &) = delete;

  private:
    bool discard;
};
#endif
