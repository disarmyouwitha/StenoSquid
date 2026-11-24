#include "native_journal.h"
#include "native_allocator.h"
#include <cassert>
#include <fstream>
#include <iterator>
#include <iostream>
#include <vector>
using namespace candidate_b;
struct Cut {};
struct Nor {
    std::vector<uint8_t> data;
    unsigned operations=0, cut=0, tear=0;
    bool fail_read=false, damage_write=false;
    explicit Nor(std::vector<uint8_t> image) : data(std::move(image)) {}
    bool Read(uint32_t at,uint8_t *out,uint32_t n) {
        if (fail_read || at>data.size() || n>data.size()-at) return false;
        std::memcpy(out,data.data()+at,n); return true;
    }
    bool Program(uint32_t at,const uint8_t *bytes,uint32_t n) {
        assert(n && at/256==(at+n-1)/256 && at+n<=data.size());
        for (uint32_t i=0;i<n;++i) assert((data[at+i]&bytes[i])==bytes[i]);
        const bool interrupted=++operations==cut;
        uint32_t amount=interrupted ? (tear<n?tear:n) : n;
        for(uint32_t i=0;i<amount;++i) data[at+i]&=bytes[i];
        if(damage_write && amount) data[at+amount-1]^=1;
        if(interrupted) throw Cut{};
        return true;
    }
    bool Erase(uint32_t segment) {
        assert((segment+1)*65536<=data.size());
        for(uint32_t at=segment*65536;at<(segment+1)*65536;at+=4096) {
            bool interrupted=++operations==cut;
            uint32_t n=interrupted?(tear<4096?tear:4096):4096;
            std::memset(data.data()+at,255,n);
            if(interrupted) throw Cut{};
        }
        return true;
    }
};
std::vector<uint8_t> load(const char *path) {
    std::ifstream file(path,std::ios::binary);
    return {(std::istreambuf_iterator<char>(file)),{}};
}
int main(int argc,char **argv) {
    assert(argc==3);
    SegmentMap protectedMap;
    assert(protectedMap.Add(10,4));
    const auto unchangedMap=protectedMap;
    assert(!protectedMap.Add(kMaxSegments-1,2));
    assert(!protectedMap.Add(UINT32_MAX,2));
    assert(std::memcmp(&unchangedMap,&protectedMap,sizeof(protectedMap))==0);
    SegmentMap pinned; assert(pinned.Add(2,3)); protectedMap.Union(pinned);
    AllocationPlan plan(protectedMap);
    uint32_t allocated=999;
    assert(plan.Reserve(4,allocated) && allocated==5);
    assert(plan.Reserve(6,allocated) && allocated==14);
    auto oldMap=plan.Map();
    assert(!plan.Reserve(0,allocated));
    assert(!plan.Reserve(254,allocated));
    assert(std::memcmp(&oldMap,&plan.Map(),sizeof(oldMap))==0);
    SegmentMap fragmented;
    for(uint32_t i=2;i<255;i+=2) assert(fragmented.Add(i,1));
    AllocationPlan fragmentedPlan(fragmented);
    assert(fragmentedPlan.Free()>100 && !fragmentedPlan.Reserve(2,allocated));
    SegmentMap empty;
    AllocationPlan full(empty);
    assert(full.Reserve(250,allocated) && allocated==2 && full.Free()==3);
    assert(!full.Reserve(1,allocated));
    // Deterministic comparisons against an independent boolean first-fit model.
    uint32_t rng=0x125fb16;
    for(unsigned iteration=0;iteration<20000;++iteration) {
        auto random=[&]() { rng^=rng<<13; rng^=rng>>17; rng^=rng<<5; return rng; };
        SegmentMap used; bool model[256]{};
        model[0]=model[1]=model[255]=true;
        for(uint32_t i=2;i<255;++i) if(random()%3==0) { used.Add(i,1); model[i]=true; }
        AllocationPlan candidate(used);
        for(unsigned request=0;request<8;++request) {
            uint32_t length=random()%20, free=0, expected=999, run=0;
            for(uint32_t i=2;i<255;++i) free+=!model[i];
            if(length && length<=free && free-length>=3) {
                for(uint32_t i=2;i<255;++i) {
                    run=model[i]?0:run+1;
                    if(run==length) { expected=i+1-length; break; }
                }
            }
            uint32_t actual=999;
            assert(candidate.Reserve(length,actual)==(expected!=999));
            assert(actual==expected);
            if(expected!=999) for(uint32_t i=actual;i<actual+length;++i) model[i]=true;
            for(uint32_t i=0;i<256;++i) assert(candidate.Map().Contains(i)==model[i]);
        }
    }
    Nor nor(load(argv[1])); NativeJournal<Nor> journal(nor);
    static_assert(sizeof(journal)<=272);
    assert(journal.Open(2,1)==JournalResult::Ok);
    JournalRecord record;
    uint32_t at=2*65536+256;
    for(unsigned i=1;i<=4;++i) {
        assert(journal.Read(at,record)==JournalResult::Ok && record.sequence==i);
        at+=record.span;
    }
    assert(journal.Read(at,record)==JournalResult::End);
    JournalScan scan;
    static_assert(sizeof(scan)<=24);
    assert(journal.BeginScan(2,1,0,scan)==JournalResult::Ok);
    for(unsigned i=1;i<=4;++i) {
        assert(journal.Step(scan,record)==JournalResult::Ok && record.sequence==i);
    }
    assert(journal.Step(scan,record)==JournalResult::End && scan.next==at);
    assert(journal.Open(2,2)==JournalResult::Corrupt);
    assert(journal.BeginScan(1,1,0,scan)==JournalResult::Invalid && !scan.ready);
    assert(journal.BeginScan(2,1,4,scan)==JournalResult::Ok);
    const uint32_t initial=scan.next;
    assert(journal.Step(scan,record)==JournalResult::Corrupt && scan.next==initial);
    nor.fail_read=true;
    assert(journal.Step(scan,record)==JournalResult::IoError && scan.next==initial);
    nor.fail_read=false;
    uint32_t strokes[]={8,256};
    const uint8_t text[]={0xc3,0xa7,'a',' ','v','a'};
    assert(journal.Append(at,5,2,0,strokes,2,text,sizeof(text))==JournalResult::Ok);
    std::ofstream output(argv[2],std::ios::binary);
    output.write(reinterpret_cast<char *>(nor.data.data()),nor.data.size()); output.close();
    unsigned cases=0;
    // Header start at every aligned page offset, short and maximum payloads.
    for(unsigned alignment=0;alignment<256;alignment+=4) {
        for(unsigned length : {0u,2047u}) {
            Nor original(std::vector<uint8_t>(3*65536,255));
            NativeJournal<Nor> writer(original);
            assert(writer.Initialize(2,1)==JournalResult::Ok);
            uint32_t address=2*65536+256+alignment;
            std::vector<uint8_t> value(length,'x');
            Nor dry=original; dry.operations=0;
            NativeJournal<Nor> dryWriter(dry);
            assert(dryWriter.Append(address,1,1,0,strokes,2,value.data(),length)==JournalResult::Ok);
            for(unsigned cut=1;cut<=dry.operations;++cut) for(unsigned tear : {0u,1u,3u,127u}) {
                Nor trial=original; trial.operations=0; trial.cut=cut; trial.tear=tear;
                NativeJournal<Nor> w(trial);
                try { w.Append(address,1,1,0,strokes,2,value.data(),length); assert(false); }
                catch(const Cut &) {}
                trial.cut=0;
                auto before=trial.data;
                NativeJournal<Nor> restored(trial);
                auto status=restored.Read(address,record);
                assert(status==JournalResult::Ok || status==JournalResult::End || status==JournalResult::Sealed);
                assert(trial.data==before);
                if(status==JournalResult::Ok) assert(record.sequence==1);
                ++cases;
            }
            Nor corrupted=original; corrupted.damage_write=true;
            NativeJournal<Nor> broken(corrupted);
            assert(broken.Append(address,1,1,0,strokes,2,value.data(),length)!=JournalResult::Ok);
            assert(Word(corrupted.data.data()+address+28)!=kCommit);
        }
    }
    auto unchanged=nor.data;
    assert(journal.Append(at,6,1,0,strokes,2,text,sizeof(text))==JournalResult::Sealed);
    assert(nor.data==unchanged);
    const uint8_t invalid[]={0xc0,0x80};
    assert(journal.Append(at+4096,6,1,0,strokes,2,invalid,2)==JournalResult::Invalid);
    assert(journal.Append(at+4096,6,1,1,strokes,2,text,6)==JournalResult::Invalid);
    assert(journal.Append(at+4096,6,1,0,strokes,17,text,6)==JournalResult::Invalid);
    assert(nor.data==unchanged);
    // Even CRC-consistent committed bytes must satisfy semantic/UTF-8 limits.
    for(const std::vector<uint8_t> &invalidText : {
            std::vector<uint8_t>{0}, {0xc0,0x80}, {0xed,0xa0,0x80},
            {0xf4,0x90,0x80,0x80}, {0xc3}, {0x80}}) {
        Nor invalidNor=nor;
        uint8_t *header=invalidNor.data.data()+at;
        const uint32_t payloadSize=16+invalidText.size();
        PutWord(header+4,(payloadSize+35)&~3u); PutWord(header+16,payloadSize);
        header[32+6]=uint8_t(invalidText.size()); header[32+7]=0;
        std::memcpy(header+48,invalidText.data(),invalidText.size());
        PutWord(header+20,Crc(Crc(0,header+8,8),header+32,payloadSize));
        PutWord(header+24,Crc(0,header,24));
        NativeJournal<Nor> invalidReader(invalidNor);
        assert(invalidReader.Read(at,record)==JournalResult::Corrupt);
    }
    // Interrupted explicit initialization never invents a valid epoch/header.
    Nor blank(std::vector<uint8_t>(3*65536,255));
    Nor initialized=blank;
    NativeJournal<Nor> init(initialized);
    assert(init.Initialize(2,1)==JournalResult::Ok);
    unsigned initCases=0;
    for(unsigned cut=1;cut<=initialized.operations;++cut) for(unsigned tear : {0u,1u,127u,4095u}) {
        Nor trial=blank; trial.cut=cut; trial.tear=tear;
        NativeJournal<Nor> initTrial(trial);
        try { initTrial.Initialize(2,1); assert(false); } catch(const Cut &) {}
        trial.cut=0;
        auto saved=trial.data;
        auto status=initTrial.Open(2,1);
        assert(status==JournalResult::Ok || status==JournalResult::Corrupt);
        assert(trial.data==saved);
        ++initCases;
    }
    nor.fail_read=true;
    assert(journal.Open(2,1)==JournalResult::IoError);
    assert(journal.Read(at,record)==JournalResult::IoError);
    nor.fail_read=false;
    nor.data[at+40]^=1;
    assert(journal.Read(at,record)==JournalResult::Corrupt);
    std::cout<<"Native binary journal: bidirectional Python parity; "<<cases
             <<" torn append cases; "<<initCases<<" torn initialization cases; fixed workspace and commit/readback checks passed\n";
    std::cout<<"Native allocator: 160000 randomized first-fit requests; fragmentation, pins and reserve checks passed\n";
}
