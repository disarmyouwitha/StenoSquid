// Upload high-address plans (segments >= 512, above 32 MiB) on a 64 MiB pool through
// the transfer session and firmware glue, and require flash identical to the legacy path.
// argv: initial.bin named.plan journal.plan   (same inputs as test_native_geometry)
#include "native_stage_fixture.h"
#include "../../src/steno_squid/transfer_port.h"

#include <iostream>

using namespace steno_transfer;

struct LargeDevice : NativeDevice {
    uint32_t PoolEnd() const { return kLargePoolEnd; }
};
using Stage = NativeStage<LargeDevice>;

static bool ImageOk(const uint8_t *extent, const uint8_t *row) {
    return Word(extent + 12) == 1 && row[11] == kUserSnapshot;
}

struct Backend {
    Stage &stage;
    bool Active() const { return stage.Active(); }
    uint8_t KindId() const { return static_cast<uint8_t>(stage.Kind()); }
    uint32_t Epoch() const { return 0; }
    bool Erase(uint32_t s) { return stage.Erase(s); }
    bool Write(uint32_t a, const uint8_t *d, uint32_t n) { return stage.Write(a, d, n); }
    bool Read(uint32_t a, uint8_t *o, uint32_t n) { return stage.Read(a, o, n); }
    StagedCommit Commit(uint32_t seg, uint32_t bytes, uint32_t crc, bool &reopen) {
        switch (stage.Commit(seg, bytes, crc, reopen, +ImageOk)) {
        case CatalogResult::Ok: return StagedCommit::kOk;
        case CatalogResult::Busy: return StagedCommit::kBusy;
        case CatalogResult::Quarantined: return StagedCommit::kQuarantined;
        default: return StagedCommit::kRefused;
        }
    }
    void Abort() { stage.Abort(); }
};

struct Env {
    bool TryService() { return true; }
    void Unlock() {}
    bool StageActive() { return true; }
    bool Owns(uint32_t) { return true; }
    void Touch() {}
    uint32_t Now() { return 0; }
    template <class F> void WithFlash(F f) { f(); }
};

static std::vector<uint8_t> MakeFrame(uint8_t type, uint16_t session, uint32_t offset, const uint8_t *p = nullptr, size_t n = 0) {
    std::vector<uint8_t> out(kMaxFrameBytes);
    out.resize(Encode(type, session, offset, p, n, out.data(), out.size()));
    return out;
}

static std::vector<uint8_t> Records(const Plan &plan) {
    std::vector<uint8_t> s;
    for (const auto &[segment, block] : plan.blocks) {
        s.push_back(kRecordErase); s.push_back(segment); s.push_back(segment >> 8);
        for (size_t off = 0; off < block.size(); off += 256) {
            const size_t n = std::min<size_t>(256, block.size() - off);
            const auto *start = block.data() + off;
            if (std::all_of(start, start + n, [](uint8_t v) { return v == 255; })) continue;
            uint8_t h[7] = {kRecordWrite};
            Put32(h + 1, segment * kSegmentBytes + off);
            Put16(h + 5, n);
            s.insert(s.end(), h, h + 7);
            s.insert(s.end(), start, start + n);
        }
    }
    return s;
}

// Returns true if the session reached DONE; `refused` reports an early, side-effect-free refusal.
static bool Transfer(Stage &stage, const Plan &plan, bool &refused) {
    Backend backend{stage};
    Env env;
    FirmwarePort<Backend, Env> port(backend, env);
    const auto stream = Records(plan);
    uint8_t begin[13 + kStagedArgsBytes];
    Put32(begin, stream.size()); Put32(begin + 4, Crc32(stream.data(), stream.size()));
    Put16(begin + 8, 49); Put16(begin + 10, 8); begin[12] = static_cast<uint8_t>(stage.Kind());
    Put32(begin + 13, plan.metadata); Put32(begin + 17, plan.bytes); Put32(begin + 21, plan.crc);
    auto exchange = [&](const std::vector<uint8_t> &frame) {
        uint8_t report[63] = {}, out[63];
        memcpy(report, frame.data(), frame.size());
        const size_t n = port.OnReport(report, sizeof report, 0, out, sizeof out);
        steno_transfer::Decoder d; d.Push(out, n);
        steno_transfer::Frame f;
        if (!n) return uint8_t(0); // Silent: a cumulative acknowledgement is not due.
        assert(d.Poll(f));
        return f.type;
    };
    refused = exchange(MakeFrame(steno_transfer::kBegin, 1, 0, begin, sizeof begin)) != steno_transfer::kAck;
    if (refused) return false;
    for (size_t off = 0; off < stream.size(); off += 49) {
        const size_t n = std::min<size_t>(49, stream.size() - off);
        // Cumulative acknowledgements: silence inside a group, never a NAK or error, and the last frame is acked.
        const auto reply = exchange(MakeFrame(steno_transfer::kData, 1, off, stream.data() + off, n));
        if (reply != 0 && reply != steno_transfer::kAck) return false;
        if (off + n == stream.size() && reply != steno_transfer::kAck) return false;
    }
    for (int i = 0; i < 1000; i++) {
        const auto r = exchange(MakeFrame(steno_transfer::kEnd, 1, stream.size()));
        if (r == steno_transfer::kAck) break;
        if (r != steno_transfer::kNak) return false;
    }
    return exchange(MakeFrame(steno_transfer::kCommit, 1, stream.size())) == steno_transfer::kDone;
}

struct Rig {
    Nor nor;
    LargeDevice device{{{nor}}};
    NativeStore<LargeDevice> store{device};
    NativeValidator<LargeDevice> validator{device, store.catalog};
    Stage stage{store, validator};
    explicit Rig(const std::vector<uint8_t> &image) {
        nor.data = image;
        Open();
    }
    void Open() {
        assert(store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }) == CatalogResult::Ok);
    }
};

int main(int argc, char **argv) {
    assert(argc == 4);
    std::ifstream source(argv[1], std::ios::binary);
    const std::vector<uint8_t> image((std::istreambuf_iterator<char>(source)), {});
    assert(image.size() == kLargePoolEnd * kSegmentBytes);

    Rig legacy(image), session(image);
    for (unsigned index = 2; index < 4; ++index) {
        const bool named = index == 2;
        const auto kind = named ? NativeUpdateKind::SnapshotNamed : NativeUpdateKind::Personal;
        const Plan plan = ReadPlan(argv[index]);
        assert(plan.metadata >= (named ? 512u : 768u));

        assert(legacy.stage.Begin(kind, 16));
        for (const auto &[segment, block] : plan.blocks) {
            assert(legacy.stage.Erase(segment));
            for (unsigned off = 0; off < block.size(); off += 512) {
                const auto n = std::min(size_t(512), block.size() - off);
                const auto *d = block.data() + off;
                if (std::all_of(d, d + n, [](uint8_t v) { return v == 255; })) continue;
                assert(legacy.stage.Write(segment * kSegmentBytes + off, d, n));
            }
        }
        bool reopen = false;
        assert(legacy.stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen, +ImageOk) == CatalogResult::Ok);
        legacy.stage.Abort();
        legacy.Open();

        assert(session.stage.Begin(kind, 16));
        bool refused = false;
        const bool done = Transfer(session.stage, plan, refused);
        if (!done) {
            std::cout << (refused ? "REFUSED at BEGIN (high segment): the Manager would fall back to Base64\n"
                                  : "FAILED mid-transfer\n");
            return 1;
        }
        session.stage.Abort();
        session.Open();
        assert(session.nor.data == legacy.nor.data);
    }
    std::cout << "PASS: 64 MiB high-address uploads (segments >= 512) identical to the legacy path\n";
}
