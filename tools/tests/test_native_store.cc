#include "native_store_fixture.h"
#include <iostream>
int main() {
    NativeFixture f;assert(f.Edit(1,1,"first")==LiveResult::Ok);
    const auto high=f.store.live.Highwater();assert(f.store.Enable(2,false,f.Check())==CatalogResult::Ok);
    assert(f.store.live.Highwater()==high && f.store.live.TailCount()==1);
    f.Open();uint8_t row[96];assert(f.store.Row(1,row) && !Word(row+4));
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Checkpoint)==JobResult::More);
    LiveView held;assert(f.store.Snapshot(held));f.Finish();
    RecordView record;uint32_t key=1;
    assert(f.store.live.FindSnapshot(held,1,&key,1,record)==LiveResult::Ok && record.text_bytes==5);
    assert(f.store.Release(held));
    LiveView before_clear;assert(f.store.Snapshot(before_clear));
    uint32_t replacement=0;assert(f.store.Clear(1,replacement,f.Check())==CatalogResult::Ok && replacement==4);
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
    // A malformed committed index is never reformatted during engine open.
    f.nor.data[2*65536+256+32]^=1;const auto bytes=f.nor.data;
    assert(f.store.Open([&](const CatalogRoot &r){return f.validator.Validate(r);})!=CatalogResult::Ok);
    assert(!f.store.opened && f.nor.data==bytes);
    std::cout<<"Native engine store: persistent flags, atomic fresh-ID reset, scheduled maintenance, leased readers, export deduplication and fail-closed reopen passed\n";
}
