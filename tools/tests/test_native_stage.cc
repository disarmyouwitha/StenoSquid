#include "native_store_fixture.h"
#include "native_stage.h"
#include <fstream>
#include <iostream>

struct Plan {
    uint32_t metadata, bytes, crc;
    std::vector<std::pair<uint32_t, std::vector<uint8_t>>> blocks;
};

uint32_t ReadWord(std::istream &stream) {
    uint8_t bytes[4];
    stream.read(reinterpret_cast<char *>(bytes), sizeof(bytes));
    assert(stream);
    return Word(bytes);
}

Plan ReadPlan(const char *path) {
    std::ifstream file(path, std::ios::binary);
    const uint32_t count = ReadWord(file);
    Plan plan{ReadWord(file), ReadWord(file), ReadWord(file), {}};
    for (uint32_t i = 0; i < count; ++i) {
        const uint32_t segment = ReadWord(file);
        std::vector<uint8_t> body(ReadWord(file));
        file.read(reinterpret_cast<char *>(body.data()), body.size());
        assert(file);
        plan.blocks.emplace_back(segment, std::move(body));
    }
    return plan;
}

void Check(NativeStore<NativeDevice> &store, bool updated, bool merge) {
    auto value = [&](uint32_t id, const uint32_t *key, uint8_t count) {
        RecordView record;
        const auto status = store.live.FindLive(id, key, count, record);
        if (status == LiveResult::Missing) return std::string("<missing>");
        assert(status == LiveResult::Ok);
        if (record.operation == Operation::Reset) return std::string("<missing>");
        if (record.operation == Operation::Remove) return std::string("<remove>");
        assert(record.operation == Operation::Set);
        std::string text(record.text_bytes, ' ');
        assert(store.device.Read(record.text_address, reinterpret_cast<uint8_t *>(text.data()), text.size()));
        return text;
    };
    const uint32_t a=1,b=2,c=3,d=4,e=5,f=6,multi[]={7,8},reset=9;
    assert(value(1,&a,1) == (updated && merge ? "changed" : "stay-1"));
    assert(value(1,&b,1) == "keep-2");
    assert(value(1,&c,1) == "stay-3");
    assert(value(1,&d,1) == "keep-4");
    assert(value(2,&e,1) == "<remove>");
    assert(value(2,&f,1) == "base-override");
    assert(value(1,&reset,1) == "<missing>");
    assert(value(1,multi,2) == (updated && merge ? "ça" : "<missing>"));
}

void Upload(NativeStage<NativeDevice> &stage, const Plan &plan) {
    for (const auto &[segment, block] : plan.blocks) {
        assert(stage.Erase(segment));
        for (unsigned offset = 0; offset < block.size(); offset += 512) {
            const uint32_t n = std::min(size_t(512), block.size() - offset);
            const auto *start = block.data() + offset;
            if (std::all_of(start, start + n, [](uint8_t v) { return v == 255; })) continue;
            assert(stage.Write(segment * kSegmentBytes + offset, start, n));
        }
    }
}

void Run(const std::vector<uint8_t> &image, const Plan &plan, bool merge) {
    unsigned most_operations = 0;
    for (unsigned cut = 0; cut <= most_operations; ++cut) {
        for (unsigned tear : {1u, 4095u}) {
            Nor nor;
            nor.data = image;
            nor.operations = 0;
            nor.cut = cut;
            nor.tear = tear;
            NativeDevice device{{nor}};
            NativeStore<NativeDevice> store(device);
            NativeValidator<NativeDevice> validator(device, store.catalog);
            auto open = [&] {
                return store.Open([&](const CatalogRoot &root) { return validator.Validate(root); });
            };
            assert(open() == CatalogResult::Ok);
            NativeStage<NativeDevice> stage(store, validator);
            assert(stage.Begin(merge));
            assert(!stage.Erase(0) && !stage.Erase(2) && !stage.Erase(255));
            uint32_t stroke = 1;
            assert(store.Edit(1,0,&stroke,1,nullptr,0) == LiveResult::Busy);
            assert(store.Start(NativeStore<NativeDevice>::Work::Checkpoint) == JobResult::Busy);
            Check(store, false, merge);
            bool reopen = false;
            try {
                Upload(stage, plan);
                if (!cut) {
                    LiveView pinned;
                    assert(store.Snapshot(pinned));
                    assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen) == CatalogResult::Busy);
                    assert(!reopen && store.Release(pinned));
                }
                assert(stage.Commit(plan.metadata, plan.bytes, plan.crc ^ 1, reopen) == CatalogResult::Corrupt);
                assert(!reopen);
                assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen) == CatalogResult::Ok);
                assert(!cut && reopen);
                most_operations = nor.operations;
            } catch (const Cut &) {
                assert(cut);
            }
            nor.cut = 0;
            NativeStore<NativeDevice> cold(device);
            NativeValidator<NativeDevice> recovered(device, cold.catalog);
            const auto result = cold.Open([&](const CatalogRoot &root) { return recovered.Validate(root); });
            assert(result == CatalogResult::Ok || result == CatalogResult::Quarantined);
            Check(cold, cold.selected.root.generation > 2, merge);
            if (!cut) {
                assert(cold.selected.root.generation == 4);
                if (!merge) {
                    SegmentMap owned;
                    assert(CollectOwnership(device, cold.catalog, cold.selected.root, owned) == CatalogResult::Ok);
                    assert(!owned.Contains(2));
                    unsigned journal_count = 0;
                    uint8_t prefix[8];
                    assert(device.Read(cold.selected.root.address, prefix, 8));
                    for (unsigned i=0;i<prefix[6];++i) {
                        uint8_t row[24];
                        assert(device.Read(cold.selected.root.address+32+prefix[4]*96+i*24,row,24));
                        journal_count += Word(row+12)==2;
                    }
                    assert(journal_count==2);
                }
                break;
            }
        }
    }
    std::cout << (merge ? "Bulk import" : "Live-record compaction") << ": preserved verified state at "
              << 2 * most_operations << " torn-write boundaries\n";
}

void RefuseLostEntry(const std::vector<uint8_t> &image, const Plan &plan) {
    Nor nor;
    nor.data = image;
    NativeDevice device{{nor}};
    NativeStore<NativeDevice> store(device);
    NativeValidator<NativeDevice> validator(device, store.catalog);
    assert(store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }) == CatalogResult::Ok);
    NativeStage<NativeDevice> stage(store, validator);
    assert(stage.Begin(true));
    Upload(stage, plan);
    bool reopen = false;
    assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen) == CatalogResult::Corrupt);
    assert(!reopen);
    stage.Abort();
    Check(store, false, true);
    assert(std::equal(image.begin(), image.begin() + 2 * kSegmentBytes, nor.data.begin()));
}

int main(int argc, char **argv) {
    assert(argc == 5);
    std::ifstream file(argv[1], std::ios::binary);
    std::vector<uint8_t> image((std::istreambuf_iterator<char>(file)), {});
    image.resize(256 * kSegmentBytes, 0x42);
    Run(image, ReadPlan(argv[2]), true);
    Run(image, ReadPlan(argv[3]), false);
    RefuseLostEntry(image, ReadPlan(argv[4]));
}
