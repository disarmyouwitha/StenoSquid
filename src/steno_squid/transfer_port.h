#pragma once
/* Firmware-side glue between HID reports and the transfer session.
 *
 * Host-testable: every firmware dependency (flash lock, input pause, reboot,
 * ownership, clock) comes through the Env template parameter, so the policy
 * below is exercised on the host exactly as it will run on the keyboard.
 *
 * Binary frames start with 0xB5, which console text and Base64 never produce,
 * so a report is routed by its first byte; no mode switch exists and the legacy
 * console is untouched. The host sends one frame per report (zero padded);
 * replies are at most 17 bytes and are packed into the reply buffer.
 *
 * Env requirements:
 *   bool TryService();              // take the flash lock without blocking
 *   void Unlock();
 *   bool StageActive();             // a staged update is open
 *   bool Owns(uint32_t source);     // that update was opened by this connection
 *   void Touch();                   // keep the install watchdog and status alive
 *   uint32_t Now();                 // milliseconds, wrapping
 *   template <class F> void WithFlash(F f);
 *       // Pause typing, unmap flash, run f(); f returns true when the store must
 *       // reopen (reboot sequence), otherwise mapping is restored.
 *
 * `source` is 0 for USB and the BLE session id otherwise.
 */
#include "transfer_staged_sink.h"

namespace steno_transfer {

constexpr size_t kMaxReplyBytes = kHeaderBytes + 3 + kTrailerBytes;

template <class Backend, class Env>
class FirmwarePort {
public:
    FirmwarePort(Backend &backend, Env &env, uint32_t idle_timeout_ms = 5000)
        : env_(env), sink_(backend), receiver_(sink_, idle_timeout_ms) {}

    Receiver &receiver() { return receiver_; }
    StagedSink<Backend> &sink() { return sink_; }

    /* Handle one HID report. Returns the number of reply bytes written to `out`. */
    size_t OnReport(const uint8_t *data, size_t size, uint32_t source, uint8_t *out, size_t capacity) {
        if (source != last_source_) { decoder_.Reset(); last_source_ = source; }
        size_t written = 0;
        for (size_t pushed = 0; pushed < size;) {
            const size_t count = decoder_.Push(data + pushed, size - pushed);
            pushed += count;
            Frame frame;
            // A full reply buffer leaves remaining frames queued for the next report.
            while (capacity - written >= kMaxReplyBytes && decoder_.Poll(frame)) {
                written += HandleFrame(frame, source).Encode(out + written, capacity - written);
            }
            if (!count) break;
        }
        return written;
    }

private:
    Reply HandleFrame(const Frame &f, uint32_t source) {
        if (!env_.TryService()) return Nak(f, kNakSinkBusy); // Flash busy: host retries the same frame.
        const Reply reply = Locked(f, source);
        env_.Unlock();
        return reply;
    }

    Reply Locked(const Frame &f, uint32_t source) {
        const uint32_t now = env_.Now();
        receiver_.Tick(now); // A stalled earlier session is aborted before anything else.
        if (receiver_.active() && source != session_source_) return Fail(f, kErrBusy); // One connection owns it.
        if (f.type == kBegin && !receiver_.active()) {
            // Only attach to an update this very connection opened on the legacy console.
            if (!env_.StageActive() || !env_.Owns(source)) return Fail(f, kErrBadState);
            rebooted_ = false;
        }
        Reply reply;
        env_.WithFlash([&] {
            reply = receiver_.Handle(f, now);
            const State s = receiver_.state();
            const bool finished = s == State::kCommitted || s == State::kFailed;
            const bool reboot = sink_.reopen_required() && finished && !rebooted_;
            rebooted_ |= reboot;
            return reboot;
        });
        if (f.type == kBegin && receiver_.active()) session_source_ = source;
        if (receiver_.active()) env_.Touch();
        return reply;
    }

    static Reply Nak(const Frame &f, uint8_t reason) {
        Reply r; r.type = kNak; r.session = f.session; r.offset = f.offset;
        r.payload[0] = reason; r.length = 1;
        return r;
    }
    static Reply Fail(const Frame &f, uint8_t code) {
        Reply r; r.type = kError; r.session = f.session; r.offset = 0;
        r.payload[0] = code; r.length = 1;
        return r;
    }

    Env &env_;
    StagedSink<Backend> sink_;
    Receiver receiver_;
    Decoder decoder_;
    uint32_t last_source_ = 0, session_source_ = 0;
    bool rebooted_ = false;
};

} // namespace steno_transfer
