// Single ordered input stream. Caller supplies a short lock around each method.
// Overflow/mode cancellation discards the entire pending chord and waits for all
// physical keys up; it never turns a partial chord into another translation.
#pragma once
#include <cstdint>
namespace candidate_b {
struct NativeInputEvent {
    uint32_t when = 0;
    uint8_t key = 255;
    bool pressed = false;
};
enum class InputOffer { Immediate, Queued, Discarded };
template <unsigned Capacity = 512> class NativeInputQueue {
    static_assert(Capacity >= 4 && Capacity <= 4096);
    NativeInputEvent events[Capacity]{};
    uint64_t held = 0;
    uint32_t first = 0, count = 0, depth = 0, high = 0, overflows = 0, cancellations = 0,
             cancel_when = 0;
    bool deferred = false, discard = false, cancel = false, draining = false;
    InputOffer Append(uint8_t key, bool pressed, uint32_t when) {
        if (discard) {
            if (!held) {
                discard = false;
                cancel = true;
                cancel_when = when;
                deferred = true;
            }
            return InputOffer::Discarded;
        }
        if (count == Capacity) {
            ++overflows;
            first = count = 0;
            discard = held != 0;
            cancel = true;
            cancel_when = when;
            deferred = true;
            return InputOffer::Discarded;
        }
        events[(first + count) % Capacity] = {when, key, pressed};
        ++count;
        if (count > high)
            high = count;
        return InputOffer::Queued;
    }

  public:
    void Pause() {
        ++depth;
        deferred = true;
    }
    void Resume() {
        if (depth)
            --depth;
    }
    bool Paused() const { return depth != 0; }
    bool Pending() const { return depth || draining || count || cancel; }
    bool HasEvents() const { return count || cancel; }
    bool BeginDrain() {
        if (depth)
            return false;
        draining = true;
        return true;
    }
    void FinishDrain() {
        draining = false;
        if (!depth && !count && !cancel && !discard)
            deferred = false;
    }
    InputOffer Offer(int key, bool pressed, uint32_t when) {
        if (key < 0 || key >= 64)
            return InputOffer::Discarded;
        const uint64_t bit = uint64_t(1) << key;
        if (pressed)
            held |= bit;
        else
            held &= ~bit;
        return deferred || depth ? Append(uint8_t(key), pressed, when) : InputOffer::Immediate;
    }
    // The delivery thread already offered this event before failing its flash
    // lock attempt. Keep its original timestamp without updating physical state.
    void Retry(int key, bool pressed, uint32_t when) {
        deferred = true;
        Append(uint8_t(key), pressed, when);
    }
    void Cancel(uint32_t when) {
        if (!deferred && !depth && !draining && !count)
            return;
        ++cancellations;
        first = count = 0;
        discard = held != 0;
        cancel = true;
        cancel_when = when;
        deferred = true;
    }
    bool Pop(NativeInputEvent &event) {
        if (depth)
            return false;
        if (cancel) {
            event = {cancel_when, 255, false};
            cancel = false;
            return true;
        }
        if (!count)
            return false;
        event = events[first];
        first = (first + 1) % Capacity;
        --count;
        return true;
    }
    uint32_t Count() const { return count; }
    uint32_t Highwater() const { return high; }
    uint32_t Overflows() const { return overflows; }
    uint32_t Cancellations() const { return cancellations; }
};
static_assert(sizeof(NativeInputEvent) == 8);
} // namespace candidate_b
