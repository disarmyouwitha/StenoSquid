// Device simulator for the Manager-side transfer client tests.
//
// Hosts the REAL NativeStage on simulated NOR behind the real FirmwarePort, and
// speaks 63-byte HID reports over stdin/stdout so Python can drive it exactly as
// it would drive the keyboard.
//   argv: before.bin plan.bin kind
//   stdin  messages: 'R' + 63 bytes (an output report) | 'E' (finish)
//   stdout messages: 'R' + 63 bytes (an input report)  | 'N' (no reply) ; for 'E':
//                    "MATCH <generation>" or "DIFF" or "FAIL" + newline
// On 'E' the result is compared with the legacy erase/write/commit path.
#include "native_stage_fixture.h"
#include "../../src/steno_squid/transfer_port.h"

#include <cstdio>
#include <unistd.h>

using namespace steno_transfer;

struct StageBackend {
    NativeStage<NativeDevice> &stage;
    bool Active() const { return stage.Active(); }
    uint8_t KindId() const { return static_cast<uint8_t>(stage.Kind()); }
    uint32_t Epoch() const { return 0; }
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

struct Env {
    bool TryService() { return true; }
    void Unlock() {}
    bool StageActive() { return true; }
    bool Owns(uint32_t) { return true; }
    void Touch() {}
    uint32_t Now() { return now_ms++; }
    template <class F> void WithFlash(F f) { if (f()) rebooted = true; }
    uint32_t now_ms = 0;
    bool rebooted = false;
};

struct Rig {
    Nor nor;
    NativeDevice dev{{nor}};
    NativeStore<NativeDevice> store{dev};
    NativeValidator<NativeDevice> val{dev, store.catalog};
    NativeStage<NativeDevice> stage{store, val};
    explicit Rig(const std::vector<uint8_t> &image) {
        nor.data = image; nor.operations = 0;
        assert(store.Open([&](const CatalogRoot &r) { return val.Validate(r); }) == CatalogResult::Ok);
    }
};

static bool ReadFully(void *buffer, size_t size) {
    auto *p = static_cast<uint8_t *>(buffer);
    while (size) {
        const ssize_t n = read(0, p, size);
        if (n <= 0) return false;
        p += n; size -= n;
    }
    return true;
}

int main(int argc, char **argv) {
    assert(argc == 4);
    std::ifstream file(argv[1], std::ios::binary);
    std::vector<uint8_t> image((std::istreambuf_iterator<char>(file)), {});
    image.resize(256 * kSegmentBytes, 0x42);
    const Plan plan = ReadPlan(argv[2]);
    const auto kind = static_cast<NativeUpdateKind>(atoi(argv[3]));

    Rig rig(image);
    assert(rig.stage.Begin(kind));
    StageBackend backend{rig.stage};
    Env env;
    FirmwarePort<StageBackend, Env> port(backend, env);

    for (;;) {
        char tag;
        if (!ReadFully(&tag, 1)) return 1;
        if (tag == 'R') {
            uint8_t report[63], out[63] = {};
            if (!ReadFully(report, sizeof report)) return 1;
            const size_t n = port.OnReport(report, sizeof report, 0, out, sizeof out);
            if (n) { putchar('R'); fwrite(out, 1, 63, stdout); } else putchar('N');
            fflush(stdout);
        } else if (tag == 'E') {
            Rig ref(image);
            assert(ref.stage.Begin(kind));
            Upload(ref.stage, plan);
            bool reopen = false;
            assert(ref.stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen) == CatalogResult::Ok);
            const bool same = rig.nor.data == ref.nor.data;
            NativeStore<NativeDevice> cold(rig.dev);
            NativeValidator<NativeDevice> cv(rig.dev, cold.catalog);
            const auto result = cold.Open([&](const CatalogRoot &r) { return cv.Validate(r); });
            if (same && env.rebooted && result == CatalogResult::Ok)
                printf("MATCH %u\n", unsigned(cold.selected.root.generation));
            else
                printf("%s\n", same ? "FAIL" : "DIFF");
            fflush(stdout);
            return 0;
        } else {
            return 2;
        }
    }
}
