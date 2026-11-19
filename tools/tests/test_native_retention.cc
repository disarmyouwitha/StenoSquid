#include "native_store_fixture.h"
#include <iostream>
using Work = NativeStore<NativeDevice>::Work;
void Edit(NativeFixture &f, uint32_t key, const char *text) {
    assert(f.Edit(1, key, text) == LiveResult::Ok);
}
std::string Text(NativeFixture &f, uint32_t key, const LiveView *view = nullptr) {
    RecordView record;
    const auto result = view ? f.store.live.FindSnapshot(*view, 1, &key, 1, record)
                             : f.store.live.FindLive(1, &key, 1, record);
    assert(result == LiveResult::Ok && record.operation == Operation::Set);
    return std::string(reinterpret_cast<const char *>(f.nor.data.data() + record.text_address),
                       record.text_bytes);
}
void Checkpoint(NativeFixture &f) {
    assert(f.store.Start(Work::Checkpoint) == JobResult::More);
    f.Finish();
}
void ReservePool(NativeFixture &f) {
    const auto &root = f.store.selected.root;
    std::vector<uint8_t> payload(root.bytes + 24, 0);
    assert(f.device.Read(root.address, payload.data(), root.bytes));
    payload[6] = 2;
    // A one-byte synthetic read-only base reserves the rest of the pool. Its
    // content is CRC-verified, while keeping this staging-pressure test small.
    payload[32 + 96 + 8] = 1;
    payload[32 + 96 + 9] = 0;
    auto *row = payload.data() + root.bytes;
    row[0] = 11;
    row[2] = 244;
    PutWord(row + 4, 1);
    PutWord(row + 8, Crc(0, f.nor.data.data() + 11 * 65536, 1));
    PutWord(row + 12, 1);
    CatalogSelection next;
    assert(f.store.catalog.Publish(f.store.selected, payload.size(), Source{payload}, f.Check(),
                                   next) == CatalogResult::Ok);
    f.Open();
}
void Baseline(NativeFixture &f) {
    ReservePool(f);
    Edit(f, 1, "one");
    Checkpoint(f);
    Edit(f, 2, "two");
    Checkpoint(f);
    Edit(f, 3, "three");
}
void capacity_and_reuse() {
    NativeFixture f;
    Baseline(f);
    NativeCheckpointJob<NativeDevice> without_retirement(f.device, f.store.catalog, f.store.live);
    const auto bytes = f.nor.data;
    assert(without_retirement.Begin(f.store.selected) == JobResult::More);
    JobResult result;
    do {
        result = without_retirement.Step(f.Check());
    } while (result == JobResult::More);
    assert(result == JobResult::NoSpace && f.nor.data == bytes);
    const auto generation = f.store.selected.root.generation, sequence = f.store.live.Highwater();
    Checkpoint(f);
    assert(f.store.selected.root.generation == generation + 2 &&
           f.store.live.Highwater() == sequence);
    f.Open();
    assert(Text(f, 1) == "one" && Text(f, 2) == "two" && Text(f, 3) == "three");
    for (unsigned round = 0; round < 24; ++round) {
        auto text = std::to_string(round);
        Edit(f, 3, text.c_str());
        Checkpoint(f);
        f.Open();
        assert(Text(f, 3) == text);
    }
    // The entire synthetic base remains untouched by every index reuse.
    assert(!std::memcmp(bytes.data() + 11 * 65536, f.nor.data.data() + 11 * 65536, 244 * 65536));
    std::cout << "Native retention: checkpoint succeeds with space for two index pairs where "
                 "three-pair staging refuses; 24 reuse/reboot rounds passed\n";
}
void pinned_slots() {
    NativeFixture f;
    Baseline(f);
    CatalogRoot alternate;
    assert(f.store.catalog.Inspect(uint8_t(1 - f.store.selected.slot), alternate) ==
           CatalogResult::Ok);
    uint32_t old_token = 0;
    assert(f.store.catalog.AcquirePin(alternate, old_token));
    auto bytes = f.nor.data;
    assert(f.store.Start(Work::Checkpoint) == JobResult::More);
    LiveView during_prepare;
    assert(f.store.Snapshot(during_prepare));
    assert(Text(f, 3, &during_prepare) == "three");
    assert(f.store.Release(during_prepare));
    assert(f.store.Step(f.Check()) == JobResult::Busy && f.nor.data == bytes && f.store.opened);
    assert(f.store.catalog.ReleasePin(old_token));
    LiveView source_reader;
    assert(f.store.Snapshot(source_reader));
    const auto previous = f.store.selected.root.generation;
    assert(f.store.Start(Work::Checkpoint) == JobResult::More);
    assert(f.store.Step(f.Check()) == JobResult::Busy && f.store.opened);
    assert(f.store.selected.root.generation == previous + 1 &&
           Text(f, 3, &source_reader) == "three");
    assert(f.store.live.Root().generation == f.store.selected.root.generation);
    assert(!std::memcmp(bytes.data() + 2 * 65536, f.nor.data.data() + 2 * 65536, 254 * 65536));
    // A metadata-only mirror is durable even if the following stage is blocked.
    assert(f.store.Release(source_reader));
    Checkpoint(f);
    f.Open();
    assert(Text(f, 3) == "three");
    // With identical ownership in both copies, rollover needs just its own commit.
    const auto g = f.store.selected.root.generation;
    assert(f.store.Start(Work::Rollover) == JobResult::More);
    f.Finish();
    assert(f.store.selected.root.generation == g + 1); // No retirement needed for one free journal.
    f.Open();
    assert(Text(f, 3) == "three");
    std::cout << "Native retention: old/source reader refusal, zero staging writes while blocked, "
                 "durable mirror selection and rollover passed\n";
}
void spacious_no_extra_wear() {
    NativeFixture f;
    for (unsigned i = 1; i < 8; ++i) {
        Edit(f, i, "ordinary edit");
        const auto generation = f.store.selected.root.generation;
        Checkpoint(f);
        assert(f.store.selected.root.generation == generation + 1);
        f.Open();
        assert(Text(f, i) == "ordinary edit");
    }
    std::cout << "Native retention: no extra catalog commits when staging already fits\n";
}
void ambiguous_commit() {
    NativeFixture f;
    Baseline(f);
    f.nor.fail_after_commit = true;
    assert(f.store.Start(Work::Checkpoint) == JobResult::More);
    assert(f.store.Step(f.Check()) == JobResult::RecoveryRequired && !f.store.opened);
    f.nor.fail_read = false;
    f.Open();
    assert(Text(f, 3) == "three");
    Checkpoint(f);
    f.Open();
    assert(Text(f, 3) == "three");
}
void exact_mirror_crc() {
    NativeFixture f;
    Baseline(f);
    const auto current = f.store.selected;
    std::vector<uint8_t> changed(current.root.bytes);
    assert(f.device.Read(current.root.address, changed.data(), changed.size()));
    changed[32 + 4] = 0; // Schema-valid flags, but no longer an exact mirror.
    const auto before = f.nor.data;
    CatalogSelection output;
    assert(f.store.catalog.Publish(current, changed.size(), Source{changed}, f.Check(), output,
                                   &current.root.crc) == CatalogResult::Corrupt);
    assert(!std::memcmp(before.data() + current.slot * 65536,
                        f.nor.data.data() + current.slot * 65536, 65536));
    f.Open();
    assert(f.store.selected.root.generation == current.root.generation && Text(f, 3) == "three");
}
void power_cuts() {
    NativeFixture dry;
    Baseline(dry);
    const auto baseline = dry.nor;
    const auto sequence = dry.store.live.Highwater();
    dry.nor.operations = 0;
    Checkpoint(dry);
    const unsigned operations = dry.nor.operations;
    unsigned cases = 0;
    for (unsigned point = 1; point <= operations; ++point)
        for (unsigned tear : {0u, 1u, 3u, 127u, 4095u}) {
            Nor interrupted;
            {
                NativeFixture running;
                running.nor = baseline;
                running.Open();
                running.nor.operations = 0;
                running.nor.cut = point;
                running.nor.tear = tear;
                try {
                    Checkpoint(running);
                    assert(false);
                } catch (const Cut &) {
                }
                interrupted = running.nor;
            }
            NativeFixture boot;
            interrupted.cut = 0;
            boot.nor = interrupted;
            const auto bytes = boot.nor.data;
            const auto opened =
                boot.store.Open([&](const CatalogRoot &r) { return boot.validator.Validate(r); });
            assert(opened == CatalogResult::Ok || opened == CatalogResult::Quarantined);
            assert(boot.nor.data == bytes && boot.store.live.Highwater() == sequence);
            assert(Text(boot, 1) == "one" && Text(boot, 2) == "two" && Text(boot, 3) == "three");
            assert(!std::memcmp(baseline.data.data() + 2 * 65536, boot.nor.data.data() + 2 * 65536,
                                65536));
            if (opened == CatalogResult::Quarantined)
                assert(boot.Edit(1, 3, "refused") == LiveResult::ReadOnly);
            else {
                Edit(boot, 4, "after cut");
                boot.Open();
                assert(Text(boot, 4) == "after cut");
            }
            ++cases;
        }
    std::cout << "Native retention: " << cases
              << " torn mirror/index/catalog operations preserve committed edits and read-only "
                 "recovery\n";
}
int main() {
    capacity_and_reuse();
    pinned_slots();
    spacious_no_extra_wear();
    ambiguous_commit();
    exact_mirror_crc();
    power_cuts();
}
