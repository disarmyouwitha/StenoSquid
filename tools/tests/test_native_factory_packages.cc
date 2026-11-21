#include "native_stage_fixture.h"
#include <cstdlib>
#include <iostream>

static bool Probe(const uint8_t *, const uint8_t *) { return true; }

static std::vector<uint8_t> Image(const char *path) {
    std::ifstream file(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(file), {}};
}

static void Run(const std::vector<uint8_t> &image, const Plan &plan, unsigned id, bool restore) {
    unsigned operations = 0;
    for (unsigned cut = 0; cut <= operations; ++cut) {
        for (const unsigned tear : {1u, 4095u}) {
            Nor nor;
            nor.data = image;
            nor.cut = cut;
            nor.tear = tear;
            NativeDevice device{{nor}};
            NativeStore<NativeDevice> store(device);
            NativeValidator<NativeDevice> validator(device, store.catalog);
            assert(store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }) == CatalogResult::Ok);
            const uint64_t generation = store.selected.root.generation;
            const uint64_t sequence = store.live.Highwater();
            uint8_t old_row[96];
            assert(store.Row(id - 1, old_row));
            const unsigned old_extent = old_row[8] | unsigned(old_row[9]) << 8;
            unsigned old_segment = 0;
            if (!restore) {
                uint8_t extent[24];
                assert(device.Read(store.selected.root.address + 32 + 7 * 96 + old_extent * 24, extent, 24));
                old_segment = extent[0] | unsigned(extent[1]) << 8;
            }
            NativeStage<NativeDevice> stage(store, validator);
            assert(stage.Begin(NativeUpdateKind::FactoryMember, 16, id));
            bool reboot = false;
            try {
                Upload(stage, plan);
                if (!cut) {
                    LiveView pin;
                    assert(store.Snapshot(pin));
                    assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reboot, Probe) == CatalogResult::Busy);
                    assert(!reboot && store.Release(pin));
                }
                assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reboot, Probe) == CatalogResult::Ok);
                assert(!cut && reboot);
                operations = nor.operations;
            } catch (const Cut &) {
                assert(cut);
            }
            nor.cut = 0;
            NativeStore<NativeDevice> cold(device);
            NativeValidator<NativeDevice> recovered(device, cold.catalog);
            const auto result = cold.Open([&](const CatalogRoot &root) { return recovered.Validate(root); });
            assert(result == CatalogResult::Ok || result == CatalogResult::Quarantined);
            const bool published = cold.selected.root.generation > generation;
            assert(cold.live.Highwater() == sequence);
            const bool absent = published != restore;
            uint8_t row[96];
            assert(cold.Row(id - 1, row) && Word(row) == id);
            assert((row[11] == kFactoryAbsent) == absent);
            if (absent || published) assert(!Word(row + 4));
            const uint32_t one = 1, remove = 5, replace = 6;
            RecordView record;
            assert(cold.live.FindLive(1, &one, 1, record) == LiveResult::Ok && record.operation == Operation::Set);
            assert(cold.live.FindLive(2, &remove, 1, record) == LiveResult::Ok && record.operation == Operation::Remove);
            assert(cold.live.FindLive(2, &replace, 1, record) == LiveResult::Ok && record.operation == Operation::Set);
            if (!cut && !restore) {
                SegmentMap owned;
                assert(CollectOwnership(device, cold.catalog, cold.selected.root, owned) == CatalogResult::Ok);
                assert(!owned.Contains(old_segment));
                const auto offset = old_segment * kSegmentBytes;
                assert(std::equal(image.begin() + offset, image.begin() + offset + kSegmentBytes, nor.data.begin() + offset));
            }
            if (!cut) break;
        }
    }
    std::cout << FactoryName(id) << (restore ? " restore" : " uninstall")
              << ": " << operations * 2 << " torn flash operations passed\n";
}

int main(int argc, char **argv) {
    assert(argc == 6);
    const unsigned id = std::strtoul(argv[5], nullptr, 10);
    Run(Image(argv[1]), ReadPlan(argv[2]), id, false);
    Run(Image(argv[3]), ReadPlan(argv[4]), id, true);
}
