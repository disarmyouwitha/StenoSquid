#include "native_store_fixture.h"
#include "native_overlay_usage.h"
#include <iostream>

static void TestStagedRecovery() {
    NativeFixture fixture;
    assert(fixture.Edit(1, 1, "before boot") == LiveResult::Ok);
    LiveView scratch;
    assert(fixture.store.Enable(3, false, scratch, fixture.Check()) == CatalogResult::Ok);
    assert(fixture.store.Enable(2, false, scratch, fixture.Check()) == CatalogResult::Ok);
    assert(fixture.store.selected.slot == 0);
    bool reuse_selected = false;
    const auto open = [&] {
        reuse_selected = false;
        unsigned reads_after_validation = 0;
        const auto result = fixture.store.Open([&](const CatalogRoot &root) {
            const auto validated = fixture.validator.Validate(root, false, nullptr, nullptr, &fixture.store.live);
            reads_after_validation = fixture.nor.reads;
            return validated;
        }, CatalogValidation::ReuseIdenticalPayload, true);
        reuse_selected = fixture.nor.reads == reads_after_validation;
        return result;
    };
    assert(open() == CatalogResult::Ok && reuse_selected);
    const auto newer = fixture.store.live.Root();
    uint8_t row[96];
    assert(fixture.store.Row(1, row) && !Word(row + 4));
    assert(fixture.store.live.TailCount() == 1 && fixture.store.live.MaximumOutline(1) == 1);
    assert(fixture.Edit(1, 3, "after boot") == LiveResult::Ok);
    assert(open() == CatalogResult::Ok && fixture.store.live.TailCount() == 2);

    // The newer catalog has distinct metadata. Its rejection must select the
    // older state and retain quarantine even if a scan was staged for it.
    fixture.nor.data[newer.address] ^= 1;
    assert(open() == CatalogResult::Quarantined);
    assert(fixture.store.live.Root().generation < newer.generation);
    assert(fixture.store.Row(1, row) && Word(row + 4));
    assert(fixture.store.live.TailCount() == 2);
    assert(fixture.Edit(1, 4, "quarantined") == LiveResult::ReadOnly);
}

int main() {
    TestStagedRecovery();
    NativeFixture f;assert(f.Edit(1,1,"first")==LiveResult::Ok);
    LiveView scratch;
    const auto high=f.store.live.Highwater();assert(f.store.Enable(2,false,scratch,f.Check())==CatalogResult::Ok);
    assert(!scratch.token);
    assert(f.store.Snapshot(scratch));
    assert(f.store.Enable(2,true,scratch,f.Check())==CatalogResult::Busy);
    uint32_t blocked=0;assert(f.store.Clear(1,blocked,scratch,f.Check())==CatalogResult::Busy);
    assert(f.store.Release(scratch));
    assert(f.store.live.Highwater()==high && f.store.live.TailCount()==1);
    f.Open();uint8_t row[96];assert(f.store.Row(1,row) && !Word(row+4));
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Checkpoint)==JobResult::More);
    LiveView held;assert(f.store.Snapshot(held));f.Finish();
    RecordView record;uint32_t key=1;
    assert(f.store.live.FindSnapshot(held,1,&key,1,record)==LiveResult::Ok && record.text_bytes==5);
    assert(f.store.Release(held));
    LiveView before_clear;assert(f.store.Snapshot(before_clear));
    uint32_t replacement=0;assert(f.store.Clear(1,replacement,scratch,f.Check())==CatalogResult::Ok && replacement==4);
    assert(f.store.live.FindSnapshot(before_clear,1,&key,1,record)==LiveResult::Ok && record.text_bytes==5);
    assert(f.store.Release(before_clear));
    LiveView fresh;assert(f.store.Snapshot(fresh));
    assert(f.store.live.FindSnapshot(fresh,4,&key,1,record)==LiveResult::Missing);assert(f.store.Release(fresh));
    f.Open();assert(f.store.Row(0,row) && Word(row)==4);
    for(unsigned i=0;i<256;++i)assert(f.Edit(4,1,"latest")==LiveResult::Ok);
    assert(f.Edit(4,1,"not yet")==LiveResult::Busy);f.Finish();
    assert(f.Edit(4,1,"after checkpoint")==LiveResult::Ok);f.Open();
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Rollover)==JobResult::More);f.Finish();
    assert(f.Edit(4,2,"next journal")==LiveResult::Ok);f.Open();
    OverlayCursor cursor;MergeRecord item;bool more=true;LiveView export_view;assert(f.store.Snapshot(export_view));
    unsigned count=0;
    while(more){auto result=NextOverlay(f.device,f.store.live,export_view,cursor,item,more);if(result==LiveResult::Ok){assert(item.identity==4);++count;}else assert(result==LiveResult::Missing || result==LiveResult::More);}
    assert(count==2 && f.store.Release(export_view));
    assert(f.Edit(4, 1, "unused")==LiveResult::Ok);
    assert(f.Edit(4, 1, "", 1)==LiveResult::Ok);
    assert(f.Edit(4, 2, "", 2)==LiveResult::Ok);
    assert(f.Edit(2, 2, "other")==LiveResult::Ok);
    OverlayUsage usage[3]{};
    uint32_t position=0;
    const auto &view=f.store.live.CurrentView();
    const auto collect = [&](unsigned slot, unsigned segment, unsigned operation, unsigned span) {
        assert(view.journals.Contains(segment));
        ++usage[slot].count;
        usage[slot].entries += operation == 0;
        usage[slot].bytes += span;
        return true;
    };
    while(position<view.forward_count+view.tail.count) {
        const auto before=position;
        assert(MeasureOverlay(f.device,view,position,collect,[]{return true;}));
        assert(position==before+1);
    }
    assert(usage[0].count==1 && usage[0].entries==0 && usage[0].bytes==44);
    assert(usage[1].count==1 && usage[1].entries==1 && usage[1].bytes==52);
    // A malformed committed index is never reformatted during engine open.
    f.nor.data[2*65536+256+32]^=1;const auto bytes=f.nor.data;
    assert(f.store.Open([&](const CatalogRoot &r){return f.validator.Validate(r, false);})!=CatalogResult::Ok);
    assert(!f.store.opened && f.nor.data==bytes);
    std::cout<<"Native engine store: persistent flags, atomic fresh-ID reset, scheduled maintenance, leased readers, export deduplication and fail-closed reopen passed\n";
}
