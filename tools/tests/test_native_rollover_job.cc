#include "native_rollover_job.h"
#include "native_live_fixture.h"
#include <iostream>
using Job=NativeRolloverJob<LiveDevice>;
SegmentMap Excluded(const LiveFixture &fixture){SegmentMap out;out.Add(fixture.segments,255-fixture.segments);return out;}
unsigned Finish(LiveFixture &fixture,Job &job) {
    for(unsigned i=1;i<100000;++i) {
        const auto result=job.Step(fixture.CheckExtent());
        if(result==JobResult::Done){fixture.selection=job.Selection();fixture.Recover();return i;}
        assert(result==JobResult::More);
    }
    assert(false);return 0;
}
void basic() {
    LiveFixture fixture;const std::string text(2047,'x');unsigned count=0;
    while(fixture.Edit(1,1,text)==LiveResult::Ok)++count;
    assert(count==31);LiveView older;assert(fixture.live.Snapshot(older)==LiveResult::Ok);
    Job job(fixture.device,fixture.catalog,fixture.live);static_assert(sizeof(job)<2200);
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);
    assert(fixture.Edit(1,1,"no")==LiveResult::Busy);
    const auto steps=Finish(fixture,job);
    assert(fixture.live.TailCount()==31 && fixture.live.Highwater()==31);
    assert(fixture.Edit(1,1,"new segment")==LiveResult::Ok);
    assert(fixture.Text(1,1)=="new segment" && fixture.Text(1,1,&older)==text);
    assert(fixture.live.Release(older));fixture.Recover();assert(fixture.Text(1,1)=="new segment");
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);
    assert(job.Cancel()==JobResult::Cancelled && fixture.live.Ready());
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);Finish(fixture,job);
    assert(fixture.Edit(1,2,"again")==LiveResult::Ok);fixture.Recover();assert(fixture.Text(1,2)=="again");
    LiveFixture small(6);assert(small.Edit(1,1,"kept")==LiveResult::Ok);Job refused(small.device,small.catalog,small.live);
    const auto bytes=small.nor.data;
    assert(refused.Begin(small.selection,Excluded(small))==JobResult::More);
    JobResult result;do{result=refused.Step(small.CheckExtent());}while(result==JobResult::More);
    assert(result==JobResult::NoSpace && small.nor.data==bytes && small.live.Ready());
    std::cout<<"Native rollover job: "<<steps<<" steps, maximum texts, preserved tail/snapshots, cancellation/reuse and reserve refusal passed; host workspace "<<sizeof(job)<<" bytes\n";
}
void cuts() {
    LiveFixture source;assert(source.Edit(1,1,"committed")==LiveResult::Ok);const auto baseline=source.nor;
    Job dry(source.device,source.catalog,source.live);assert(dry.Begin(source.selection,Excluded(source))==JobResult::More);
    source.nor.operations=0;Finish(source,dry);const auto operations=source.nor.operations;
    unsigned cases=0;
    for(unsigned point=1;point<=operations;++point)for(unsigned tear:{0u,1u,127u,4095u}) {
        Nor interrupted;
        {
            LiveFixture running;running.nor=baseline;running.Recover();Job job(running.device,running.catalog,running.live);
            assert(job.Begin(running.selection,Excluded(running))==JobResult::More);
            running.nor.operations=0;running.nor.cut=point;running.nor.tear=tear;
            try{Finish(running,job);assert(false);}catch(const Cut &){}
            interrupted=running.nor;
        }
        interrupted.cut=0;LiveFixture boot;boot.nor=interrupted;const auto bytes=boot.nor.data;
        boot.Recover();assert(boot.nor.data==bytes && boot.Text(1,1)=="committed");
        if(boot.selection.quarantine)assert(boot.Edit(1,1,"no")==LiveResult::ReadOnly);
        else {assert(boot.Edit(1,2,"next")==LiveResult::Ok);boot.Recover();assert(boot.Text(1,2)=="next");}
        ++cases;
    }
    std::cout<<"Native rollover job: "<<cases<<" torn initialization/publication cases preserve committed edits after fresh recovery\n";
}
void empty_catalog_and_epoch_limit() {
    LiveFixture empty;
    empty.journals.clear();auto data=empty.Payload(0,0,0,0,0,0,0);CatalogSelection next;
    assert(empty.catalog.Publish(empty.selection,data.size(),Source{data},empty.CheckExtent(),next)==CatalogResult::Ok);
    empty.selection=next;empty.Recover();assert(empty.Edit(1,1,"not yet")==LiveResult::NeedJournal);
    Job first(empty.device,empty.catalog,empty.live);
    const auto previous=std::vector<uint8_t>(empty.nor.data.begin()+next.root.address,empty.nor.data.begin()+next.root.address+next.root.bytes);
    assert(first.Begin(empty.selection,Excluded(empty))==JobResult::More);Finish(empty,first);
    const auto at_new=empty.selection.root.address;
    assert(!std::memcmp(previous.data(),empty.nor.data.data()+at_new,6));
    assert(!std::memcmp(previous.data()+8,empty.nor.data.data()+at_new+8,previous.size()-8));
    assert(empty.Edit(1,1,"first journal")==LiveResult::Ok);empty.Recover();assert(empty.Text(1,1)=="first journal");
    // Explicitly construct a fresh test image whose last epoch cannot increment.
    LiveFixture max;
    max.catalog.Invalidate();assert(max.nor.EraseCatalog(0,65536) && max.nor.EraseCatalog(65536,65536));
    assert(max.journal.Initialize(2,UINT64_MAX)==JournalResult::Ok);max.journals={{2,UINT64_MAX}};
    data=max.Payload(0,0,0,0,0,0,0);CatalogSelection virgin;
    assert(max.catalog.Publish(virgin,data.size(),Source{data},max.CheckExtent(),next)==CatalogResult::Ok);
    max.selection=next;max.Recover();const auto bytes=max.nor.data;
    Job limit(max.device,max.catalog,max.live);assert(limit.Begin(max.selection,Excluded(max))==JobResult::More);
    JobResult status;do{status=limit.Step(max.CheckExtent());}while(status==JobResult::More);
    assert(status==JobResult::NoSpace && max.nor.data==bytes && max.live.Ready());
    std::cout<<"Native rollover job: empty catalog first-journal provisioning, metadata copy and epoch-wrap refusal passed\n";
}
int main(){basic();cuts();empty_catalog_and_epoch_limit();}
