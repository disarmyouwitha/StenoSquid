#include "native_stage_fixture.h"
#include <iostream>
#include <string_view>

int main(int argc, char **argv) {
    assert(argc == 3 || argc == 4);
    const bool clearing = argc == 4 && std::string_view(argv[3]) == "clear";
    const uint32_t identity = argc == 4 && !clearing ? 8 : 1;
    std::ifstream source(argv[1], std::ios::binary);
    std::vector<uint8_t> contents(std::istreambuf_iterator<char>(source), {});
    contents.resize(256 * kSegmentBytes, 255);
    const auto plan = ReadPlan(argv[2]);
    unsigned operations = 0;
    for (unsigned cut = 0; cut <= operations; ++cut) {
        for (unsigned tear : {1u, 4095u}) {
            Nor nor;
            nor.data = contents;
            nor.cut = cut;
            nor.tear = tear;
            NativeDevice device{{nor}};
            NativeStore<NativeDevice> store(device);
            NativeValidator<NativeDevice> validator(device, store.catalog);
            auto open = [&] { return store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }); };
            assert(open() == CatalogResult::Ok);
            const auto previous_generation = store.selected.root.generation;
            uint8_t original[96];
            assert(store.Row(3, original));
            NativeStage<NativeDevice> stage(store, validator);
            assert(stage.Begin(clearing ? NativeUpdateKind::SnapshotClear : NativeUpdateKind::SnapshotPersonal, 16, identity));
            try {
                Upload(stage, plan);
                bool reopen = false;
                auto probe = [](const uint8_t *extent, const uint8_t *row) {
                    return Word(extent + 12) == 1 && row[11] == kUserSnapshot;
                };
                assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen, probe) == CatalogResult::Ok);
                assert(!cut && reopen);
                operations = nor.operations;
            } catch (const Cut &) {
                assert(cut);
            }
            nor.cut = 0;
            stage.Abort();
            const auto result = open();
            assert(result == CatalogResult::Ok || result == CatalogResult::Quarantined);
            uint8_t row[96];
            assert(store.Row(3, row) && Word(row) == identity && !Word(row + 4));
            if (clearing) {
                const bool cleared = store.selected.root.generation > previous_generation;
                assert(row[11] == (cleared ? 0 : original[11]));
                if (cleared) assert(row[8] == 255 && row[9] == 255);
                RecordView record;
                const uint32_t bob = 66096, pinned = 6;
                assert(store.live.FindLive(identity, &bob, 1, record) == (cleared ? LiveResult::Missing : LiveResult::Ok));
                assert(store.live.FindLive(2, &pinned, 1, record) == LiveResult::Ok && record.operation == Operation::Set);
                if (!cut) assert(cleared && result == CatalogResult::Ok);
                continue;
            }
            const bool converted = store.selected.root.generation > previous_generation;
            assert((row[11] == kUserSnapshot) == converted);
            RecordView record;
            const uint32_t kat = 524552, bob = 66096, added = 1048836, pinned = 6;
            assert(store.live.FindLive(identity, &bob, 1, record) == LiveResult::Ok && record.operation == Operation::Remove);
            assert(store.live.FindLive(identity, &kat, 1, record) == (converted ? LiveResult::Missing : LiveResult::Ok));
            assert(store.live.FindLive(2, &pinned, 1, record) == LiveResult::Ok && record.operation == Operation::Set);
            if (!cut) {
                assert(converted && result == CatalogResult::Ok);
                const uint8_t text[] = "editable";
                assert(store.Edit(identity, 0, &added, 1, text, sizeof(text) - 1) == LiveResult::Ok);
                assert(store.Edit(identity, 0, &kat, 1, text, sizeof(text) - 1) == LiveResult::Ok);
                assert(store.Edit(identity, 1, &added, 1, text, 0) == LiveResult::Ok);
                assert(open() == CatalogResult::Ok);
                assert(store.live.FindLive(identity, &kat, 1, record) == LiveResult::Ok && record.operation == Operation::Set);
                assert(store.live.FindLive(identity, &added, 1, record) == LiveResult::Ok && record.operation == Operation::Remove);
            }
        }
    }
    if (clearing) {
        std::cout << "Personal clear preserved slot and other dictionaries through " << operations << " flash boundaries\n";
        return 0;
    }
    std::cout << (identity == 1 ? "Personal" : "Named journal")
              << " snapshot conversion: preserved ID, disabled flag, priority and tombstones through "
              << operations << " flash boundaries; continued edits passed after cold boot\n";
}
