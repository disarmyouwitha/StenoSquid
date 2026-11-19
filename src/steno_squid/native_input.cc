#include "native_input.h"
#include "native_store_runtime.h"
#include "javelin_engine_lock.h"
#include "engine_work.h"
#include "../../tools/storage/native_input_queue.h"
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
extern "C" {
extern struct k_mutex flash_lock;
void zmk_javelin_steno_native_process_key(int key, bool pressed, uint32_t when);
void zmk_javelin_steno_native_cancel(void);
}
namespace {
candidate_b::NativeInputQueue<512> input;
struct k_spinlock input_lock;
atomic_t max_replay_delay_ms; // The engine queue is the only writer.
void Drain(k_work *);
K_WORK_DEFINE(drain_work, Drain);
// Only the engine queue reads these values; callers on other threads never
// inspect them. Microsecond time remains real, so sleep/busy waits still work.
bool replaying = false;
int64_t replay_time = 0;
void Schedule() { k_work_submit_to_queue(&steno_squid_engine_queue, &drain_work); }
void Drain(k_work *) {
    auto key = k_spin_lock(&input_lock);
    const bool run = input.BeginDrain();
    k_spin_unlock(&input_lock, key);
    if (!run)
        return;
    {
        const JavelinEngineLock lock;
        if (!lock) {
            k_mutex_lock(&flash_lock, K_FOREVER);
            key = k_spin_lock(&input_lock);
            input.Cancel(k_uptime_get_32());
            candidate_b::NativeInputEvent ignored;
            while (input.Pop(ignored)) {
            }
            k_spin_unlock(&input_lock, key);
            zmk_javelin_steno_native_cancel();
            k_mutex_unlock(&flash_lock);
        } else {
            for (unsigned count = 0; count < 64; ++count) {
                candidate_b::NativeInputEvent event;
                key = k_spin_lock(&input_lock);
                const bool next = input.Pop(event);
                k_spin_unlock(&input_lock, key);
                if (!next)
                    break;
                if (event.key == 255) {
                    zmk_javelin_steno_native_cancel();
                    continue;
                }
                const int64_t now = k_uptime_get();
                const uint32_t delay = uint32_t(now) - event.when;
                if (delay > uint32_t(atomic_get(&max_replay_delay_ms)))
                    atomic_set(&max_replay_delay_ms, delay);
                replay_time = now - delay;
                replaying = true;
                zmk_javelin_steno_native_process_key(event.key, event.pressed, event.when);
                replaying = false;
            }
        }
    }
    // Keep deferral set until AFTER releasing flash_lock: a fresh key cannot
    // overtake the batch between the final pop and mutex release.
    key = k_spin_lock(&input_lock);
    input.FinishDrain();
    const bool more = input.HasEvents() && !input.Paused();
    k_spin_unlock(&input_lock, key);
    if (more)
        Schedule();
}
} // namespace
extern "C" bool steno_squid_native_input_offer(int key, bool pressed, uint32_t when) {
    const auto guard = k_spin_lock(&input_lock);
    const auto result = input.Offer(key, pressed, when);
    const bool wake = input.HasEvents() && !input.Paused();
    k_spin_unlock(&input_lock, guard);
    if (wake)
        Schedule();
    return result != candidate_b::InputOffer::Immediate;
}
extern "C" bool steno_squid_native_input_retry(int key, bool pressed, uint32_t when) {
    if (!steno_squid_native_dictionary_ready())
        return false;
    const auto guard = k_spin_lock(&input_lock);
    input.Retry(key, pressed, when);
    const bool wake = !input.Paused();
    k_spin_unlock(&input_lock, guard);
    if (wake)
        Schedule();
    return true;
}
extern "C" void steno_squid_native_input_pause(void) {
    const auto key = k_spin_lock(&input_lock);
    input.Pause();
    k_spin_unlock(&input_lock, key);
}
extern "C" void steno_squid_native_input_resume(void) {
    const auto key = k_spin_lock(&input_lock);
    input.Resume();
    const bool wake = !input.Paused();
    k_spin_unlock(&input_lock, key);
    if (wake)
        Schedule();
}
extern "C" void steno_squid_native_input_cancel(void) {
    const auto key = k_spin_lock(&input_lock);
    input.Cancel(k_uptime_get_32());
    const bool wake = input.HasEvents() && !input.Paused();
    k_spin_unlock(&input_lock, key);
    if (wake)
        Schedule();
}
extern "C" bool steno_squid_native_input_pending(void) {
    const auto key = k_spin_lock(&input_lock);
    const bool result = input.Pending();
    k_spin_unlock(&input_lock, key);
    return result;
}
extern "C" bool steno_squid_native_input_waiting(void) {
    const auto key = k_spin_lock(&input_lock);
    const bool result = input.HasEvents();
    k_spin_unlock(&input_lock, key);
    return result;
}
extern "C" int64_t steno_squid_native_input_time(void) {
    if (k_current_get() == k_work_queue_thread_get(&steno_squid_engine_queue) && replaying)
        return replay_time;
    return k_uptime_get();
}
extern "C" uint32_t steno_squid_native_input_overflows(void) {
    const auto key = k_spin_lock(&input_lock);
    auto result = input.Overflows();
    k_spin_unlock(&input_lock, key);
    return result;
}
extern "C" uint32_t steno_squid_native_input_highwater(void) {
    const auto key = k_spin_lock(&input_lock);
    auto result = input.Highwater();
    k_spin_unlock(&input_lock, key);
    return result;
}
extern "C" uint32_t steno_squid_native_input_max_delay_ms(void) {
    return atomic_get(&max_replay_delay_ms);
}
