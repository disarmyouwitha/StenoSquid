// Host tests for the firmware glue policy in src/steno_squid/transfer_port.h.
#include "../../src/steno_squid/transfer_port.h"

#include <cstdio>
#include <map>
#include <vector>

using namespace steno_transfer;

static int failures = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); failures++; } } while (0)

struct FakeBackend {
    bool active = true;
    uint8_t kind = 3;
    uint32_t epoch = 1;
    int aborts = 0, begins_seen = 0;
    StagedCommit commit_result = StagedCommit::kOk;
    bool commit_reopen = true;
    std::map<uint32_t, uint8_t> flash;
    bool Active() const { return active; }
    uint8_t KindId() const { return kind; }
    uint32_t Epoch() const { return epoch; }
    bool Erase(uint32_t) { return true; }
    bool Write(uint32_t a, const uint8_t *d, uint32_t n) { for (uint32_t i = 0; i < n; i++) flash[a + i] = d[i]; return true; }
    bool Read(uint32_t a, uint8_t *o, uint32_t n) { for (uint32_t i = 0; i < n; i++) o[i] = flash[a + i]; return true; }
    StagedCommit Commit(uint32_t, uint32_t, uint32_t, bool &reopen) { reopen = commit_reopen; return commit_result; }
    void Abort() { aborts++; active = false; }
};

struct FakeEnv {
    bool lock_free = true, locked = false, owner = true, stage_active = true;
    uint32_t owner_source = 0, now = 1000;
    int unlocks = 0, touches = 0, flash_calls = 0, reboots = 0, mapped_restores = 0;
    bool TryService() { if (!lock_free) return false; locked = true; return true; }
    void Unlock() { CHECK(locked); locked = false; unlocks++; }
    bool StageActive() { return stage_active; }
    bool Owns(uint32_t s) { return owner && s == owner_source; }
    void Touch() { touches++; }
    uint32_t Now() { return now; }
    template <class F> void WithFlash(F f) {
        CHECK(locked); // Flash work only ever happens under the lock.
        flash_calls++;
        if (f()) reboots++; else mapped_restores++;
    }
};

using Port = FirmwarePort<FakeBackend, FakeEnv>;

static std::vector<uint8_t> MakeFrame(uint8_t type, uint16_t session, uint32_t offset, const std::vector<uint8_t> &payload = {}) {
    std::vector<uint8_t> out(kMaxFrameBytes);
    out.resize(Encode(type, session, offset, payload.data(), payload.size(), out.data(), out.size()));
    return out;
}

// Record stream: erase segment 5, write 8 bytes at its start.
static std::vector<uint8_t> Stream() {
    std::vector<uint8_t> s = {kRecordErase, 5, 0, kRecordWrite};
    uint8_t h[6]; Put32(h, 5 * 65536); Put16(h + 4, 8);
    s.insert(s.end(), h, h + 6);
    for (uint8_t i = 0; i < 8; i++) s.push_back(0xA0 + i);
    return s;
}

static std::vector<uint8_t> BeginPayload(const std::vector<uint8_t> &stream, uint8_t kind = 3) {
    std::vector<uint8_t> p(13 + kStagedArgsBytes);
    Put32(&p[0], stream.size()); Put32(&p[4], Crc32(stream.data(), stream.size()));
    Put16(&p[8], 49); Put16(&p[10], 8); p[12] = kind;
    uint8_t payload[8];
    for (uint8_t i = 0; i < 8; i++) payload[i] = 0xA0 + i;
    Put32(&p[13], 5); Put32(&p[17], 8); Put32(&p[21], Crc32(payload, 8));
    return p;
}

struct Rig {
    FakeBackend backend;
    FakeEnv env;
    Port port{backend, env};
    std::vector<Reply> Send(const std::vector<uint8_t> &frames, uint32_t source = 0) {
        std::vector<uint8_t> report(frames);
        report.resize(63, 0); // HID reports are zero padded.
        uint8_t out[63];
        const size_t n = port.OnReport(report.data(), report.size(), source, out, sizeof out);
        std::vector<Reply> replies;
        Decoder d; d.Push(out, n);
        Frame f;
        while (d.Poll(f)) {
            Reply r; r.type = f.type; r.session = f.session; r.offset = f.offset; r.length = f.length;
            memcpy(r.payload, f.payload, f.length < 4 ? f.length : 4);
            replies.push_back(r);
        }
        return replies;
    }
    Reply One(const std::vector<uint8_t> &frame, uint32_t source = 0) {
        auto r = Send(frame, source);
        CHECK(r.size() == 1);
        return r.empty() ? Reply{} : r[0];
    }
};

static void Upload(Rig &rig, uint16_t sid, const std::vector<uint8_t> &stream, uint32_t source = 0) {
    CHECK(rig.One(MakeFrame(kBegin, sid, 0, BeginPayload(stream)), source).type == kAck);
    for (size_t off = 0; off < stream.size(); off += 20) {
        size_t n = std::min<size_t>(20, stream.size() - off);
        CHECK(rig.One(MakeFrame(kData, sid, off, {stream.begin() + off, stream.begin() + off + n}), source).type == kAck);
    }
}

static void TestHappyPath() {
    Rig rig; auto stream = Stream();
    Upload(rig, 1, stream);
    Reply v;
    int busy = 0;
    do { v = rig.One(MakeFrame(kEnd, 1, stream.size())); busy += v.type == kNak; } while (v.type == kNak && busy < 100);
    CHECK(v.type == kAck && v.payload[0] == 3);
    CHECK(rig.One(MakeFrame(kCommit, 1, stream.size())).type == kDone);
    CHECK(rig.env.reboots == 1);
    CHECK(rig.One(MakeFrame(kCommit, 1, stream.size())).type == kDone && rig.env.reboots == 1); // Reboot only once.
    CHECK(rig.env.locked == false && rig.env.unlocks == rig.env.flash_calls);
    CHECK(rig.env.touches > 0 && rig.backend.flash[5 * 65536] == 0xA0);
}

static void TestLockContention() {
    Rig rig; auto stream = Stream();
    rig.env.lock_free = false;
    Reply nak = rig.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream)));
    CHECK(nak.type == kNak && nak.payload[0] == kNakSinkBusy && !rig.port.receiver().active() && rig.env.flash_calls == 0);
    rig.env.lock_free = true;
    CHECK(rig.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream))).type == kAck);
    rig.env.lock_free = false;
    Reply dn = rig.One(MakeFrame(kData, 1, 0, {kRecordErase, 5, 0}));
    CHECK(dn.type == kNak && rig.port.receiver().next_offset() == 0); // Frame not consumed.
    rig.env.lock_free = true;
    CHECK(rig.One(MakeFrame(kData, 1, 0, {kRecordErase, 5, 0})).type == kAck);
}

static void TestOwnership() {
    auto stream = Stream();
    { Rig rig; rig.env.owner = false;
      Reply e = rig.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream)));
      CHECK(e.type == kError && e.payload[0] == kErrBadState && rig.backend.begins_seen == 0 && rig.env.flash_calls == 0); }
    { Rig rig; rig.env.stage_active = false;
      CHECK(rig.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream))).type == kError); }
    { Rig rig; rig.env.owner_source = 7; // Opened by Bluetooth session 7, not USB.
      CHECK(rig.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream)), 0).type == kError);
      CHECK(rig.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream)), 7).type == kAck); }
    { Rig rig; rig.backend.kind = 9; // Wrong kind for the staged update.
      CHECK(rig.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream))).type == kError); }
}

static void TestSourceIsolation() {
    Rig rig; auto stream = Stream();
    rig.env.owner_source = 7;
    CHECK(rig.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream)), 7).type == kAck);
    // Another connection cannot drive, abort or finish the session.
    Reply d = rig.One(MakeFrame(kData, 1, 0, {kRecordErase, 5, 0}), 0);
    CHECK(d.type == kError && d.payload[0] == kErrBusy);
    CHECK(rig.One(MakeFrame(kAbort, 1, 0), 0).type == kError && rig.port.receiver().active() && rig.backend.aborts == 0);
    CHECK(rig.One(MakeFrame(kData, 1, 0, {kRecordErase, 5, 0}), 7).type == kAck);
    // Half a frame from one source never combines with bytes from another.
    auto f = MakeFrame(kData, 1, 3, {kRecordWrite});
    uint8_t out[63];
    CHECK(rig.port.OnReport(f.data(), 7, 7, out, sizeof out) == 0);
    auto g = MakeFrame(kData, 1, 3, {kRecordWrite});
    uint8_t padded[63] = {}; memcpy(padded, g.data(), g.size());
    CHECK(rig.port.OnReport(padded, sizeof padded, 7, out, sizeof out) > 0);
}

static void TestStaleSessionsAndEpoch() {
    Rig rig; auto stream = Stream();
    rig.env.owner_source = 0;
    CHECK(rig.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream))).type == kAck);
    // Idle timeout: a new BEGIN (even from another connection) first expires the old session.
    rig.env.now += 6000; rig.env.owner_source = 9;
    rig.backend.active = true; rig.backend.epoch++; // Manager re-opened a staged update on the console (the expired one aborted below).
    CHECK(rig.One(MakeFrame(kBegin, 2, 0, BeginPayload(stream)), 9).type == kAck);
    CHECK(rig.backend.aborts == 0); // The old session lost its update (epoch changed): it must not abort the new one.

    // The update is lost and a newer one begins on the console; the old session must not touch it.
    Rig lost; lost.env.owner_source = 0;
    CHECK(lost.One(MakeFrame(kBegin, 1, 0, BeginPayload(stream))).type == kAck);
    lost.backend.epoch++; lost.backend.active = true; // legacy abort + new begin
    Reply e = lost.One(MakeFrame(kData, 1, 0, {kRecordErase, 5, 0}));
    CHECK(e.type == kError && e.payload[0] == kErrSink);
    CHECK(lost.backend.aborts == 0 && lost.backend.active); // The newer update is left alone.
}

static void TestCommitFailureAndNoise() {
    Rig rig; auto stream = Stream();
    rig.backend.commit_result = StagedCommit::kQuarantined;
    Upload(rig, 1, stream);
    Reply v;
    do { v = rig.One(MakeFrame(kEnd, 1, stream.size())); } while (v.type == kNak);
    Reply c = rig.One(MakeFrame(kCommit, 1, stream.size()));
    CHECK(c.type == kError && c.payload[0] == kErrCommit && rig.port.receiver().state() == State::kFailed);
    CHECK(rig.env.reboots == 1); // Ambiguous commit still reopens, like the console path.

    Rig noise; uint8_t out[63]; uint8_t junk[63];
    for (int i = 0; i < 63; i++) junk[i] = i * 7 + 1;
    junk[0] = kMagic; // Magic with garbage behind it.
    CHECK(noise.port.OnReport(junk, 63, 0, out, sizeof out) == 0 && noise.env.flash_calls == 0);
    auto bad = MakeFrame(kBegin, 1, 0, BeginPayload(stream)); bad[12] ^= 1;
    bad.resize(63, 0);
    CHECK(noise.port.OnReport(bad.data(), 63, 0, out, sizeof out) == 0 && !noise.port.receiver().active());
}

static void TestTwoFramesOneReport() {
    Rig rig; auto stream = Stream();
    auto begin = MakeFrame(kBegin, 1, 0, BeginPayload(stream));
    auto data = MakeFrame(kData, 1, 0, {kRecordErase, 5, 0});
    std::vector<uint8_t> both(begin); both.insert(both.end(), data.begin(), data.end());
    CHECK(both.size() <= 63);
    auto replies = rig.Send(both);
    CHECK(replies.size() == 2 && replies[0].type == kAck && replies[1].type == kAck && replies[1].offset == 3);
}

int main() {
    TestHappyPath();
    TestLockContention();
    TestOwnership();
    TestSourceIsolation();
    TestStaleSessionsAndEpoch();
    TestCommitFailureAndNoise();
    TestTwoFramesOneReport();
    if (failures) { printf("%d FAILURES\n", failures); return 1; }
    printf("PASS transfer port\n");
    return 0;
}
