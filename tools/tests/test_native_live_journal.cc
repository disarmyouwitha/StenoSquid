#include "native_live_fixture.h"
#include <iostream>
void basic_and_snapshots() {
    LiveFixture fixture;
    static_assert(sizeof(LiveView)<=1536);
    assert(fixture.live.Ready() && fixture.live.TailCount()==0);
    assert(fixture.Edit(1,1,"old")==LiveResult::Ok);
    LiveView old;assert(fixture.live.Snapshot(old)==LiveResult::Ok);
    assert(fixture.Edit(1,1,"new")==LiveResult::Ok);
    assert(fixture.Text(1,1)=="new" && fixture.Text(1,1,&old)=="old");
    assert(fixture.Checkpoint()==CatalogResult::Ok);
    assert(fixture.Text(1,1)=="new" && fixture.Text(1,1,&old)=="old");
    assert(fixture.Edit(1,1,"next")==LiveResult::Ok);
    // The reader pinned slot 0. A third catalog publication must wait, never
    // overwrite metadata while that reader still has a valid snapshot.
    assert(fixture.Checkpoint()==CatalogResult::Busy);
    assert(fixture.Text(1,1,&old)=="old" && fixture.Text(1,1)=="next");
    auto copied=old;
    assert(fixture.live.Release(old) && !fixture.live.Release(old));
    RecordView record;uint32_t key=1;
    assert(fixture.live.FindSnapshot(copied,1,&key,1,record)==LiveResult::Invalid);
    assert(fixture.Checkpoint()==CatalogResult::Ok);
    fixture.Recover();assert(fixture.Text(1,1)=="next");
    SegmentMap owned;
    assert(CollectOwnership(fixture.device,fixture.catalog,fixture.live.Root(),owned)==CatalogResult::Ok);
    assert(owned.Contains(0) && owned.Contains(1) && owned.Contains(2) && owned.Contains(255));
    fixture.nor.fail_read=true;
    assert(CollectOwnership(fixture.device,fixture.catalog,fixture.live.Root(),owned)!=CatalogResult::Ok);
    AllocationPlan closed(owned);uint32_t reserved=0;
    assert(!closed.Reserve(1,reserved));fixture.nor.fail_read=false;
    std::cout<<"Live journal: snapshot isolation, maintenance read fallback, deferred publication and fail-closed ownership passed\n";
}
void churn() {
    LiveFixture fixture;
    std::map<LiveKey,std::pair<uint8_t,std::string>> expected;
    uint32_t random=20261118;
    auto next=[&](){random^=random<<13;random^=random>>17;random^=random<<5;return random;};
    for(unsigned i=0;i<700;++i) {
        uint32_t id=1+next()%2,key=1+next()%16;uint8_t op=next()%3;
        std::string text=op?"":"word "+std::to_string(i)+" ç";
        auto result=fixture.Edit(id,key,text,op);
        if(result==LiveResult::NeedCheckpoint){assert(fixture.Checkpoint()==CatalogResult::Ok);result=fixture.Edit(id,key,text,op);}
        if(result==LiveResult::NeedJournal){assert(fixture.Rollover());result=fixture.Edit(id,key,text,op);}
        assert(result==LiveResult::Ok);expected[{id,{key}}]={op,text};
        for(uint32_t d=1;d<=2;++d)for(uint32_t k=1;k<=16;++k) {
            auto found=expected.find({d,{k}});
            const auto wanted=found==expected.end() || found->second.first?"<absent>":found->second.second;
            assert(fixture.Text(d,k)==wanted);
        }
        if(i%137==0)fixture.Recover();
    }
    fixture.Recover();
    std::cout<<"Live journal: 700 mixed edits, 22400 oracle lookups, checkpoints and incremental reboot replay passed\n";
}
void bounds_and_rollover() {
    LiveFixture small(8);
    for(unsigned pass=0;pass<2;++pass) {
        for(unsigned i=0;i<256;++i)assert(small.Edit(1,1,"value "+std::to_string(pass*256+i))==LiveResult::Ok);
        auto before=small.nor.data;
        assert(small.Edit(1,1,"overflow")==LiveResult::NeedCheckpoint && before==small.nor.data);
        if(!pass)assert(small.Checkpoint()==CatalogResult::Ok);
        else {assert(small.Checkpoint()==CatalogResult::Invalid);assert(before==small.nor.data);}
    }
    assert(small.Text(1,1)=="value 511");
    LiveFixture big;
    const std::string long_text(2047,'x');
    unsigned committed=0;
    while(big.Edit(1,1,long_text)==LiveResult::Ok)++committed;
    assert(committed==31 && big.Edit(1,1,long_text)==LiveResult::NeedJournal);
    LiveView old;assert(big.live.Snapshot(old)==LiveResult::Ok);
    assert(big.Rollover());assert(big.Edit(1,1,"new segment")==LiveResult::Ok);
    assert(big.Text(1,1)=="new segment" && big.Text(1,1,&old)==long_text);
    assert(big.live.Release(old));big.Recover();assert(big.Text(1,1)=="new segment");
    std::cout<<"Live journal: full-tail refusal, near-full safe refusal, maximum texts and journal rollover passed\n";
}
void recovery_failures() {
    LiveFixture fixture;
    assert(fixture.Edit(1,1,"before")==LiveResult::Ok);
    // Commit succeeds but subsequent readback fails: no retry against uncertain
    // writer state. Reopen must recover and expose that committed edit.
    fixture.nor.fail_after_commit=true;
    assert(fixture.Edit(1,1,"committed")==LiveResult::IoError && !fixture.live.Ready());
    const auto uncertain=fixture.nor.data;
    assert(fixture.Edit(1,1,"unsafe retry")==LiveResult::Invalid && fixture.nor.data==uncertain);
    fixture.nor.fail_read=false;fixture.Recover();assert(fixture.Text(1,1)=="committed");
    auto dry=fixture.nor;dry.operations=0;
    fixture.nor.operations=0;
    // Count physical operations in the same real live Append path.
    assert(fixture.Edit(1,1,"next")==LiveResult::Ok);
    const unsigned operations=fixture.nor.operations;
    for(unsigned point=1;point<=operations;++point)for(unsigned tear:{0u,1u,127u,4095u}) {
        fixture.nor=dry;fixture.Recover();fixture.nor.operations=0;fixture.nor.cut=point;fixture.nor.tear=tear;
        try{fixture.Edit(1,1,"next");assert(false);}catch(const Cut &){}
        fixture.nor.cut=0;auto before=fixture.nor.data;
        fixture.Recover();assert(fixture.nor.data==before);
        auto value=fixture.Text(1,1);assert(value=="committed" || value=="next");
        auto result=fixture.Edit(1,1,"after recovery");
        if(result==LiveResult::NeedJournal){assert(fixture.Rollover());result=fixture.Edit(1,1,"after recovery");}
        assert(result==LiveResult::Ok && fixture.Text(1,1)=="after recovery");
        fixture.Recover();assert(fixture.Text(1,1)=="after recovery");
        fixture.journals={{2,1}}; // Restore fixture topology when returning to dry bytes.
    }
    // A deliberately overlong tail is not silently truncated or made writable.
    LiveFixture overflow;
    for(unsigned i=0;i<256;++i)assert(overflow.Edit(1,1,"v")==LiveResult::Ok);
    JournalScan scan;JournalRecord row;
    assert(overflow.journal.BeginScan(2,1,0,scan)==JournalResult::Ok);
    while(overflow.journal.Step(scan,row)==JournalResult::Ok){}
    uint32_t key=1;const uint8_t text='x';
    assert(overflow.journal.Append(scan.next,257,1,0,&key,1,&text,1)==JournalResult::Ok);
    assert(overflow.live.Begin(overflow.selection)==LiveResult::More);
    LiveResult status;
    do{status=overflow.live.RecoverStep();}while(status==LiveResult::More);
    assert(status==LiveResult::NeedRebuild && !overflow.live.Ready());
    std::cout<<"Live journal: "<<operations*4<<" torn appends, ambiguous commit/readback recovery, sealed tails and overlong-tail refusal passed\n";
}
void bounded_lookup_and_invalidation() {
    LiveFixture fixture;
    const std::string long_text(2047,'x');
    for(uint32_t key=1;key<=256;++key) {
        auto status=fixture.Edit(1,key,long_text);
        if(status==LiveResult::NeedJournal){assert(fixture.Rollover());status=fixture.Edit(1,key,long_text);}
        assert(status==LiveResult::Ok);
    }
    uint32_t key=257;RecordView result;
    const auto before=fixture.nor.read_bytes;
    assert(fixture.live.FindLive(1,&key,1,result)==LiveResult::Missing);
    const auto miss_bytes=fixture.nor.read_bytes-before;
    assert(miss_bytes<20000); // Not 256 complete 2 KiB translations.
    LiveView snapshot;const auto calls=fixture.nor.reads;
    assert(fixture.live.Snapshot(snapshot)==LiveResult::Ok && fixture.nor.reads==calls);
    key=256;assert(fixture.live.FindSnapshot(snapshot,1,&key,1,result)==LiveResult::Ok);
    fixture.nor.data[result.text_address]^=1;
    assert(fixture.live.FindLive(1,&key,1,result)==LiveResult::Corrupt && !fixture.live.Ready());
    fixture.catalog.Invalidate();
    assert(!fixture.catalog.CanWrite(snapshot.root));
    assert(fixture.live.FindSnapshot(snapshot,1,&key,1,result)==LiveResult::Invalid);
    assert(fixture.live.Release(snapshot));
    std::cout<<"Live journal: full-tail miss reads "<<miss_bytes<<" bytes; zero-read pins, matched-payload corruption and trust invalidation passed\n";
}
int main(){basic_and_snapshots();churn();bounds_and_rollover();recovery_failures();bounded_lookup_and_invalidation();}
