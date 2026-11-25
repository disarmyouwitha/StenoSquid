#pragma once
/* Transport-neutral receive-side transfer session (host -> keyboard upload).
 *
 * One session at a time, strictly in-order streaming into an abstract Sink
 * (the future adapter over the existing staged writer). No buffering of the
 * whole payload: the session only keeps counters and a running CRC.
 *
 * Success ladder, never skipped:
 *   ACK(Open)      BEGIN accepted by the sink
 *   ACK(Receiving) DATA accepted by the sink (offset = next expected byte)
 *   ACK(Verified)  END: length and whole-payload CRC match and Sink::Verify passed
 *   DONE           COMMIT: Sink::Commit made the result durable
 * Cancel, timeout, disconnect and every error call Sink::Abort exactly once,
 * and a session that did not reach DONE is never reported as success.
 */
#include "transfer_frame.h"

namespace steno_transfer {

enum class SinkResult : uint8_t { kOk, kBusy, kFail };

constexpr size_t kBeginPayloadBytes = 13; // total u32, crc u32, max_chunk u16, window u16, kind u8
constexpr size_t kMaxBeginArgs = 16;      // optional sink-defined bytes after the fixed fields
constexpr uint16_t kMaxWindow = 16;

struct BeginInfo {
    uint32_t total;
    uint32_t crc;          // CRC-32 of the whole payload stream
    uint8_t kind;
    const uint8_t *args;   // sink-defined, valid only during Begin
    size_t args_length;
};

class Sink {
public:
    virtual ~Sink() = default;
    virtual SinkResult Begin(const BeginInfo &info) = 0;
    /* Offsets arrive strictly in order. kBusy means "not consumed, retry". */
    virtual SinkResult Write(uint32_t offset, const uint8_t *data, size_t size) = 0;
    virtual SinkResult Verify() = 0;  // e.g. staged flash readback + integrity
    virtual SinkResult Commit() = 0;  // e.g. catalog publication
    virtual void Abort() = 0;         // release or quarantine staged work
};

enum class State : uint8_t { kIdle, kOpen, kReceiving, kVerified, kCommitted, kFailed };

enum ErrorCode : uint8_t {
    kErrNone = 0,
    kErrBusy = 1,         // another session is active
    kErrStaleSession = 2, // unknown or finished session id
    kErrBadState = 3,
    kErrBadLength = 4,    // payload longer than chunk/total allows
    kErrCrc = 5,          // whole-payload CRC mismatch
    kErrSink = 6,         // sink refused Begin/Write
    kErrVerify = 7,
    kErrCommit = 8,
    kErrTimeout = 9,
    kErrAborted = 10,     // host ABORT or disconnect
    kErrBadParams = 11,
};

enum NakReason : uint8_t {
    kNakOutOfOrder = 1, // offset is ahead of the expected one
    kNakBadOffset = 2,  // overlaps accepted data without matching its boundary
    kNakSinkBusy = 3,   // backpressure: retry the same frame later
};

struct Reply {
    uint8_t type = 0; // 0 means "send nothing"
    uint16_t session = 0;
    uint32_t offset = 0;
    uint8_t payload[4] = {};
    uint8_t length = 0;

    size_t Encode(uint8_t *out, size_t capacity) const {
        return type ? steno_transfer::Encode(type, session, offset, payload, length, out, capacity) : 0;
    }
};

class Receiver {
public:
    explicit Receiver(Sink &sink, uint32_t idle_timeout_ms = 5000)
        : sink_(sink), idle_timeout_(idle_timeout_ms) {}

    State state() const { return state_; }
    uint8_t last_error() const { return last_error_; }
    uint32_t next_offset() const { return next_; }
    bool active() const { return state_ == State::kOpen || state_ == State::kReceiving || state_ == State::kVerified; }

    Reply Handle(const Frame &f, uint32_t now_ms) {
        if (f.type == kBegin) return Begin(f, now_ms);
        if (f.type < kBegin || f.type > kAbort) return Error(f.session, kErrBadState);
        if (f.session != session_ || (state_ == State::kIdle)) return Error(f.session, kErrStaleSession);
        switch (f.type) {
        case kData: return Data(f, now_ms);
        case kEnd: return End(f, now_ms);
        case kCommit: return Commit(f, now_ms);
        default: // kAbort
            if (!active()) return Error(f.session, kErrBadState); // Never rewrites a finished result.
            return Fail(kErrAborted);
        }
    }

    /* Call periodically from the worker. Aborts a stalled session. */
    Reply Tick(uint32_t now_ms) {
        if (active() && now_ms - last_activity_ >= idle_timeout_) return Fail(kErrTimeout);
        return {};
    }

    /* Transport disconnected or the BLE session changed. Never reports success. */
    void Disconnect() {
        if (active()) Fail(kErrAborted);
    }

private:
    Reply Begin(const Frame &f, uint32_t now) {
        if (f.length < kBeginPayloadBytes || f.length > kBeginPayloadBytes + kMaxBeginArgs)
            return Error(f.session, kErrBadParams);
        const uint32_t total = Get32(f.payload), crc = Get32(f.payload + 4);
        const uint16_t chunk = Get16(f.payload + 8), window = Get16(f.payload + 10);
        const uint8_t kind = f.payload[12];
        if (total == 0 || chunk == 0 || chunk > kMaxPayload || window == 0) return Error(f.session, kErrBadParams);
        if (active()) {
            // A retransmitted BEGIN is idempotent only while nothing was received.
            if (f.session == session_ && total == total_ && crc == crc_ && chunk == chunk_ && kind == kind_ &&
                f.length == begin_length_ && !memcmp(f.payload + kBeginPayloadBytes, args_, f.length - kBeginPayloadBytes) &&
                state_ == State::kOpen) {
                last_activity_ = now;
                return Ack(kStateOpen);
            }
            return Error(f.session, kErrBusy); // No silent session switching.
        }
        session_ = f.session;
        if (sink_.Begin({total, crc, kind, f.payload + kBeginPayloadBytes, f.length - kBeginPayloadBytes}) !=
            SinkResult::kOk) {
            sink_.Abort(); // Release anything a refused Begin left behind.
            state_ = State::kFailed;
            last_error_ = kErrSink;
            next_ = 0;
            return Error(session_, kErrSink);
        }
        total_ = total; crc_ = crc; chunk_ = chunk; kind_ = kind;
        window_ = window > kMaxWindow ? kMaxWindow : window;
        begin_length_ = f.length;
        memcpy(args_, f.payload + kBeginPayloadBytes, f.length - kBeginPayloadBytes);
        next_ = 0;
        running_ = 0;
        last_error_ = kErrNone;
        last_activity_ = now;
        state_ = State::kOpen;
        return Ack(kStateOpen);
    }

    Reply Data(const Frame &f, uint32_t now) {
        if (state_ != State::kOpen && state_ != State::kReceiving) {
            // Late DATA after END/commit/failure must not touch the sink.
            return state_ == State::kVerified ? Nak(kNakBadOffset) : Error(f.session, kErrBadState);
        }
        if (f.length == 0 || f.length > chunk_ || uint64_t(f.offset) + f.length > total_) {
            return Fail(kErrBadLength);
        }
        last_activity_ = now;
        if (f.offset + uint64_t(f.length) <= next_) return Ack(kStateReceiving); // Duplicate; already written.
        if (f.offset > next_) return Nak(kNakOutOfOrder);
        if (f.offset < next_) return Nak(kNakBadOffset); // Partial overlap: sender must resend from next_.
        switch (sink_.Write(f.offset, f.payload, f.length)) {
        case SinkResult::kOk: break;
        case SinkResult::kBusy: return Nak(kNakSinkBusy);
        default: return Fail(kErrSink);
        }
        running_ = Crc32Update(running_, f.payload, f.length);
        next_ += f.length;
        state_ = State::kReceiving;
        return Ack(kStateReceiving);
    }

    Reply End(const Frame &f, uint32_t now) {
        if (state_ == State::kVerified) return Ack(kStateVerified); // Repeated END is idempotent.
        if (state_ != State::kOpen && state_ != State::kReceiving) return Error(f.session, kErrBadState);
        last_activity_ = now;
        if (f.offset != total_ || next_ != total_) {
            return f.offset == total_ ? Nak(kNakOutOfOrder) : Fail(kErrBadLength);
        }
        if (running_ != crc_) return Fail(kErrCrc);
        const SinkResult verified = sink_.Verify();
        if (verified == SinkResult::kBusy) return Nak(kNakSinkBusy);
        if (verified != SinkResult::kOk) return Fail(kErrVerify);
        state_ = State::kVerified;
        return Ack(kStateVerified);
    }

    Reply Commit(const Frame &f, uint32_t now) {
        if (state_ == State::kCommitted) return Done(); // Repeated COMMIT is idempotent.
        if (state_ != State::kVerified) return Error(f.session, kErrBadState);
        last_activity_ = now;
        const SinkResult committed = sink_.Commit();
        if (committed == SinkResult::kBusy) return Nak(kNakSinkBusy);
        if (committed != SinkResult::kOk) return Fail(kErrCommit);
        state_ = State::kCommitted;
        return Done();
    }

    /* Terminal failure: abort the sink once and remember why. */
    Reply Fail(uint8_t code) {
        if (active()) sink_.Abort();
        state_ = State::kFailed;
        last_error_ = code;
        return Error(session_, code);
    }

    static constexpr uint8_t kStateOpen = 1, kStateReceiving = 2, kStateVerified = 3;

    Reply Ack(uint8_t status) const {
        Reply r;
        r.type = kAck; r.session = session_; r.offset = next_;
        r.payload[0] = status;
        const uint16_t credit = window_;
        Put16(r.payload + 1, credit);
        r.length = 3;
        return r;
    }
    Reply Nak(uint8_t reason) const {
        Reply r;
        r.type = kNak; r.session = session_; r.offset = next_;
        r.payload[0] = reason; r.length = 1;
        return r;
    }
    Reply Error(uint16_t session, uint8_t code) const {
        Reply r;
        r.type = kError; r.session = session ? session : 1; r.offset = next_;
        r.payload[0] = code; r.length = 1;
        return r;
    }
    Reply Done() const {
        Reply r;
        r.type = kDone; r.session = session_; r.offset = next_;
        return r;
    }

    Sink &sink_;
    const uint32_t idle_timeout_;
    State state_ = State::kIdle;
    uint16_t session_ = 0;
    uint32_t total_ = 0, crc_ = 0, next_ = 0, running_ = 0, last_activity_ = 0;
    uint16_t chunk_ = 0, window_ = 0;
    uint8_t kind_ = 0, last_error_ = kErrNone;
    uint8_t begin_length_ = 0, args_[kMaxBeginArgs] = {};
};

} // namespace steno_transfer
