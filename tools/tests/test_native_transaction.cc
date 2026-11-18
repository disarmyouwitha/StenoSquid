#include "native_catalog.h"
#include "native_index_sink.h"
#include "native_test_nor.h"
#include <iostream>
using namespace candidate_b;
using namespace native_test;

std::vector<uint8_t> catalog_payload(uint32_t forward,uint32_t reverse,uint32_t fcrc,uint32_t rcrc,uint64_t through) {
    std::vector<uint8_t> data(32+96+72,0);
    PutWord(data.data(),2);data[4]=1;data[6]=3;PutWord(data.data()+8,through);
    auto *row=data.data()+32;PutWord(row,1);PutWord(row+4,1);row[8]=row[9]=255;row[12]=4;std::memcpy(row+16,"Test",4);
    for(unsigned i=0;i<3;++i) {
        auto *extent=data.data()+128+i*24;extent[0]=i==0?2:i==1?forward:reverse;extent[2]=1;
        PutWord(extent+4,i==0?65536:i==1?8:through==2?8:4);
        PutWord(extent+8,i==0?0:i==1?fcrc:rcrc);PutWord(extent+12,i==0?2:i==1?3:4);
        if(!i)PutWord(extent+16,1);
    }
    return data;
}
struct VerifyExtent {
    Nor &nor;
    bool operator()(uint32_t,const uint8_t *row) {
        uint32_t segment=uint16_t(row[0]) | uint16_t(row[1])<<8;
        uint32_t size=Word(row+4),kind=Word(row+12);
        if(segment>=7 || !size || size>65536)return false;
        if(kind==2)return segment==2 && Word(nor.data.data()+segment*65536+16)==Crc(0,nor.data.data()+segment*65536,16);
        return (kind==3 || kind==4) && !(size%4) && Crc(0,nor.data.data()+segment*65536,size)==Word(row+8);
    }
};
struct JournalDevice {
    Nor &nor;
    bool Read(uint32_t at,uint8_t *out,uint32_t n){return nor.Read(at,out,n);}
    bool Program(uint32_t at,const uint8_t *data,uint32_t n){return nor.Program(at,data,n);}
    bool Erase(uint32_t segment){return nor.EraseSegment(segment);}
};
int main() {
    Nor original;JournalDevice device{original};NativeJournal<JournalDevice> journal(device);
    assert(journal.Initialize(2,1)==JournalResult::Ok);
    uint32_t address=2*65536+256,records[4]{};
    const uint32_t key[]={1,2,1,2};const char *text[]={"cat","dog","new",""};
    for(unsigned i=0;i<4;++i) {
        records[i]=address;
        assert(journal.Append(address,i+1,1,i==3?1:0,key+i,1,reinterpret_cast<const uint8_t *>(text[i]),std::strlen(text[i]))==JournalResult::Ok);
        JournalRecord record;assert(journal.Read(address,record)==JournalResult::Ok);address+=record.span;
    }
    IndexSink<Nor> forward(original),reverse(original);
    assert(forward.Begin(3*65536,65536) && reverse.Begin(4*65536,65536));
    assert(forward.Append(records[0]) && forward.Append(records[1]) && forward.Finish());
    assert(reverse.Append(records[0]) && reverse.Append(records[1]) && reverse.Finish());
    auto first=catalog_payload(3,4,forward.Checksum(),reverse.Checksum(),2);
    CatalogSelection empty,old;
    NativeCatalog<Nor> catalog(original);
    assert(catalog.Publish(empty,first.size(),Source{first},VerifyExtent{original},old)==CatalogResult::Ok);
    auto transaction=[&](Nor &nor,CatalogSelection &next) {
        if(!nor.EraseSegment(5) || !nor.EraseSegment(6))return false;
        IndexSink<Nor> f(nor),r(nor);
        if(!f.Begin(5*65536,65536) || !r.Begin(6*65536,65536) || !f.Append(records[2]) ||
           !f.Append(records[3]) || !r.Append(records[2]) || !f.Finish() || !r.Finish())return false;
        auto second=catalog_payload(5,6,f.Checksum(),r.Checksum(),4);
        NativeCatalog<Nor> writer(nor);
        return writer.Publish(old,second.size(),Source{second},VerifyExtent{nor},next)==CatalogResult::Ok;
    };
    Nor dry=original;dry.operations=0;CatalogSelection final;
    assert(transaction(dry,final));
    unsigned cases=0;
    for(uint32_t point=1;point<=dry.operations;++point)for(uint32_t tear:{0u,1u,127u,4095u}) {
        Nor trial=original;trial.operations=0;trial.cut=point;trial.tear=tear;
        CatalogSelection result;
        try{transaction(trial,result);assert(false);}catch(const Cut &){}
        trial.cut=0;auto before=trial.data;
        NativeCatalog<Nor> recovered(trial);
        assert(recovered.Open(result,VerifyExtent{trial})==CatalogResult::Ok);
        assert(result.root.generation==1 || result.root.generation==2);
        assert(trial.data==before);
        // Every previously committed journal edit remains available to replay,
        // even when the catalog falls back to its older checkpoint boundary.
        JournalDevice replayDevice{trial};NativeJournal<JournalDevice> replay(replayDevice);
        JournalScan scan;JournalRecord row;
        assert(replay.BeginScan(2,1,0,scan)==JournalResult::Ok);
        unsigned seen=0;while(replay.Step(scan,row)==JournalResult::Ok)++seen;
        assert(seen==4);
        for(unsigned i=0;i<4;++i)assert(std::memcmp(trial.data.data()+records[i],original.data.data()+records[i],44)==0);
        // Selected root must refer to a complete, verified pair, never one old
        // table and one new table. A retry may reuse only unpublished extents.
        uint8_t f[24],r[24];
        assert(trial.Read(result.root.address+128+24,f,24) && trial.Read(result.root.address+128+48,r,24));
        assert((result.root.generation==1 && f[0]==3 && r[0]==4) ||
               (result.root.generation==2 && f[0]==5 && r[0]==6));
        if(result.root.generation==1)assert(transaction(trial,final));
        ++cases;
    }
    std::cout<<"Native combined transaction: "<<cases<<" torn staging/index/catalog cases; root-pair atomicity and committed edit preservation passed\n";
}
