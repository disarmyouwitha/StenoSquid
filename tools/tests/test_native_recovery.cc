#include "native_stage_fixture.h"
#include <iostream>

void CheckState(NativeStore<NativeDevice> &store, NativeUpdateKind action, bool changed) {
    uint8_t row[96];
    assert(store.Row(1, row));
    const bool deleted = changed && action == NativeUpdateKind::SnapshotDelete;
    const bool consolidated = changed && action == NativeUpdateKind::SnapshotConsolidate;
    const bool renamed = changed && action == NativeUpdateKind::Rename;
    if (deleted) {
        assert(Word(row) == 2 && !row[11]);
    } else {
        assert(Word(row) == 8 && row[11] == kUserSnapshot && Word(row + 4) == 1);
        assert(row[12] == (renamed ? 7 : 5));
        assert(!std::memcmp(row + 16, renamed ? "Renamed" : "Smoke", row[12]));
    }
    RecordView record;
    const uint32_t kat = 524552, bob = 66096, pinned = 6;
    if (!deleted) {
        const auto expected = consolidated ? LiveResult::Missing : LiveResult::Ok;
        assert(store.live.FindLive(8, &kat, 1, record) == expected);
        if (expected == LiveResult::Ok) assert(record.operation == Operation::Set && record.text_bytes == 7);
        assert(store.live.FindLive(8, &bob, 1, record) == expected);
        if (expected == LiveResult::Ok) assert(record.operation == Operation::Remove);
    }
    assert(store.live.FindLive(2, &pinned, 1, record) == LiveResult::Ok);
    assert(record.operation == Operation::Set && record.text_bytes == 13);
}

void CheckMergeState(NativeStore<NativeDevice> &store, bool changed) {
    uint8_t row[96];
    assert(store.Row(1, row) && Word(row) == 9 && row[11] == kUserSnapshot);
    assert(store.Row(2, row) && Word(row) == (changed ? 2u : 8u));
    const uint32_t pinned = 6;
    RecordView record;
    assert(store.live.FindLive(2, &pinned, 1, record) == LiveResult::Ok);
    assert(record.operation == Operation::Set && record.text_bytes == 13);
}

void CheckCuts(const std::vector<uint8_t> &contents, const Plan &plan, NativeUpdateKind action, bool unsafe) {
    const bool merging = action == NativeUpdateKind::SnapshotMerge;
    unsigned operations = 0;
    for (unsigned cut = 0; cut <= operations; ++cut) {
        for (const unsigned tear : {1u, 4095u}) {
            Nor nor;
            nor.data = contents;
            nor.cut = cut;
            nor.tear = tear;
            NativeDevice device{{nor}};
            NativeStore<NativeDevice> store(device);
            NativeValidator<NativeDevice> validator(device, store.catalog);
            auto open = [&] { return store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }); };
            assert(open() == CatalogResult::Ok);
            if (merging) CheckMergeState(store, false);
            else CheckState(store, action, false);
            NativeStage<NativeDevice> stage(store, validator);
            const uint32_t target = merging ? 9 : action == NativeUpdateKind::Rename ? 0 : 8;
            assert(stage.Begin(action, 16, target, merging ? (unsafe ? 2 : 8) : 0));
            try {
                Upload(stage, plan);
                bool reopen = false;
                auto probe = +[](const uint8_t *extent, const uint8_t *row) {
                    return Word(extent + 12) == 1 && (Word(row) == 8 || Word(row) == 9) && row[11] == kUserSnapshot;
                };
                const auto status = stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen, probe);
                assert(!cut && reopen == !unsafe && status == (unsafe ? CatalogResult::Corrupt : CatalogResult::Ok));
                if (!unsafe) operations = std::max(operations, nor.operations);
            } catch (const Cut &) {
                assert(cut);
            }
            nor.cut = 0;
            stage.Abort();
            const auto result = open();
            assert(result == CatalogResult::Ok || result == CatalogResult::Quarantined);
            const bool switched = store.selected.root.generation > 2;
            if (!cut) assert(switched == !unsafe);
            if (merging) CheckMergeState(store, switched);
            else CheckState(store, action, switched);
        }
    }
    std::cout << "Snapshot action " << int(action) << ": " << operations << " torn boundaries with two cuts each"
              << (unsafe ? " (refused to drop an unrelated live record)" : "") << std::endl;
}

int main(int argc, char **argv) {
    assert(argc == 8);
    std::ifstream input(argv[1], std::ios::binary);
    std::vector<uint8_t> contents(std::istreambuf_iterator<char>(input), {});
    contents.resize(256 * kSegmentBytes, 255);
    CheckCuts(contents, ReadPlan(argv[2]), NativeUpdateKind::Rename, false);
    CheckCuts(contents, ReadPlan(argv[3]), NativeUpdateKind::SnapshotDelete, false);
    CheckCuts(contents, ReadPlan(argv[4]), NativeUpdateKind::SnapshotConsolidate, false);
    CheckCuts(contents, ReadPlan(argv[5]), NativeUpdateKind::SnapshotConsolidate, true);
    std::ifstream merge_input(argv[6], std::ios::binary);
    std::vector<uint8_t> merge_contents(std::istreambuf_iterator<char>(merge_input), {});
    merge_contents.resize(256 * kSegmentBytes, 255);
    CheckCuts(merge_contents, ReadPlan(argv[7]), NativeUpdateKind::SnapshotMerge, false);
    CheckCuts(merge_contents, ReadPlan(argv[7]), NativeUpdateKind::SnapshotMerge, true);
}
