#include "native_stage_fixture.h"
#include <array>
#include <iostream>

static constexpr unsigned FixtureInitialJournal = 8; // Two roots and six individual factory images.

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
            assert(stage.Begin(merge ? NativeUpdateKind::Personal : NativeUpdateKind::Compact));
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
                    assert(!owned.Contains(FixtureInitialJournal));
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
    assert(stage.Begin(NativeUpdateKind::Personal));
    Upload(stage, plan);
    bool reopen = false;
    assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen) == CatalogResult::Corrupt);
    assert(!reopen);
    stage.Abort();
    Check(store, false, true);
    assert(std::equal(image.begin(), image.begin() + 2 * kSegmentBytes, nor.data.begin()));
}

void CheckNamedCatalog(const std::vector<uint8_t> &image, const Plan &plan, NativeUpdateKind kind) {
    unsigned operations = 0;
    for (unsigned cut = 0; cut <= operations; ++cut) {
        Nor nor;
        nor.data = image;
        nor.cut = cut;
        nor.tear = 1;
        NativeDevice device{{nor}};
        NativeStore<NativeDevice> store(device);
        NativeValidator<NativeDevice> validator(device, store.catalog);
        auto open = [&] { return store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }); };
        assert(open() == CatalogResult::Ok);
        NativeStage<NativeDevice> stage(store, validator);
        assert(stage.Begin(kind, 16));
        const bool named = kind == NativeUpdateKind::Named;
        try {
            Upload(stage, plan);
            bool reopen = false;
            assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen) == CatalogResult::Ok);
            assert(!cut && reopen);
            operations = nor.operations;
        } catch (const Cut &) {
            assert(cut);
        }
        nor.cut = 0;
        stage.Abort();
        const auto recovered = open();
        assert(recovered == CatalogResult::Ok || recovered == CatalogResult::Quarantined);
        Check(store, false, false);
        const bool published = store.selected.root.generation > 2;
        uint8_t row[96];
        assert(store.Row(0, row) && Word(row) == (published && !named ? 6u : 1u));
        if (named) {
            assert(store.Row(1, row) && Word(row) == (published ? 8u : 2u));
            if (published) {
                assert(row[12] == 7 && !std::memcmp(row + 16, "Medical", 7));
                const uint32_t outline = 1;
                RecordView record;
                assert(store.live.FindLive(8, &outline, 1, record) == LiveResult::Ok);
                assert(record.text_bytes == 14 &&
                       !std::memcmp(nor.data.data() + record.text_address, "named override", 14));
            }
        } else {
            assert(store.live.TailCount() == 7);
        }
    }
    std::cout << (kind == NativeUpdateKind::Named ? "Named import" : "Priority move")
              << ": " << operations << " interrupted writes recovered" << std::endl;
}

void CheckMetadata(const std::vector<uint8_t> &image, const Plan &plan, bool deleting) {
    unsigned operations = 0;
    for (unsigned cut = 0; cut <= operations; ++cut) {
        for (unsigned tear : {1u, 4095u}) {
            Nor nor;
            nor.data = image;
            nor.cut = cut;
            nor.tear = tear;
            NativeDevice device{{nor}};
            NativeStore<NativeDevice> store(device);
            NativeValidator<NativeDevice> validator(device, store.catalog);
            auto open = [&] { return store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }); };
            assert(open() == CatalogResult::Ok);
            NativeStage<NativeDevice> stage(store, validator);
            assert(stage.Begin(NativeUpdateKind::Backup));
            assert(!stage.Erase(plan.metadata));
            bool reopen = false;
            assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen) == CatalogResult::Invalid);
            assert(!reopen);
            stage.Abort();
            assert(stage.Begin(deleting ? NativeUpdateKind::Delete : NativeUpdateKind::Rename));
            try {
                Upload(stage, plan);
                assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen) == CatalogResult::Ok);
                assert(!cut && reopen);
                operations = nor.operations;
            } catch (const Cut &) {
                assert(cut);
            }
            nor.cut = 0;
            stage.Abort();
            const auto result = open();
            assert(result == CatalogResult::Ok || result == CatalogResult::Quarantined);
            Check(store, false, false);
            const bool published = store.selected.root.generation > 4;
            uint8_t prefix[4], row[96];
            assert(device.Read(store.selected.root.address, prefix, 4) && Word(prefix) == 9);
            assert(store.Row(1, row));
            const uint32_t outline = 1;
            RecordView record;
            if (published && deleting) {
                assert(Word(row) == 2);
                assert(!store.Row(7, row));
                assert(store.live.FindLive(8, &outline, 1, record) == LiveResult::Invalid);
            } else {
                const std::string expected = published ? "Medical Practice" : "Medical";
                assert(Word(row) == 8 && row[12] == expected.size());
                assert(!std::memcmp(row + 16, expected.data(), expected.size()));
                assert(store.live.FindLive(8, &outline, 1, record) == LiveResult::Ok);
                assert(record.text_bytes == 14 &&
                       !std::memcmp(nor.data.data() + record.text_address, "named override", 14));
            }
            if (!cut) break;
        }
    }
    std::cout << (deleting ? "Delete" : "Rename") << ": " << 2 * operations
              << " interrupted writes recovered" << std::endl;
}

void CheckRecoveryBackup(const std::vector<uint8_t> &image) {
    Nor nor;
    nor.data = image;
    nor.data[kSegmentBytes + 32] ^= 1;
    const auto before = nor.data;
    NativeDevice device{{nor}};
    NativeStore<NativeDevice> store(device);
    NativeValidator<NativeDevice> validator(device, store.catalog);
    assert(store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }) ==
           CatalogResult::Quarantined);
    NativeStage<NativeDevice> stage(store, validator);
    assert(!stage.Begin(NativeUpdateKind::Compact));
    assert(stage.Begin(NativeUpdateKind::Backup));
    uint8_t bytes[64];
    assert(stage.Read(stage.Root().address, bytes, sizeof(bytes)));
    unsigned used = 2;
    while (!stage.Protected().Contains(used)) ++used;
    unsigned free = 2;
    while (stage.Protected().Contains(free)) ++free;
    assert(stage.Read(used * kSegmentBytes, bytes, sizeof(bytes)));
    assert(!stage.Read(free * kSegmentBytes, bytes, sizeof(bytes)));
    assert(!stage.Erase(free) && !stage.Write(free * kSegmentBytes, bytes, sizeof(bytes)));
    bool reboot = false;
    assert(stage.Commit(free, 64, 0, reboot) == CatalogResult::Invalid);
    assert(!reboot && nor.data == before);
    stage.Abort();
    Check(store, false, false);
    std::cout << "Quarantined catalog: read-only backup serves selected extents and leaves flash intact" << std::endl;
}

void CheckIdleCompaction(const std::vector<uint8_t> &image, bool cancel) {
    unsigned operations = 0;
    for (unsigned cut = 0; cut <= operations; ++cut) {
        Nor nor;
        nor.data = image;
        nor.cut = cut;
        nor.tear = 77;
        NativeDevice device{{nor}};
        NativeStore<NativeDevice> store(device);
        NativeValidator<NativeDevice> validator(device, store.catalog);
        auto check = [&](uint32_t i, const uint8_t *row) { return validator.Extent(i, row); };
        auto open = [&] { return store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }); };
        assert(open() == CatalogResult::Ok);
        const auto original = store.selected.root;
        SegmentMap before;
        assert(CollectOwnership(device, store.catalog, original, before) == CatalogResult::Ok);
        assert(store.Start(NativeStore<NativeDevice>::Work::Checkpoint, true) == JobResult::More);
        bool cancelled = false;
        try {
            for (unsigned i = 0; i < 10000; ++i) {
                const auto status = store.Step(check);
                if (cancel && nor.operations && !store.checkpoint.Published()) {
                    assert(store.CancelCheckpoint());
                    assert(store.selected.root.generation == original.generation);
                    cancelled = true;
                    break;
                }
                if (status != JobResult::More) {
                    assert(status == JobResult::Done && !cut);
                    operations = nor.operations;
                    break;
                }
            }
            assert(cancel ? cancelled : operations && store.work == NativeStore<NativeDevice>::Work::None);
        } catch (const Cut &) {
            assert(cut);
            assert(!cancel);
        }
        nor.cut = 0;
        NativeStore<NativeDevice> recovered(device);
        NativeValidator<NativeDevice> recovered_validator(device, recovered.catalog);
        const auto result = recovered.Open([&](const CatalogRoot &root) { return recovered_validator.Validate(root); });
        assert(result == CatalogResult::Ok || result == CatalogResult::Quarantined);
        Check(recovered, false, false);
        if (!cut && !cancel) {
            SegmentMap after;
            assert(CollectOwnership(device, recovered.catalog, recovered.selected.root, after) == CatalogResult::Ok);
            assert(AllocationPlan(after).Free() > AllocationPlan(before).Free());
            assert(!after.Contains(FixtureInitialJournal) && recovered.live.TailCount() == 0);
            assert(store.checkpoint.ReclaimedJournals() == 1);
            assert(recovered.live.Highwater() == 12);
        }
    }
    std::cout << (cancel ? "Idle compaction cancellation passed" : "Idle compaction: interrupted writes recovered")
              << " (" << operations << " flash operations)" << std::endl;
}

void CheckBaseReplacement(const std::vector<uint8_t> &image, const Plan &plan) {
    unsigned operations = 0;
    for (unsigned cut = 0; cut <= operations; ++cut) {
        Nor nor;
        nor.data = image;
        nor.cut = cut;
        nor.tear = 11;
        NativeDevice device{{nor}};
        NativeStore<NativeDevice> store(device);
        NativeValidator<NativeDevice> validator(device, store.catalog);
        auto open = [&] { return store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }); };
        assert(open() == CatalogResult::Ok);
        NativeStage<NativeDevice> stage(store, validator);
        assert(stage.Begin(NativeUpdateKind::Base));
        uint8_t old_row[96], new_row[96];
        assert(store.Row(1, old_row));
        try {
            Upload(stage, plan);
            bool reopen = false;
            auto fail = [](const uint8_t *, const uint8_t *) { return false; };
            assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen, fail) == CatalogResult::Corrupt);
            assert(!reopen);
            auto probe = [](const uint8_t *extent, const uint8_t *row) {
                return Word(extent + 12) == 1 && row[10] < 5;
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
        Check(store, false, false);
        assert(store.Row(1, new_row) && !std::memcmp(new_row, old_row, 64));
        assert((std::memcmp(new_row + 64, old_row + 64, 32) != 0) ==
               (store.selected.root.generation > 2));
    }
    std::cout << "Pinned base replacement: " << operations << " interrupted writes recovered" << std::endl;
}

void CheckSnapshotPublication(const std::vector<uint8_t> &image, const Plan &plan) {
    unsigned operations = 0;
    for (unsigned cut = 0; cut <= operations; ++cut) {
        for (unsigned tear : {1u, 4095u}) {
            Nor nor;
            nor.data = image;
            nor.cut = cut;
            nor.tear = tear;
            NativeDevice device{{nor}};
            NativeStore<NativeDevice> store(device);
            NativeValidator<NativeDevice> validator(device, store.catalog);
            auto open = [&] { return store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }); };
            assert(open() == CatalogResult::Ok);
            std::vector<std::array<uint8_t, 96>> original;
            uint8_t row[96];
            for (unsigned i = 0; store.Row(i, row); ++i) {
                original.emplace_back();
                std::copy_n(row, sizeof(row), original.back().begin());
            }
            NativeStage<NativeDevice> stage(store, validator);
            assert(stage.Begin(NativeUpdateKind::SnapshotNamed));
            try {
                Upload(stage, plan);
                bool reopen = false;
                auto probe = [](const uint8_t *extent, const uint8_t *row) {
                    return Word(extent + 12) == 1 && row[10] == 0 && row[11] == kUserSnapshot;
                };
                assert(stage.Commit(plan.metadata, plan.bytes, plan.crc, reopen, probe) == CatalogResult::Ok);
                assert(!cut && reopen);
                operations = std::max(operations, nor.operations);
            } catch (const Cut &) {
                assert(cut);
            }
            nor.cut = 0;
            stage.Abort();
            const auto result = open();
            assert(result == CatalogResult::Ok || result == CatalogResult::Quarantined);
            Check(store, false, false);
            const bool published = store.selected.root.generation > 2;
            assert(!store.Row(original.size() + published, row));
            for (unsigned i = 0; i < original.size(); ++i) {
                uint8_t row[96];
                assert(store.Row(i + (published && i != 0), row));
                assert(!std::memcmp(row, original[i].data(), sizeof(row)));
            }
            if (published) {
                uint8_t row[96];
                assert(store.Row(1, row) && row[11] == kUserSnapshot && Word(row + 4) == 1);
                assert(row[12] == 12 && !std::memcmp(row + 16, "Experimental", 12));
            }
            if (!cut) {
                assert(published && result == CatalogResult::Ok);
                assert(store.Row(1, row));
                const uint32_t id = Word(row);
                const uint32_t added = 1048836, replaced = 524552, removed = 66096; // TAS, KAT, PWOB
                const uint8_t text[] = "snapshot overlay";
                for (const uint32_t *key : {&added, &replaced}) {
                    assert(store.Edit(id, 0, key, 1, text, sizeof(text) - 1) == LiveResult::Ok);
                }
                assert(store.Edit(id, 1, &removed, 1, text, 0) == LiveResult::Ok);
                assert(open() == CatalogResult::Ok);
                RecordView record;
                for (const uint32_t *key : {&added, &replaced}) {
                    assert(store.live.FindLive(id, key, 1, record) == LiveResult::Ok);
                    assert(record.operation == Operation::Set && record.text_bytes == sizeof(text) - 1);
                    assert(!std::memcmp(nor.data.data() + record.text_address, text, sizeof(text) - 1));
                }
                assert(store.live.FindLive(id, &removed, 1, record) == LiveResult::Ok);
                assert(record.operation == Operation::Remove);
                Check(store, false, false);
            }
        }
    }
    std::cout << "Snapshot publication: " << operations << " flash boundaries, two cuts each" << std::endl;
}

int main(int argc, char **argv) {
    assert(argc == 12);
    std::ifstream file(argv[1], std::ios::binary);
    std::vector<uint8_t> image((std::istreambuf_iterator<char>(file)), {});
    image.resize(256 * kSegmentBytes, 0x42);
    CheckRecoveryBackup(image);
    CheckIdleCompaction(image, true);
    CheckIdleCompaction(image, false);
    Run(image, ReadPlan(argv[2]), true);
    Run(image, ReadPlan(argv[3]), false);
    RefuseLostEntry(image, ReadPlan(argv[4]));
    CheckNamedCatalog(image, ReadPlan(argv[5]), NativeUpdateKind::Named);
    CheckNamedCatalog(image, ReadPlan(argv[6]), NativeUpdateKind::Reorder);
    std::ifstream named_file(argv[7], std::ios::binary);
    std::vector<uint8_t> named_image((std::istreambuf_iterator<char>(named_file)), {});
    named_image.resize(256 * kSegmentBytes, 0x42);
    CheckMetadata(named_image, ReadPlan(argv[8]), false);
    CheckMetadata(named_image, ReadPlan(argv[9]), true);
    CheckBaseReplacement(image, ReadPlan(argv[10]));
    CheckSnapshotPublication(image, ReadPlan(argv[11]));
}
