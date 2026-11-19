#include "native_store_fixture.h"
#include <iostream>

void Edit(NativeFixture &f, uint32_t key, const char *text, uint8_t op = 0) {
    for (unsigned tries = 0; tries < 10; ++tries) {
        const auto result = f.Edit(1, key, text, op);
        if (result == LiveResult::Ok)
            return;
        assert(result == LiveResult::Busy);
        f.Finish();
    }
    assert(false);
}
std::string Text(NativeFixture &f, uint32_t key) {
    RecordView record;
    const auto result = f.store.live.FindLive(1, &key, 1, record);
    if (result == LiveResult::Missing)
        return "<missing>";
    assert(result == LiveResult::Ok);
    if (record.operation == Operation::Reset)
        return "<reset>";
    if (record.operation == Operation::Remove)
        return "<remove>";
    std::string text(record.text_bytes, ' ');
    LiveView view;
    assert(f.store.Snapshot(view));
    assert(f.store.live.ReadText(view, record, reinterpret_cast<uint8_t *>(text.data()),
                                 text.size()) == LiveResult::Ok);
    assert(f.store.Release(view));
    return text;
}
void History(NativeFixture &f, bool keep_first = false) {
    if (keep_first)
        Edit(f, 2, "surviving first-journal entry");
    std::string text(2047, 'x');
    for (unsigned i = 0; i < 70; ++i)
        Edit(f, 1, text.c_str());
    Edit(f, 1, "latest");
}
SegmentMap Journals(NativeFixture &f) {
    LiveView view;
    assert(f.store.Snapshot(view));
    const auto result = view.journals;
    assert(f.store.Release(view));
    return result;
}
unsigned Count(const SegmentMap &map) {
    unsigned n = 0;
    for (unsigned i = 2; i < 255; ++i)
        n += map.Contains(i);
    return n;
}
void Checkpoint(NativeFixture &f) {
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Checkpoint) == JobResult::More);
    f.Finish();
}
void reader_ownership() {
    NativeFixture f;
    History(f);
    const auto before = Journals(f);
    assert(Count(before) >= 3);
    const auto bytes = f.nor.data;
    LiveView reader;
    assert(f.store.Snapshot(reader));
    Checkpoint(f);
    assert(Count(Journals(f)) == 1 && Text(f, 1) == "latest");
    SegmentMap owned;
    assert(CollectOwnership(f.device, f.store.catalog, f.store.selected.root, owned) ==
           CatalogResult::Ok);
    for (unsigned i = 2; i < 255; ++i)
        if (before.Contains(i)) {
            assert(owned.Contains(i));
            assert(!std::memcmp(bytes.data() + i * 65536, f.nor.data.data() + i * 65536, 65536));
        }
    RecordView record;
    uint32_t key = 1;
    assert(f.store.live.FindSnapshot(reader, 1, &key, 1, record) == LiveResult::Ok &&
           record.text_bytes == 6);
    // The retained reader protects its old catalog slot and all old extents.
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Checkpoint) == JobResult::More);
    JobResult result;
    do {
        result = f.store.Step(f.Check());
    } while (result == JobResult::More);
    assert(result == JobResult::Busy && f.store.opened);
    assert(f.store.Release(reader));
    Checkpoint(f);
    assert(CollectOwnership(f.device, f.store.catalog, f.store.selected.root, owned) ==
           CatalogResult::Ok);
    const auto live = Journals(f);
    for (unsigned i = 2; i < 255; ++i)
        if (before.Contains(i) && !live.Contains(i))
            assert(!owned.Contains(i) ||
                   std::memcmp(bytes.data() + i * 65536, f.nor.data.data() + i * 65536, 65536));
    f.Open();
    assert(Text(f, 1) == "latest");
    Edit(f, 1, "next");
    f.Open();
    assert(Text(f, 1) == "next");
    Checkpoint(f);
    f.Open();
    assert(Text(f, 1) == "next");
    bool reused = false;
    for (unsigned i = 2; i < 255; ++i)
        if (before.Contains(i) && !live.Contains(i))
            reused |=
                std::memcmp(bytes.data() + i * 65536, f.nor.data.data() + i * 65536, 65536) != 0;
    assert(reused); // Ordinary staging can reuse a dead journal only after release.
}
void boundaries() {
    NativeFixture f;
    History(f, true);
    Checkpoint(f);
    assert(Count(Journals(f)) == 2 && Text(f, 1) == "latest" &&
           Text(f, 2) == "surviving first-journal entry");
    // Reset is absent from the new index, but its sequence boundary must remain.
    Edit(f, 1, "", 2);
    Checkpoint(f);
    f.Open();
    assert(Text(f, 1) == "<missing>" && Text(f, 2) == "surviving first-journal entry");
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Rollover) == JobResult::More);
    f.Finish();
    const auto before = Journals(f);
    Checkpoint(f);
    f.Open();
    assert(Count(Journals(f)) == Count(before)); // Empty writer + boundary + live key.
    Edit(f, 1, "after empty writer");
    f.Open();
    assert(Text(f, 1) == "after empty writer");
    // An all-reset dictionary still keeps the boundary record and can reopen.
    NativeFixture empty;
    History(empty);
    Edit(empty, 1, "", 2);
    Checkpoint(empty);
    empty.Open();
    assert(Count(Journals(empty)) == 1 && Text(empty, 1) == "<missing>");
}
void power_cuts() {
    NativeFixture dry;
    History(dry);
    const auto baseline = dry.nor;
    const auto old = Journals(dry);
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
            const auto opened = boot.store.Open(
                [&](const CatalogRoot &root) { return boot.validator.Validate(root); });
            if (opened != CatalogResult::Ok && opened != CatalogResult::Quarantined) {
                std::cerr << "failed cut " << point << " tear " << tear << " status "
                          << unsigned(opened) << "\n";
                assert(false);
            }
            assert(boot.nor.data == bytes && Text(boot, 1) == "latest");
            if (opened == CatalogResult::Quarantined) {
                LiveView scratch;
                uint32_t replacement = 0;
                assert(boot.Edit(1, 1, "refused") == LiveResult::ReadOnly);
                assert(boot.store.Enable(1, false, scratch, boot.Check()) ==
                       CatalogResult::Quarantined);
                assert(boot.store.Clear(1, replacement, scratch, boot.Check()) ==
                       CatalogResult::Quarantined);
                assert(boot.store.Start(NativeStore<NativeDevice>::Work::Checkpoint) ==
                       JobResult::ReadOnly);
                assert(boot.nor.data == bytes && Text(boot, 1) == "latest");
            }
            for (unsigned i = 2; i < 255; ++i)
                if (old.Contains(i))
                    assert(!std::memcmp(baseline.data.data() + i * 65536,
                                        boot.nor.data.data() + i * 65536, 65536));
            ++cases;
        }
    std::cout << "Dead-journal pruning: " << cases
              << " torn operation cases retain every committed translation and never erase old "
                 "journals\n";
}
int main() {
    reader_ownership();
    boundaries();
    power_cuts();
    std::cout << "Dead-journal pruning: reader/catalog ownership, reset boundaries, empty append "
                 "journal, live-key retention and reopen passed\n";
}
