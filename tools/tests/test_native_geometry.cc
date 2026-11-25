#include "native_stage_fixture.h"
#include "native_layout_map.h"

struct LargeDevice : NativeDevice {
    uint32_t PoolEnd() const { return kLargePoolEnd; }
};

int main(int argc, char **argv) {
    assert(argc == 4);
    Nor nor;
    std::ifstream source(argv[1], std::ios::binary);
    nor.data.assign(std::istreambuf_iterator<char>(source), {});
    assert(nor.data.size() == kLargePoolEnd * kSegmentBytes);
    LargeDevice device{{{nor}}};
    NativeStore<LargeDevice> store(device);
    NativeValidator<LargeDevice> validator(device, store.catalog);
    auto open = [&] {
        assert(store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }) == CatalogResult::Ok);
    };
    open();
    NativeStage<LargeDevice> stage(store, validator);
    for (unsigned index = 2; index < 4; ++index) {
        const bool named = index == 2;
        assert(stage.Begin(named ? NativeUpdateKind::SnapshotNamed : NativeUpdateKind::Personal, 16));
        assert(!stage.Erase(kLargePoolEnd));
        const auto plan = ReadPlan(argv[index]);
        assert(plan.metadata >= (named ? 512u : 768u));
        for (const auto &[segment, block] : plan.blocks) {
            assert(segment >= (named ? 512u : 768u));
            assert(stage.Erase(segment));
            for (unsigned offset = 0; offset < block.size(); offset += 512) {
                const auto length = std::min(size_t(512), block.size() - offset);
                const auto *data = block.data() + offset;
                if (std::all_of(data, data + length, [](uint8_t value) { return value == 255; })) continue;
                assert(stage.Write(segment * kSegmentBytes + offset, data, length));
            }
        }
        bool reopen = false;
        auto image_ok = +[](const uint8_t *extent, const uint8_t *row) {
            return Word(extent + 12) == 1 && row[11] == kUserSnapshot;
        };
        assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen, image_ok) == CatalogResult::Ok);
        assert(reopen);
        stage.Abort();
        open();
    }
    const uint32_t outline = 8;
    RecordView record;
    assert(store.live.FindLive(1, &outline, 1, record) == LiveResult::Ok);
    assert(record.text_bytes == 12);
    char page[256];
    assert(ReadLayout(device, store.selected.root, page, [] { return false; }, 512) && page[0] == 's');
    assert(ReadLayout(device, store.selected.root, page, [] { return false; }, 768) && page[0] == 'h' && page[255] == 'x');
    NativeDevice small{{nor}};
    NativeStore<NativeDevice> restricted(small);
    NativeValidator<NativeDevice> small_validator(small, restricted.catalog);
    assert(restricted.Open([&](const auto &root) { return small_validator.Validate(root); }) != CatalogResult::Ok);
}
