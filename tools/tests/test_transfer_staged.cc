// Drive the REAL NativeStage on simulated NOR through the transfer session and
// StagedSink, and require the same result as the legacy erase/write/commit path.
// argv: before.bin import-plan.bin compact-plan.bin
#include "native_stage_fixture.h"
#include "../../src/steno_squid/transfer_staged_sink.h"
#include <iostream>

using namespace steno_transfer;

struct StageBackend {
    NativeStage<NativeDevice> &stage;
    bool Active() const { return stage.Active(); }
    uint8_t KindId() const { return static_cast<uint8_t>(stage.Kind()); }
    bool Erase(uint32_t s) { return stage.Erase(s); }
    bool Write(uint32_t a, const uint8_t *d, uint32_t n) { return stage.Write(a, d, n); }
    bool Read(uint32_t a, uint8_t *o, uint32_t n) { return stage.Read(a, o, n); }
    StagedCommit Commit(uint32_t seg, uint32_t bytes, uint32_t crc, bool &reopen) {
        switch (stage.Commit(seg, bytes, crc, reopen)) {
        case CatalogResult::Ok: return StagedCommit::kOk;
        case CatalogResult::Busy: return StagedCommit::kBusy;
        case CatalogResult::Quarantined: return StagedCommit::kQuarantined;
        default: return StagedCommit::kRefused;
        }
    }
    void Abort() { stage.Abort(); }
};

static std::vector<uint8_t> MakeFrame(uint8_t type, uint16_t session, uint32_t offset, const uint8_t *p = nullptr, size_t n = 0) {
    std::vector<uint8_t> out(kMaxFrameBytes);
    out.resize(Encode(type, session, offset, p, n, out.data(), out.size()));
    assert(!out.empty());
    return out;
}

// Record stream for a plan, in the same order Upload() uses: erase, then non-blank 256-byte writes.
static std::vector<uint8_t> Records(const Plan &plan) {
    std::vector<uint8_t> s;
    for (const auto &[segment, block] : plan.blocks) {
        s.push_back(kRecordErase); s.push_back(segment); s.push_back(segment >> 8);
        for (size_t off = 0; off < block.size(); off += 256) {
            const size_t n = std::min<size_t>(256, block.size() - off);
            const auto *start = block.data() + off;
            if (std::all_of(start, start + n, [](uint8_t v) { return v == 255; })) continue;
            assert(n % 4 == 0);
            uint8_t h[7] = {kRecordWrite};
            Put32(h + 1, segment * kSegmentBytes + off);
            Put16(h + 5, n);
            s.insert(s.end(), h, h + 7);
            s.insert(s.end(), start, start + n);
        }
    }
    return s;
}

struct Outcome { bool committed; State state; bool reopen; bool stage_active; uint8_t error = 0; };

// Full session over an in-memory link. `commit_crc` lets tests lie about the image.
static Outcome Run(NativeStage<NativeDevice> &stage, const Plan &plan, uint32_t commit_crc, size_t truncate = 0,
                   bool disconnect_midway = false) {
    StageBackend backend{stage};
    StagedSink<StageBackend> sink(backend);
    Receiver receiver(sink, 1u << 30);
    auto stream = Records(plan);
    if (truncate) stream.resize(stream.size() - truncate);
    uint8_t begin[13 + kStagedArgsBytes];
    Put32(begin, stream.size()); Put32(begin + 4, Crc32(stream.data(), stream.size()));
    Put16(begin + 8, 49); Put16(begin + 10, 8); begin[12] = static_cast<uint8_t>(stage.Kind());
    Put32(begin + 13, plan.metadata); Put32(begin + 17, plan.bytes); Put32(begin + 21, commit_crc);
    Decoder decoder;
    uint32_t now = 0;
    auto send = [&](const std::vector<uint8_t> &bytes) {
        decoder.Push(bytes.data(), bytes.size());
        steno_transfer::Frame f; Reply last{};
        while (decoder.Poll(f)) last = receiver.Handle(f, now++);
        return last;
    };
    if (send(MakeFrame(steno_transfer::kBegin, 1, 0, begin, sizeof begin)).type != steno_transfer::kAck) return {false, receiver.state(), false, stage.Active()};
    for (size_t off = 0; off < stream.size(); off += 49) {
        const size_t n = std::min<size_t>(49, stream.size() - off);
        const Reply r = send(MakeFrame(steno_transfer::kData, 1, off, stream.data() + off, n));
        if (r.type != steno_transfer::kAck) return {false, receiver.state(), false, stage.Active()};
        if (disconnect_midway && off > stream.size() / 2) {
            receiver.Disconnect();
            return {false, receiver.state(), false, stage.Active()};
        }
    }
    Reply v;
    for (int i = 0; i < 1000; i++) { // END is retried while the sink verifies in slices.
        v = send(MakeFrame(steno_transfer::kEnd, 1, stream.size()));
        if (v.type != steno_transfer::kNak) break;
        assert(v.payload[0] == steno_transfer::kNakSinkBusy);
    }
    if (v.type != steno_transfer::kAck) return {false, receiver.state(), false, stage.Active(), receiver.last_error()};
    Reply d = send(MakeFrame(steno_transfer::kCommit, 1, stream.size()));
    return {d.type == kDone, receiver.state(), sink.reopen_required(), stage.Active()};
}

struct Rig {
    Nor nor;
    NativeDevice dev{{nor}};
    NativeStore<NativeDevice> store{dev};
    NativeValidator<NativeDevice> val{dev, store.catalog};
    NativeStage<NativeDevice> stage{store, val};
    explicit Rig(const std::vector<uint8_t> &image, unsigned cut = 0) {
        nor.data = image; nor.operations = 0; nor.cut = cut; nor.tear = 1;
        assert(store.Open([&](const CatalogRoot &r) { return val.Validate(r); }) == CatalogResult::Ok);
    }
};

static void Equivalent(const std::vector<uint8_t> &image, const Plan &plan, NativeUpdateKind kind, const char *label) {
    // Reference: the legacy path.
    Rig ref(image);
    assert(ref.stage.Begin(kind));
    Upload(ref.stage, plan);
    bool reopen = false;
    assert(ref.stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen) == CatalogResult::Ok);
    const unsigned total_ops = ref.nor.operations;

    // Session path must leave byte-identical flash.
    Rig rig(image);
    assert(rig.stage.Begin(kind));
    const Outcome ok = Run(rig.stage, plan, plan.crc);
    assert(ok.committed && ok.state == State::kCommitted && ok.reopen);
    assert(rig.nor.data == ref.nor.data);

    // Every torn-write point: a cold open must succeed (or quarantine), and DONE implies a new root.
    for (unsigned cut = 1; cut <= total_ops + 8; ++cut) {
        Rig torn(image, cut);
        assert(torn.stage.Begin(kind));
        bool completed = false;
        try { completed = Run(torn.stage, plan, plan.crc).committed; } catch (const Cut &) {}
        torn.nor.cut = 0;
        NativeStore<NativeDevice> cold(torn.dev);
        NativeValidator<NativeDevice> cv(torn.dev, cold.catalog);
        const auto result = cold.Open([&](const CatalogRoot &r) { return cv.Validate(r); });
        assert(result == CatalogResult::Ok || result == CatalogResult::Quarantined);
        if (completed) assert(cold.selected.root.generation > 2);
    }

    { // Wrong commit CRC is caught by the sink's flash readback; stage released; catalog untouched.
        Rig r(image);
        assert(r.stage.Begin(kind));
        const auto generation = r.store.selected.root.generation;
        const Outcome o = Run(r.stage, plan, plan.crc ^ 1);
        assert(!o.committed && o.state == State::kFailed && !o.stage_active);
        assert(o.error == kErrVerify); // Caught by readback at END, before Commit was ever attempted.
        assert(r.store.selected.root.generation == generation);
    }
    { // Truncated stream never verifies or commits.
        Rig r(image);
        assert(r.stage.Begin(kind));
        const Outcome o = Run(r.stage, plan, plan.crc, 100);
        assert(!o.committed && !o.stage_active);
    }
    { // Disconnect mid-transfer releases the staged update.
        Rig r(image);
        assert(r.stage.Begin(kind));
        const Outcome o = Run(r.stage, plan, plan.crc, 0, true);
        assert(!o.committed && o.state == State::kFailed && !o.stage_active);
    }
    { // No staged update, or the wrong kind: refused, and an update it never attached to is left alone.
        Rig r(image);
        assert(!Run(r.stage, plan, plan.crc).committed);
        assert(r.stage.Begin(kind));
        StageBackend backend{r.stage};
        StagedSink<StageBackend> sink(backend);
        uint8_t args[kStagedArgsBytes] = {};
        Put32(args, plan.metadata); Put32(args + 4, plan.bytes); Put32(args + 8, plan.crc);
        assert(sink.Begin({1, 0, uint8_t(static_cast<uint8_t>(kind) + 1), args, sizeof args}) == SinkResult::kFail);
        sink.Abort();
        assert(r.stage.Active());
    }
    std::cout << label << ": session upload byte-identical to legacy path; " << total_ops + 8
              << " torn-write points safe\n";
}

int main(int argc, char **argv) {
    assert(argc == 4);
    std::ifstream file(argv[1], std::ios::binary);
    std::vector<uint8_t> image((std::istreambuf_iterator<char>(file)), {});
    image.resize(256 * kSegmentBytes, 0x42);
    Equivalent(image, ReadPlan(argv[2]), NativeUpdateKind::Personal, "Bulk import");
    Equivalent(image, ReadPlan(argv[3]), NativeUpdateKind::Compact, "Compaction");
    std::cout << "PASS staged transfer\n";
}
