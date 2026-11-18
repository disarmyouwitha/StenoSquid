#include "native_checkpoint_job.h"
#include "native_live_fixture.h"
#include <iostream>
using Job=NativeCheckpointJob<LiveDevice>;
SegmentMap Excluded(const LiveFixture &fixture) {
    SegmentMap result;if(fixture.segments<255)result.Add(fixture.segments,255-fixture.segments);return result;
}
unsigned Finish(LiveFixture &fixture,Job &job) {
    for(unsigned steps=1;steps<2000000;++steps) {
        const auto result=job.Step(fixture.CheckExtent());
        if(result==JobResult::Done){fixture.selection=job.Selection();fixture.Recover();return steps;}
        assert(result==JobResult::More);
    }
    assert(false);return 0;
}
void basic() {
    LiveFixture fixture;
    Job job(fixture.device,fixture.catalog,fixture.live);
    assert(sizeof(job)<6000);
    assert(fixture.Edit(1,1,"first")==LiveResult::Ok);
    assert(fixture.Edit(1,2,"second")==LiveResult::Ok);
    assert(fixture.Edit(2,1,"ç utf8")==LiveResult::Ok);
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::Busy);
    assert(fixture.Edit(1,1,"blocked")==LiveResult::Busy);
    unsigned steps=0;
    for(;;) {
        assert(fixture.Text(1,1,&job.Input())=="first");
        auto result=job.Step(fixture.CheckExtent());++steps;
        if(result==JobResult::Done)break;
        assert(result==JobResult::More);
        if(job.Published())assert(job.Cancel()==JobResult::Busy);
    }
    fixture.selection=job.Selection();fixture.Recover();
    assert(fixture.live.TailCount()==0 && fixture.Text(1,1)=="first" && fixture.Text(2,1)=="ç utf8");
    assert(fixture.Edit(1,1,"new")==LiveResult::Ok);
    assert(fixture.Edit(1,2,"",2)==LiveResult::Ok);
    assert(fixture.Edit(2,1,"",1)==LiveResult::Ok);
    LiveView older;assert(fixture.live.Snapshot(older)==LiveResult::Ok);
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);
    Finish(fixture,job);
    assert(fixture.Text(1,1)=="new" && fixture.Text(1,2)=="<absent>" && fixture.Text(2,1)=="<absent>");
    assert(fixture.Text(1,1,&older)=="new");
    assert(fixture.Edit(1,1,"next")==LiveResult::Ok);
    const auto bytes=fixture.nor.data;
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);
    JobResult status;
    do{status=job.Step(fixture.CheckExtent());}while(status==JobResult::More);
    assert(status==JobResult::Busy && fixture.live.Ready() && fixture.Text(1,1)=="next");
    // Staged pages may differ, but retained catalog slots are still untouched.
    assert(!std::memcmp(bytes.data(),fixture.nor.data.data(),2*65536));
    assert(fixture.live.Release(older));
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);Finish(fixture,job);
    std::cout<<"Native checkpoint job: "<<steps<<" initial steps; live snapshots, reset/remove, pin backpressure and reuse passed; workspace "<<sizeof(job)<<" bytes on host\n";
}
void cancellation_and_refusal() {
    LiveFixture fixture;assert(fixture.Edit(1,1,"kept")==LiveResult::Ok);
    const auto baseline=fixture.nor;
    Job dry(fixture.device,fixture.catalog,fixture.live);
    assert(dry.Begin(fixture.selection,Excluded(fixture))==JobResult::More);
    const unsigned steps=Finish(fixture,dry);
    unsigned cancelled=0,committed=0;
    for(unsigned point=0;point<steps;++point) {
        LiveFixture trial;trial.nor=baseline;trial.Recover();Job job(trial.device,trial.catalog,trial.live);
        assert(job.Begin(trial.selection,Excluded(trial))==JobResult::More);
        for(unsigned i=0;i<point;++i)assert(job.Step(trial.CheckExtent())==JobResult::More);
        if(job.Published()) {
            assert(job.Cancel()==JobResult::Busy);Finish(trial,job);
            assert(trial.Text(1,1)=="kept");++committed;
        } else {
            assert(job.Cancel()==JobResult::Cancelled && trial.live.Ready());
            assert(trial.Edit(1,1,"after cancel")==LiveResult::Ok);
            assert(job.Begin(trial.selection,Excluded(trial))==JobResult::More);Finish(trial,job);
            assert(trial.Text(1,1)=="after cancel");++cancelled;
        }
    }
    LiveFixture small(7);assert(small.Edit(1,1,"safe")==LiveResult::Ok);Job refused(small.device,small.catalog,small.live);
    auto bytes=small.nor.data;
    assert(refused.Begin(small.selection,Excluded(small))==JobResult::More);
    JobResult result;do{result=refused.Step(small.CheckExtent());}while(result==JobResult::More);
    assert(result==JobResult::NoSpace && small.nor.data==bytes && small.live.Ready());
    std::cout<<"Native checkpoint job: "<<cancelled<<" prepublication cancellation/reuse points, "<<committed<<" postcommit cancellation refusals; no-space preflight passed\n";
}
void power_cuts() {
    LiveFixture source;assert(source.Edit(1,1,"original")==LiveResult::Ok);
    assert(source.Checkpoint()==CatalogResult::Ok);
    assert(source.Edit(1,1,"latest")==LiveResult::Ok);
    assert(source.Edit(2,2,"removed")==LiveResult::Ok);
    assert(source.Edit(2,2,"",1)==LiveResult::Ok);
    const auto baseline=source.nor;
    Job dry(source.device,source.catalog,source.live);
    assert(dry.Begin(source.selection,Excluded(source))==JobResult::More);source.nor.operations=0;Finish(source,dry);
    const auto operations=source.nor.operations;unsigned cases=0;
    for(unsigned point=1;point<=operations;++point)for(unsigned tear:{0u,1u,127u,4095u}) {
        Nor interrupted;
        {
            LiveFixture trial;trial.nor=baseline;trial.Recover();Job job(trial.device,trial.catalog,trial.live);
            assert(job.Begin(trial.selection,Excluded(trial))==JobResult::More);
            trial.nor.operations=0;trial.nor.cut=point;trial.nor.tear=tear;
            try{Finish(trial,job);assert(false);}catch(const Cut &){}
            interrupted=trial.nor;
        }
        interrupted.cut=0;
        LiveFixture reboot;reboot.nor=interrupted;const auto bytes=reboot.nor.data;reboot.Recover();
        assert(reboot.nor.data==bytes && reboot.Text(1,1)=="latest" && reboot.Text(2,2)=="<absent>");
        if(reboot.selection.quarantine)assert(reboot.Edit(1,1,"no")==LiveResult::ReadOnly);
        else {assert(reboot.Edit(1,3,"next")==LiveResult::Ok);reboot.Recover();assert(reboot.Text(1,3)=="next");}
        ++cases;
    }
    std::cout<<"Native checkpoint job: "<<cases<<" torn native coordinator operations preserve committed edits after fresh recovery\n";
}
void metadata_and_multistroke() {
    LiveFixture fixture;
    uint32_t key[16];for(unsigned i=0;i<16;++i)key[i]=i+1;
    const uint8_t text[]={0xc3,0xa7,'a',' ','v','a'};
    assert(fixture.live.Append(1,0,key,16,text,sizeof(text))==LiveResult::Ok);
    assert(fixture.live.Append(1,0,key,15,nullptr,0)==LiveResult::Ok);
    assert(fixture.Checkpoint()==CatalogResult::Ok);
    // Put index descriptors before the journal/base to force a base-index remap.
    auto data=fixture.Payload(2,fixture.forward,fixture.forward_bytes,
        Crc(0,fixture.nor.data.data()+fixture.forward*65536,fixture.forward_bytes),fixture.reverse,fixture.reverse_bytes,
        Crc(0,fixture.nor.data.data()+fixture.reverse*65536,fixture.reverse_bytes));
    uint8_t journal[24];std::memcpy(journal,data.data()+224,24);
    std::memmove(data.data()+224,data.data()+248,48);std::memcpy(data.data()+272,journal,24);
    data.resize(data.size()+24);data[6]=4;
    assert(fixture.device.Erase(10));const uint8_t base[]={'B','A','S','E'};
    assert(fixture.device.Program(10*65536,base,sizeof(base)));
    auto *extent=data.data()+296;extent[0]=10;extent[2]=1;PutWord(extent+4,4);
    PutWord(extent+8,Crc(0,base,4));PutWord(extent+12,1);
    data[40]=3;data[41]=0;data[48]='N';for(unsigned i=0;i<32;++i)data[96+i]=uint8_t(i+1);
    CatalogSelection next;
    assert(fixture.catalog.Publish(fixture.selection,data.size(),Source{data},fixture.CheckExtent(),next)==CatalogResult::Ok);
    fixture.selection=next;fixture.Recover();
    assert(fixture.live.Append(1,0,key,16,text,sizeof(text))==LiveResult::Ok);
    Job job(fixture.device,fixture.catalog,fixture.live);
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);Finish(fixture,job);
    uint8_t row[96];assert(fixture.device.Read(fixture.selection.root.address+32,row,96));
    assert(row[8]==1 && row[9]==0 && row[16]=='N');
    for(unsigned i=0;i<32;++i)assert(row[64+i]==i+1);
    assert(!std::memcmp(fixture.nor.data.data()+10*65536,base,4));
    LiveView view;assert(fixture.live.Snapshot(view)==LiveResult::Ok);RecordView value;
    assert(fixture.live.FindSnapshot(view,1,key,16,value)==LiveResult::Ok && value.text_bytes==6);
    uint8_t actual[6];assert(fixture.live.ReadText(view,value,actual,6)==LiveResult::Ok && !std::memcmp(actual,text,6));
    assert(fixture.live.FindSnapshot(view,1,key,15,value)==LiveResult::Ok && value.text_bytes==0);
    assert(fixture.live.Release(view));
    // All overrides reset: publish no empty index extents, retain only storage.
    assert(fixture.live.Append(1,2,key,16,nullptr,0)==LiveResult::Ok);
    assert(fixture.live.Append(1,2,key,15,nullptr,0)==LiveResult::Ok);
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);Finish(fixture,job);
    assert(!fixture.forward_bytes && !fixture.reverse_bytes);
    std::cout<<"Native checkpoint job: 16-stroke UTF-8/empty translations, source hashes, base descriptor remapping and all-reset empty indexes passed\n";
}
void failures() {
    LiveFixture fixture;assert(fixture.Edit(1,1,"committed")==LiveResult::Ok);
    Job job(fixture.device,fixture.catalog,fixture.live);
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);
    fixture.nor.fail_after_commit=true;
    JobResult result;do{result=job.Step(fixture.CheckExtent());}while(result==JobResult::More);
    assert(result==JobResult::RecoveryRequired && !job.Published());
    fixture.nor.fail_read=false;
    assert(fixture.Edit(1,1,"unsafe")==LiveResult::Stale);
    fixture.Recover();assert(fixture.Text(1,1)=="committed" && fixture.selection.root.generation==2);
    assert(fixture.Edit(1,1,"after reopen")==LiveResult::Ok);
    // Staged output damage must never become a catalog pointer.
    LiveFixture damaged;assert(damaged.Edit(1,1,"safe")==LiveResult::Ok);
    Job corrupt(damaged.device,damaged.catalog,damaged.live);
    const auto catalogs=damaged.nor.data;
    assert(corrupt.Begin(damaged.selection,Excluded(damaged))==JobResult::More);
    bool injected=false;
    do {
        result=corrupt.Step(damaged.CheckExtent());
        if(!injected && damaged.nor.data[3*65536]!=255) {
            damaged.nor.data[3*65536]^=1;injected=true;
        }
    }while(result==JobResult::More);
    assert(injected && result==JobResult::Corrupt && !corrupt.Published());
    assert(!std::memcmp(catalogs.data(),damaged.nor.data.data(),2*65536));
    assert(damaged.Text(1,1)=="safe");
    std::cout<<"Native checkpoint job: ambiguous catalog commit requires reopen; staged corruption refuses publication passed\n";
}
void multisegment_indexes() {
    LiveFixture fixture(64);
    constexpr uint32_t count=20000;
    std::vector<uint32_t> addresses;
    uint32_t segment=2,address=segment*65536+256;
    for(uint32_t key=1;key<=count;++key) {
        const std::string text="word"+std::to_string(key);
        auto append=[&](){return fixture.journal.Append(address,key,1,0,&key,1,
            reinterpret_cast<const uint8_t *>(text.data()),text.size());};
        auto status=append();
        if(status==JournalResult::Full) {
            ++segment;assert(fixture.journal.Initialize(segment,segment-1)==JournalResult::Ok);
            fixture.journals.emplace_back(segment,segment-1);address=segment*65536+256;status=append();
        }
        assert(status==JournalResult::Ok);addresses.push_back(address);
        JournalRecord row;assert(fixture.journal.Read(address,row)==JournalResult::Ok);address+=row.span;
    }
    const uint32_t f=segment+1,r=f+2;
    IndexSink<LiveDevice> forward(fixture.device),reverse(fixture.device);
    for(uint32_t s=f;s<r+2;++s)assert(fixture.device.Erase(s));
    assert(forward.Begin(f*65536,2*65536) && reverse.Begin(r*65536,2*65536));
    for(auto a:addresses)assert(forward.Append(a));
    std::sort(addresses.begin(),addresses.end(),[&](uint32_t a,uint32_t b){auto x=fixture.Row(a),y=fixture.Row(b);return std::make_pair(x.text,x.key)<std::make_pair(y.text,y.key);});
    for(auto a:addresses)assert(reverse.Append(a));
    assert(forward.Finish() && reverse.Finish());
    auto data=fixture.Payload(count,f,forward.Bytes(),forward.Checksum(),r,reverse.Bytes(),reverse.Checksum());
    CatalogSelection next;
    assert(fixture.catalog.Publish(fixture.selection,data.size(),Source{data},fixture.CheckExtent(),next)==CatalogResult::Ok);
    fixture.selection=next;fixture.Recover();
    assert(fixture.Edit(1,1,"replacement")==LiveResult::Ok);
    assert(fixture.Edit(1,250,"",1)==LiveResult::Ok);
    assert(fixture.Edit(1,400,"",2)==LiveResult::Ok);
    assert(fixture.Edit(1,20001,"new")==LiveResult::Ok);
    Job job(fixture.device,fixture.catalog,fixture.live);
    assert(job.Begin(fixture.selection,Excluded(fixture))==JobResult::More);
    const auto steps=Finish(fixture,job);
    assert(fixture.forward_bytes>65536 && fixture.reverse_bytes>65536);
    assert(fixture.Text(1,1)=="replacement" && fixture.Text(1,250)=="<absent>" && fixture.Text(1,400)=="<absent>");
    assert(fixture.Text(1,20000)=="word20000" && fixture.Text(1,20001)=="new");
    std::cout<<"Native checkpoint job: "<<count<<" records, multi-segment output indexes and semantic coverage verified in "<<steps<<" bounded steps\n";
}
int main(){basic();cancellation_and_refusal();power_cuts();metadata_and_multistroke();failures();multisegment_indexes();}
