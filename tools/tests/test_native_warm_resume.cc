#include "native_store_fixture.h"
#include "native_warm_resume.h"
#include <iostream>
void checkpoint(NativeFixture &f) {
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Checkpoint) == JobResult::More);
    f.Finish();
}
int main() {
    NativeFixture f;
    assert(NativeWarmResumeEligible(f.store));
    std::string large(2047, 'x');
    for (unsigned i = 0; i < 80; ++i) {
        for (;;) {
            auto result = f.Edit(1, i + 1, large.c_str());
            if (result == LiveResult::Ok)
                break;
            assert(result == LiveResult::Busy);
            f.Finish();
        }
    }
    checkpoint(f);
    const auto bytes = f.nor.data;
    const auto sequence = f.store.live.Highwater();
    const auto calls = f.nor.operations, read_bytes = f.nor.read_bytes;
    assert(NativeWarmResumeEligible(f.store));
    assert(f.nor.operations == calls && f.nor.data == bytes &&
           f.store.live.Highwater() == sequence);
    assert(f.nor.read_bytes - read_bytes < 4096); // Metadata only, independent of text volume.
    LiveView reader;
    assert(f.store.Snapshot(reader));
    assert(!NativeWarmResumeEligible(f.store));
    assert(f.store.Release(reader));
    assert(NativeWarmResumeEligible(f.store));
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Checkpoint) == JobResult::More);
    assert(!NativeWarmResumeEligible(f.store));
    f.Finish();
    assert(NativeWarmResumeEligible(f.store));
    LiveView scratch;
    assert(f.store.Enable(2, false, scratch, f.Check()) == CatalogResult::Ok);
    assert(NativeWarmResumeEligible(f.store)); // Saved flags and active root already retargeted.
    f.nor.fail_read = true;
    assert(!NativeWarmResumeEligible(f.store));
    f.nor.fail_read = false;
    assert(NativeWarmResumeEligible(f.store));
    auto saved = f.nor.data;
    f.nor.data[f.store.selected.root.address + 48] ^= 1;
    assert(!NativeWarmResumeEligible(f.store));
    f.nor.data = saved;
    const uint32_t alternate = (1 - f.store.selected.slot) * 65536;
    f.nor.data[alternate] ^= 1;
    assert(!NativeWarmResumeEligible(f.store));
    const auto opened =
        f.store.Open([&](const CatalogRoot &root) { return f.validator.Validate(root); });
    assert(opened == CatalogResult::Quarantined && !NativeWarmResumeEligible(f.store));
    f.nor.data = saved;
    f.Open();
    // A valid out-of-band generation is not the in-RAM selected/live root.
    const auto current = f.store.selected;
    std::vector<uint8_t> payload(current.root.bytes);
    assert(f.device.Read(current.root.address, payload.data(), payload.size()));
    CatalogSelection newer;
    assert(f.store.catalog.Publish(current, payload.size(), Source{payload}, f.Check(), newer) ==
           CatalogResult::Ok);
    assert(!NativeWarmResumeEligible(f.store));
    f.Open();
    assert(NativeWarmResumeEligible(f.store));
    // A fresh instance cannot shortcut validation merely because bytes exist.
    NativeStore<NativeDevice> fresh(f.device);
    assert(!NativeWarmResumeEligible(fresh));
    std::cout << "Native warm resume: bounded read-only catalog checks, saved flag/maintenance "
                 "transitions, pins, metadata corruption, unknown generations and cold-instance "
                 "refusal passed\n";
}
