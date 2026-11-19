#include "native_store_fixture.h"
#include <iostream>
int main() {
    NativeFixture f;
    RecordView record;
    const uint32_t strokes[16] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
    assert(f.store.live.MaximumOutline(1) == 0);
    f.nor.reads = f.nor.read_bytes = 0;
    assert(f.store.live.FindLive(1, strokes, 1, record) == LiveResult::Missing && !f.nor.reads);
    const uint8_t text[] = {'t', 'w', 'o'};
    assert(f.store.Edit(1, 0, strokes, 2, text, 3) == LiveResult::Ok);
    assert(f.store.live.MaximumOutline(1) == 2 && f.store.live.MaximumOutline(2) == 0);
    LiveView old;
    assert(f.store.Snapshot(old));
    f.nor.reads = f.nor.read_bytes = 0;
    assert(f.store.live.FindLive(1, strokes, 1, record) == LiveResult::Missing);
    assert(f.store.live.FindLive(2, strokes, 2, record) == LiveResult::Missing);
    assert(!f.nor.reads && !f.nor.read_bytes);
    assert(f.store.Edit(1, 0, strokes, 1, text, 3) == LiveResult::Ok);
    f.nor.reads = 0;
    assert(f.store.live.FindSnapshot(old, 1, strokes, 1, record) == LiveResult::Missing &&
           !f.nor.reads);
    assert(f.store.live.FindLive(1, strokes, 1, record) == LiveResult::Ok &&
           record.operation == Operation::Set);
    assert(f.store.Release(old));
    assert(f.store.Edit(1, 1, strokes, 3, nullptr, 0) == LiveResult::Ok);
    assert(f.store.live.FindLive(1, strokes, 3, record) == LiveResult::Ok &&
           record.operation == Operation::Remove);
    assert(f.store.Edit(1, 2, strokes, 16, nullptr, 0) == LiveResult::Ok);
    assert(f.store.live.MaximumOutline(1) == 16);
    assert(f.store.live.FindLive(1, strokes, 16, record) == LiveResult::Ok &&
           record.operation == Operation::Reset);
    f.Open();
    assert(f.store.live.MaximumOutline(1) == 16);
    f.nor.reads = 0;
    assert(f.store.live.FindLive(1, strokes, 15, record) == LiveResult::Missing && !f.nor.reads);
    assert(f.store.live.FindLive(1, strokes, 0, record) == LiveResult::Invalid);
    assert(f.store.live.FindLive(1, strokes, 17, record) == LiveResult::Invalid);
    uint32_t replacement = 0;
    LiveView scratch;
    assert(f.store.Clear(1, replacement, scratch, f.Check()) == CatalogResult::Ok &&
           replacement == 4);
    f.Open();
    assert(f.store.live.MaximumOutline(4) == 0);
    f.nor.reads = 0;
    assert(f.store.live.FindLive(4, strokes, 1, record) == LiveResult::Missing && !f.nor.reads);
    assert(f.store.Edit(4, 0, strokes, 1, text, 3) == LiveResult::Ok);
    f.Open();
    assert(f.store.live.MaximumOutline(4) == 1);
    std::cout << "Native outline lengths: zero-read empty/wrong-length misses, snapshot isolation, "
                 "remove/reset, 16-stroke bounds, cold reconstruction and fresh-ID reset passed\n";
}
