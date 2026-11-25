// Host tests for src/steno_squid/transfer_frame.h and transfer_session.h.
// argv[1] = hex of a golden DATA frame built independently in Python (zlib CRC).
#include "../../src/steno_squid/transfer_session.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <random>
#include <string>
#include <vector>

using namespace steno_transfer;

static int failures = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); failures++; } } while (0)

struct MemSink : Sink {
    std::vector<uint8_t> data;
    int begins = 0, aborts = 0, verifies = 0, commits = 0, busy_writes = 0;
    SinkResult begin_result = SinkResult::kOk, write_result = SinkResult::kOk,
               verify_result = SinkResult::kOk, commit_result = SinkResult::kOk;
    SinkResult Begin(const BeginInfo &) override { begins++; data.clear(); return begin_result; }
    SinkResult Write(uint32_t offset, const uint8_t *p, size_t n) override {
        if (write_result == SinkResult::kBusy) { busy_writes++; return write_result; }
        if (write_result != SinkResult::kOk) return write_result;
        if (offset != data.size()) { printf("sink saw out-of-order write\n"); failures++; }
        data.insert(data.end(), p, p + n);
        return SinkResult::kOk;
    }
    SinkResult Verify() override { verifies++; return verify_result; }
    SinkResult Commit() override { commits++; return commit_result; }
    void Abort() override { aborts++; }
};

static std::vector<uint8_t> Enc(uint8_t type, uint16_t session, uint32_t offset, const std::vector<uint8_t> &payload = {}) {
    std::vector<uint8_t> out(kMaxFrameBytes);
    size_t n = Encode(type, session, offset, payload.data(), payload.size(), out.data(), out.size());
    out.resize(n);
    return out;
}

static Reply Send(Receiver &r, const std::vector<uint8_t> &bytes, uint32_t now = 0) {
    Decoder d;
    d.Push(bytes.data(), bytes.size());
    Frame f;
    if (!d.Poll(f)) { printf("frame did not decode\n"); failures++; return {}; }
    return r.Handle(f, now);
}

static std::vector<uint8_t> BeginPayload(const std::vector<uint8_t> &body, uint16_t chunk = 49, uint16_t window = 8) {
    std::vector<uint8_t> p(kBeginPayloadBytes);
    Put32(&p[0], body.size());
    Put32(&p[4], Crc32(body.data(), body.size()));
    Put16(&p[8], chunk);
    Put16(&p[10], window);
    p[12] = 0;
    return p;
}

static std::vector<uint8_t> Body(size_t n, uint32_t seed = 1) {
    std::mt19937 rng(seed);
    std::vector<uint8_t> b(n);
    for (auto &x : b) x = rng();
    return b;
}

static void Upload(Receiver &r, uint16_t sid, const std::vector<uint8_t> &body, uint16_t chunk = 49) {
    CHECK(Send(r, Enc(kBegin, sid, 0, BeginPayload(body, chunk))).type == kAck);
    for (size_t off = 0; off < body.size(); off += chunk) {
        size_t n = std::min<size_t>(chunk, body.size() - off);
        Reply a = Send(r, Enc(kData, sid, off, {body.begin() + off, body.begin() + off + n}));
        CHECK(a.type == kAck && a.offset == off + n);
    }
}

static void TestCodec(const char *golden_hex) {
    std::vector<uint8_t> golden;
    for (size_t i = 0; golden_hex[i] && golden_hex[i + 1]; i += 2) golden.push_back(strtoul(std::string(golden_hex + i, 2).c_str(), nullptr, 16));
    // DATA, session 0x0102, offset 0x10, payload "ABC"
    CHECK(Enc(kData, 0x0102, 0x10, {'A', 'B', 'C'}) == golden);
    CHECK(Crc32((const uint8_t *)"123456789", 9) == 0xCBF43926);

    // Byte-by-byte delivery, zero padding between frames, and a magic byte inside payload.
    auto f1 = Enc(kData, 7, 0, {kMagic, kMagic, 1, 2});
    auto f2 = Enc(kEnd, 7, 4);
    std::vector<uint8_t> stream(f1);
    stream.insert(stream.end(), 5, 0);
    stream.insert(stream.end(), f2.begin(), f2.end());
    Decoder d;
    Frame f;
    int frames = 0;
    for (uint8_t b : stream) {
        d.Push(&b, 1);
        while (d.Poll(f)) {
            frames++;
            if (frames == 1) CHECK(f.type == kData && f.length == 4 && f.payload[0] == kMagic);
            if (frames == 2) CHECK(f.type == kEnd && f.offset == 4);
        }
    }
    CHECK(frames == 2);

    // Corrupt frame is dropped, the following frame is still found.
    auto bad = f1; bad[kHeaderBytes] ^= 0x40;
    std::vector<uint8_t> mix(bad); mix.insert(mix.end(), f2.begin(), f2.end());
    Decoder d2; d2.Push(mix.data(), mix.size());
    CHECK(d2.Poll(f) && f.type == kEnd);
    CHECK(!d2.Poll(f) && d2.crc_errors() >= 1);

    // Oversized declared length is rejected without waiting for bytes that never come.
    auto huge = f1; Put16(&huge[8], 60000);
    Decoder d3; d3.Push(huge.data(), huge.size());
    CHECK(!d3.Poll(f));
    d3.Push(f2.data(), f2.size());
    CHECK(d3.Poll(f) && f.type == kEnd);

    // Encoder rejects invalid input.
    uint8_t tiny[8];
    CHECK(Encode(kData, 1, 0, nullptr, 0, tiny, sizeof tiny) == 0);
    CHECK(Encode(kData, 0, 0, nullptr, 0, tiny, 64) == 0);
    std::vector<uint8_t> big(kMaxPayload + 1);
    std::vector<uint8_t> out(kMaxFrameBytes + 8);
    CHECK(Encode(kData, 1, 0, big.data(), big.size(), out.data(), out.size()) == 0);
    CHECK(Encode(kData, 1, 0, big.data(), kMaxPayload, out.data(), out.size()) == kMaxFrameBytes);
}

static void TestHappyPath() {
    MemSink sink; Receiver r(sink);
    auto body = Body(1000);
    Upload(r, 5, body);
    Reply v = Send(r, Enc(kEnd, 5, body.size()));
    CHECK(v.type == kAck && v.payload[0] == 3 && r.state() == State::kVerified);
    CHECK(sink.commits == 0); // Not durable before COMMIT.
    Reply d = Send(r, Enc(kCommit, 5, body.size()));
    CHECK(d.type == kDone && r.state() == State::kCommitted && sink.commits == 1);
    CHECK(sink.data == body && sink.aborts == 0);
    CHECK(Send(r, Enc(kCommit, 5, body.size())).type == kDone && sink.commits == 1); // Idempotent.
    CHECK(Send(r, Enc(kAbort, 5, 0)).type == kError && r.state() == State::kCommitted); // Cannot undo.
    // A new session may follow a finished one.
    Upload(r, 6, Body(60, 2));
    CHECK(r.state() == State::kReceiving);
}

static void TestOrdering() {
    MemSink sink; Receiver r(sink);
    auto body = Body(200);
    CHECK(Send(r, Enc(kBegin, 1, 0, BeginPayload(body))).type == kAck);
    auto chunk = [&](size_t off, size_t n) { return std::vector<uint8_t>(body.begin() + off, body.begin() + off + n); };
    CHECK(Send(r, Enc(kData, 1, 0, chunk(0, 49))).type == kAck);
    Reply gap = Send(r, Enc(kData, 1, 98, chunk(98, 49)));
    CHECK(gap.type == kNak && gap.payload[0] == kNakOutOfOrder && gap.offset == 49);
    Reply dup = Send(r, Enc(kData, 1, 0, chunk(0, 49)));
    CHECK(dup.type == kAck && dup.offset == 49 && sink.data.size() == 49);
    Reply overlap = Send(r, Enc(kData, 1, 40, chunk(40, 49)));
    CHECK(overlap.type == kNak && overlap.payload[0] == kNakBadOffset && sink.data.size() == 49);
    CHECK(Send(r, Enc(kData, 1, 49, chunk(49, 49))).type == kAck);
    // Early END is refused without aborting; the host can keep sending.
    Reply early = Send(r, Enc(kEnd, 1, body.size()));
    CHECK(early.type == kNak && r.state() == State::kReceiving && sink.aborts == 0);
}

static void TestBackpressure() {
    MemSink sink; Receiver r(sink);
    auto body = Body(100);
    Send(r, Enc(kBegin, 1, 0, BeginPayload(body)));
    sink.write_result = SinkResult::kBusy;
    Reply nak = Send(r, Enc(kData, 1, 0, {body.begin(), body.begin() + 49}));
    CHECK(nak.type == kNak && nak.payload[0] == kNakSinkBusy && nak.offset == 0 && r.state() == State::kOpen);
    sink.write_result = SinkResult::kOk;
    CHECK(Send(r, Enc(kData, 1, 0, {body.begin(), body.begin() + 49})).type == kAck);
    // Verify and commit can also push back without failing the session.
    Send(r, Enc(kData, 1, 49, {body.begin() + 49, body.begin() + 98}));
    Send(r, Enc(kData, 1, 98, {body.begin() + 98, body.end()}));
    sink.verify_result = SinkResult::kBusy;
    CHECK(Send(r, Enc(kEnd, 1, 100)).type == kNak && r.state() == State::kReceiving);
    sink.verify_result = SinkResult::kOk;
    CHECK(Send(r, Enc(kEnd, 1, 100)).type == kAck);
    sink.commit_result = SinkResult::kBusy;
    CHECK(Send(r, Enc(kCommit, 1, 100)).type == kNak && r.state() == State::kVerified);
    sink.commit_result = SinkResult::kOk;
    CHECK(Send(r, Enc(kCommit, 1, 100)).type == kDone);
}

static void TestFailures() {
    { // CRC mismatch aborts exactly once and is not success.
        MemSink sink; Receiver r(sink);
        auto body = Body(60);
        auto bp = BeginPayload(body); bp[4] ^= 1;
        Send(r, Enc(kBegin, 1, 0, bp));
        Send(r, Enc(kData, 1, 0, {body.begin(), body.begin() + 49}));
        Send(r, Enc(kData, 1, 49, {body.begin() + 49, body.end()}));
        Reply e = Send(r, Enc(kEnd, 1, 60));
        CHECK(e.type == kError && e.payload[0] == kErrCrc && r.state() == State::kFailed && sink.aborts == 1);
        CHECK(Send(r, Enc(kCommit, 1, 60)).type == kError && sink.commits == 0);
    }
    { // Sink refusals.
        MemSink sink; sink.begin_result = SinkResult::kFail; Receiver r(sink);
        CHECK(Send(r, Enc(kBegin, 1, 0, BeginPayload(Body(10)))).type == kError && sink.aborts == 1);
        sink.begin_result = SinkResult::kOk;
        CHECK(Send(r, Enc(kBegin, 2, 0, BeginPayload(Body(10)))).type == kAck); // Can retry afterwards.
        sink.write_result = SinkResult::kFail;
        CHECK(Send(r, Enc(kData, 2, 0, Body(10))).type == kError && r.state() == State::kFailed && sink.aborts == 2);
    }
    { // Verify and commit failures.
        MemSink sink; Receiver r(sink); auto body = Body(10);
        Upload(r, 1, body); sink.verify_result = SinkResult::kFail;
        CHECK(Send(r, Enc(kEnd, 1, 10)).payload[0] == kErrVerify && sink.aborts == 1);
        MemSink s2; Receiver r2(s2);
        Upload(r2, 1, body); Send(r2, Enc(kEnd, 1, 10)); s2.commit_result = SinkResult::kFail;
        Reply c = Send(r2, Enc(kCommit, 1, 10));
        CHECK(c.type == kError && c.payload[0] == kErrCommit && r2.state() == State::kFailed && s2.aborts == 1);
    }
    { // Malformed parameters and lengths.
        MemSink sink; Receiver r(sink);
        auto body = Body(100);
        CHECK(Send(r, Enc(kBegin, 1, 0, {1, 2, 3})).payload[0] == kErrBadParams);
        auto zero = BeginPayload(body); Put32(&zero[0], 0);
        CHECK(Send(r, Enc(kBegin, 1, 0, zero)).payload[0] == kErrBadParams);
        auto chunk = BeginPayload(body, 241);
        CHECK(Send(r, Enc(kBegin, 1, 0, chunk)).payload[0] == kErrBadParams);
        CHECK(sink.begins == 0);
        Send(r, Enc(kBegin, 1, 0, BeginPayload(body, 32)));
        CHECK(Send(r, Enc(kData, 1, 0, Body(33))).payload[0] == kErrBadLength && sink.aborts == 1); // > chunk
        MemSink s2; Receiver r2(s2);
        Send(r2, Enc(kBegin, 1, 0, BeginPayload(body)));
        CHECK(Send(r2, Enc(kData, 1, 90, Body(20))).payload[0] == kErrBadLength); // past total
    }
}

static void TestSessions() {
    MemSink sink; Receiver r(sink);
    auto body = Body(100);
    CHECK(Send(r, Enc(kData, 9, 0, Body(5))).payload[0] == kErrStaleSession); // No session yet.
    CHECK(Send(r, Enc(kBegin, 3, 0, BeginPayload(body))).type == kAck);
    CHECK(Send(r, Enc(kBegin, 3, 0, BeginPayload(body))).type == kAck && sink.begins == 1); // Retransmitted BEGIN.
    CHECK(Send(r, Enc(kBegin, 4, 0, BeginPayload(body))).payload[0] == kErrBusy);            // No silent switch.
    CHECK(r.state() == State::kOpen && sink.aborts == 0);
    CHECK(Send(r, Enc(kData, 4, 0, Body(5))).payload[0] == kErrStaleSession);                // Wrong id never reaches the sink.
    CHECK(sink.data.empty());
    CHECK(Send(r, Enc(kAbort, 3, 0)).type == kError && r.state() == State::kFailed && sink.aborts == 1);
    CHECK(Send(r, Enc(kAbort, 3, 0)).type == kError && sink.aborts == 1); // Abort once.
    // Finished session ids are not reusable for DATA.
    CHECK(Send(r, Enc(kData, 3, 0, Body(5))).type == kError && sink.data.empty());
}

static void TestTimeoutAndDisconnect() {
    MemSink sink; Receiver r(sink, 1000);
    auto body = Body(100);
    Send(r, Enc(kBegin, 1, 0, BeginPayload(body)), 100);
    CHECK(r.Tick(1099).type == 0 && r.state() == State::kOpen);
    Send(r, Enc(kData, 1, 0, {body.begin(), body.begin() + 49}), 1000); // Activity refreshes the timer.
    CHECK(r.Tick(1999).type == 0);
    Reply t = r.Tick(2000);
    CHECK(t.type == kError && t.payload[0] == kErrTimeout && r.state() == State::kFailed && sink.aborts == 1);
    CHECK(r.Tick(9000).type == 0 && sink.aborts == 1);
    // Timeout during the commit barrier, and wraparound-safe arithmetic.
    MemSink s2; Receiver r2(s2, 1000);
    Upload(r2, 1, Body(10)); Send(r2, Enc(kEnd, 1, 10), 0xFFFFFF00u);
    CHECK(r2.Tick(0xFFFFFF00u + 999).type == 0);
    CHECK(r2.Tick(0xFFFFFF00u + 1000).payload[0] == kErrTimeout && r2.last_error() == kErrTimeout);
    // Disconnect.
    MemSink s3; Receiver r3(s3);
    Upload(r3, 1, Body(100));
    r3.Disconnect();
    CHECK(r3.state() == State::kFailed && s3.aborts == 1 && r3.last_error() == kErrAborted);
    r3.Disconnect();
    CHECK(s3.aborts == 1);
    // A committed transfer is not rewritten by a later disconnect.
    MemSink s4; Receiver r4(s4); Upload(r4, 1, Body(10)); Send(r4, Enc(kEnd, 1, 10)); Send(r4, Enc(kCommit, 1, 10));
    r4.Disconnect();
    CHECK(r4.state() == State::kCommitted && s4.aborts == 0);
}

// Lossy link: frames are dropped, duplicated, reordered within a window and bit-flipped.
// A go-back-N sender recovers; the result must be byte-exact or an explicit failure.
static void TestLossyLink(uint32_t seed) {
    std::mt19937 rng(seed);
    MemSink sink; Receiver r(sink, 1u << 30);
    auto body = Body(5000 + rng() % 3000, seed);
    const uint16_t chunk = 49, sid = 11;
    Decoder dec;
    uint32_t now = 0;
    auto deliver = [&](std::vector<uint8_t> bytes) -> Reply {
        int roll = rng() % 100;
        if (roll < 8) return {};                                   // drop
        if (roll < 14) bytes[rng() % bytes.size()] ^= 1 << (rng() % 8); // corrupt
        Reply last;
        const int copies = roll > 94 ? 2 : 1;                      // duplicate
        for (int c = 0; c < copies; c++) {
            dec.Push(bytes.data(), bytes.size());
            Frame f;
            while (dec.Poll(f)) { Reply x = r.Handle(f, now++); if (x.type) last = x; }
        }
        return last;
    };
    int guard = 0;
    while (r.state() != State::kOpen && guard++ < 100) deliver(Enc(kBegin, sid, 0, BeginPayload(body, chunk)));
    CHECK(r.state() == State::kOpen || r.state() == State::kReceiving);
    guard = 0;
    while (r.next_offset() < body.size() && guard++ < 100000) {
        // Send a window of 4 frames starting at the receiver's expected offset, shuffled.
        std::vector<std::vector<uint8_t>> window;
        for (uint32_t i = 0, off = r.next_offset(); i < 4 && off < body.size(); i++, off += chunk) {
            size_t n = std::min<size_t>(chunk, body.size() - off);
            window.push_back(Enc(kData, sid, off, {body.begin() + off, body.begin() + off + n}));
        }
        if (rng() % 4 == 0) std::shuffle(window.begin(), window.end(), rng);
        for (auto &w : window) deliver(w);
    }
    CHECK(r.next_offset() == body.size());
    guard = 0;
    while (r.state() != State::kVerified && guard++ < 1000) deliver(Enc(kEnd, sid, body.size()));
    guard = 0;
    while (r.state() != State::kCommitted && guard++ < 1000) deliver(Enc(kCommit, sid, body.size()));
    CHECK(r.state() == State::kCommitted && sink.data == body && sink.commits == 1 && sink.aborts == 0);
}

int main(int argc, char **argv) {
    if (argc < 2) { printf("usage: %s golden-hex\n", argv[0]); return 2; }
    TestCodec(argv[1]);
    TestHappyPath();
    TestOrdering();
    TestBackpressure();
    TestFailures();
    TestSessions();
    TestTimeoutAndDisconnect();
    for (uint32_t seed = 1; seed <= 200; seed++) TestLossyLink(seed);
    if (failures) { printf("%d FAILURES\n", failures); return 1; }
    printf("PASS transfer session\n");
    return 0;
}
