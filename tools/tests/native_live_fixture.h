// Real native components with a host-only orchestration/oracle. STL here is test
// machinery, not proposed firmware RAM usage or a second production store.
#pragma once
#include "native_live_journal.h"
#include "native_ownership.h"
#include "native_checkpoint.h"
#include "native_index_sink.h"
#include "native_test_nor.h"
#include <map>
#include <string>
#include <algorithm>
using namespace candidate_b;
using namespace native_test;
using LiveKey=std::pair<uint32_t,std::vector<uint32_t>>;
struct TestRow {LiveKey key;uint8_t operation;std::string text;uint64_t sequence;};
struct LiveDevice {
    Nor &nor;
    bool Read(uint32_t at,uint8_t *out,uint32_t n){return nor.Read(at,out,n);}
    bool Program(uint32_t at,const uint8_t *in,uint32_t n){return nor.Program(at,in,n);}
    bool Erase(uint32_t segment){return nor.EraseSegment(segment);}
    bool EraseIndex(uint32_t segment, uint32_t bytes) {
        assert(segment >= 2 && segment < nor.data.size() / kSegmentBytes);
        assert(bytes == kEraseSectorBytes || bytes == kSegmentBytes);
        return nor.EraseBytes(segment * kSegmentBytes, bytes);
    }
    bool ErasePoolSector(uint32_t at) {
        assert(at >= 2 * kSegmentBytes && !(at % kEraseSectorBytes));
        return nor.EraseBytes(at,kEraseSectorBytes);
    }
    bool EraseCatalog(uint32_t at,uint32_t n){return nor.EraseCatalog(at,n);}
};
struct LiveFixture {
    Nor nor;
    LiveDevice device{nor};
    NativeCatalog<LiveDevice> catalog{device};
    NativeJournal<LiveDevice> journal{device};
    LiveJournal<LiveDevice> live{device,catalog};
    CatalogSelection selection;
    unsigned segments;
    std::vector<std::pair<uint32_t,uint64_t>> journals{{2,1}};
    uint32_t forward=0,forward_bytes=0,reverse=0,reverse_bytes=0;

    auto CheckExtent() {
        return [this](uint32_t,const uint8_t *extent) {
            uint32_t start=uint16_t(extent[0]) | uint16_t(extent[1])<<8,bytes=Word(extent+4),kind=Word(extent+12);
            uint32_t count=uint16_t(extent[2]) | uint16_t(extent[3])<<8;
            if(start>=segments || !count || count>segments-start || !bytes || bytes>count*65536)return false;
            if(kind==1)return Crc(0,nor.data.data()+start*65536,bytes)==Word(extent+8);
            if(kind==2) {
                uint64_t epoch=uint64_t(Word(extent+16)) | uint64_t(Word(extent+20))<<32;
                JournalScan scan;
                if(journal.BeginScan(start,epoch,0,scan)!=JournalResult::Ok)return false;
                JournalRecord row;JournalResult status;
                while((status=journal.Step(scan,row))==JournalResult::Ok){}
                return status==JournalResult::End || status==JournalResult::Sealed;
            }
            return (kind==3 || kind==4) && !(bytes%4) && Crc(0,nor.data.data()+start*65536,bytes)==Word(extent+8);
        };
    }
    explicit LiveFixture(unsigned count=16):segments(count) {
        nor.data.resize(count*65536,255);
        assert(journal.Initialize(2,1)==JournalResult::Ok);
        auto payload=Payload(0,0,0,0,0,0,0);
        CatalogSelection empty;
        assert(catalog.Publish(empty,payload.size(),Source{payload},CheckExtent(),selection)==CatalogResult::Ok);
        Recover();
    }
    TestRow Row(uint32_t address) {
        JournalRecord record;assert(journal.Read(address,record)==JournalResult::Ok);
        uint8_t prefix[8];assert(device.Read(address+32,prefix,8));
        TestRow row{{Word(prefix),{}},prefix[4],{},record.sequence};
        for(unsigned i=0;i<prefix[5];++i) {
            uint8_t mask[4];assert(device.Read(address+40+i*4,mask,4));row.key.second.push_back(Word(mask));
        }
        uint16_t bytes=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        row.text.resize(bytes);
        if(bytes)assert(device.Read(address+40+prefix[5]*4,reinterpret_cast<uint8_t *>(row.text.data()),bytes));
        return row;
    }
    std::vector<uint8_t> Payload(uint64_t through,uint32_t f,uint32_t fbytes,uint32_t fcrc,
                                 uint32_t r,uint32_t rbytes,uint32_t rcrc) {
        uint32_t extents=journals.size()+unsigned(fbytes!=0)+unsigned(rbytes!=0);
        std::vector<uint8_t> data(32+192+extents*24,0);
        PutWord(data.data(),3);data[4]=2;data[6]=extents;
        PutWord(data.data()+8,uint32_t(through));PutWord(data.data()+12,uint32_t(through>>32));
        for(unsigned i=0;i<2;++i) {
            uint8_t *row=data.data()+32+i*96;PutWord(row,i+1);PutWord(row+4,1);row[8]=row[9]=255;
            row[12]=4;std::memcpy(row+16,"Test",4);
        }
        unsigned position=0;
        auto extent=[&](uint32_t start,uint32_t bytes,uint32_t crc,uint32_t kind,uint64_t epoch) {
            uint8_t *row=data.data()+224+position++*24;
            row[0]=start;row[2]=(bytes+65535)/65536;PutWord(row+4,bytes);PutWord(row+8,crc);PutWord(row+12,kind);
            PutWord(row+16,uint32_t(epoch));PutWord(row+20,uint32_t(epoch>>32));
        };
        for(auto j:journals)extent(j.first,65536,0,2,j.second);
        if(fbytes)extent(f,fbytes,fcrc,3,0);
        if(rbytes)extent(r,rbytes,rcrc,4,0);
        return data;
    }
    CatalogResult ValidateRoot(const CatalogRoot &root) {
        auto status=catalog.Validate(root,CheckExtent());if(status!=CatalogResult::Ok)return status;
        uint8_t prefix[32];assert(device.Read(root.address,prefix,32));
        uint32_t nd=prefix[4],ne=prefix[6];uint64_t through=uint64_t(Word(prefix+8)) | uint64_t(Word(prefix+12))<<32;
        std::map<LiveKey,uint32_t> expected;
        std::vector<uint32_t> tables[2];
        bool outline_first = false;
        for(unsigned i=0;i<ne;++i) {
            uint8_t extent[24];assert(device.Read(root.address+32+nd*96+i*24,extent,24));
            uint32_t start=extent[0],kind=Word(extent+12);
            if(kind==2) {
                JournalScan scan;assert(journal.BeginScan(start,uint64_t(Word(extent+16)) | uint64_t(Word(extent+20))<<32,0,scan)==JournalResult::Ok);
                JournalRecord record;
                while(journal.Step(scan,record)==JournalResult::Ok)if(record.sequence<=through) {
                    auto row=Row(record.address);
                    if(row.operation==2)expected.erase(row.key);else expected[row.key]=record.address;
                }
            } else if(kind==3 || kind==4) {
                if (kind == 3) outline_first = Word(extent + 16) == kOutlineFirst;
                for(unsigned offset=0;offset<Word(extent+4);offset+=4) {
                    uint8_t address[4];assert(device.Read(start*65536+offset,address,4));tables[kind==4].push_back(Word(address));
                }
            }
        }
        std::vector<uint32_t> f,r;
        for(auto entry:expected){f.push_back(entry.second);if(Row(entry.second).operation==0)r.push_back(entry.second);}
        if (outline_first) std::sort(f.begin(), f.end(), [&](uint32_t a, uint32_t b) {
            const auto x = Row(a), y = Row(b);
            return std::tie(x.key.second, x.key.first) < std::tie(y.key.second, y.key.first);
        });
        std::sort(r.begin(),r.end(),[&](uint32_t a,uint32_t b){auto x=Row(a),y=Row(b);return std::make_pair(x.text,x.key)<std::make_pair(y.text,y.key);});
        return tables[0]==f && tables[1]==r?CatalogResult::Ok:CatalogResult::Corrupt;
    }
    void Recover() {
        const auto status=catalog.OpenValidated(selection,[&](const CatalogRoot &root){return ValidateRoot(root);});
        assert(status==CatalogResult::Ok || status==CatalogResult::Quarantined);
        // Rebuild orchestration metadata from persistent bytes too. Never retain
        // a staged journal/index topology across a simulated reboot.
        journals.clear();forward=forward_bytes=reverse=reverse_bytes=0;
        uint8_t prefix[32];assert(device.Read(selection.root.address,prefix,32));
        const uint32_t dictionaries=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
        const uint32_t extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        for(uint32_t i=0;i<extents;++i) {
            uint8_t row[24];assert(device.Read(selection.root.address+32+dictionaries*96+i*24,row,24));
            const uint32_t start=uint16_t(row[0]) | uint16_t(row[1])<<8,kind=Word(row+12);
            if(kind==2)journals.emplace_back(start,uint64_t(Word(row+16)) | uint64_t(Word(row+20))<<32);
            if(kind==3){forward=start;forward_bytes=Word(row+4);}
            if(kind==4){reverse=start;reverse_bytes=Word(row+4);}
        }
        assert(live.Begin(selection)==LiveResult::More);
        for(unsigned i=0;i<kMaxEntries+1024;++i){auto step=live.RecoverStep();if(step==LiveResult::Ready)return;assert(step==LiveResult::More);}
        assert(false);
    }
    LiveResult Edit(uint32_t id,uint32_t key,const std::string &text,uint8_t op=0) {
        return live.Append(id,op,&key,1,reinterpret_cast<const uint8_t *>(text.data()),text.size());
    }
    std::string Text(uint32_t id,uint32_t key,const LiveView *old=nullptr) {
        RecordView record;auto status=old?live.FindSnapshot(*old,id,&key,1,record):live.FindLive(id,&key,1,record);
        if(status==LiveResult::Missing || (status==LiveResult::Ok && record.operation!=Operation::Set))return "<absent>";
        assert(status==LiveResult::Ok);
        std::string text(record.text_bytes,'\0');
        if(old)assert(live.ReadText(*old,record,reinterpret_cast<uint8_t *>(text.data()),text.size())==LiveResult::Ok);
        else if(!text.empty())assert(device.Read(record.text_address,reinterpret_cast<uint8_t *>(text.data()),text.size()));
        return text;
    }
    bool Plan(SegmentMap &map,uint32_t &f,uint32_t &r) {
        if(CollectOwnership(device,catalog,live.Root(),map)!=CatalogResult::Ok)return false;
        if(segments<255)map.Add(segments,255-segments);
        AllocationPlan plan(map);return plan.Reserve(1,f) && plan.Reserve(1,r);
    }
    CatalogResult Checkpoint(bool read_during_recovery=true) {
        LiveView frozen;
        if(live.BeginMaintenance(frozen)!=LiveResult::Ok)return CatalogResult::Busy;
        uint32_t blocked=1;
        assert(live.Append(1,0,&blocked,1,nullptr,0)==LiveResult::Busy);
        SegmentMap map;uint32_t fs=0,rs=0;
        if(!Plan(map,fs,rs)){assert(live.EndMaintenance(frozen));return CatalogResult::Invalid;}
        assert(device.Erase(fs) && device.Erase(rs));
        IndexSink<LiveDevice> f(device),r(device);
        assert(f.Begin(fs*65536,65536) && r.Begin(rs*65536,65536));
        struct Ops {
            LiveFixture &fixture;LiveView &view;IndexSink<LiveDevice> &f,&r;
            uint32_t reverse;
            int CompareKey(uint32_t a,uint32_t b){auto x=fixture.Row(a),y=fixture.Row(b);return x.key==y.key?0:x.key<y.key?-1:1;}
            int CompareReverse(uint32_t a,uint32_t b){auto x=fixture.Row(a),y=fixture.Row(b);auto p=std::make_pair(x.text,x.key),q=std::make_pair(y.text,y.key);return p==q?0:p<q?-1:1;}
            int Operation(uint32_t address){return fixture.Row(address).operation;}
            bool Emit(bool reverse,uint32_t address){return reverse?r.Append(address):f.Append(address);}
            bool ReadIndex(bool rev,uint32_t pos,uint32_t &address) {
                uint8_t bytes[4];if(!fixture.device.Read((rev?reverse*65536:view.forward_address)+pos*4,bytes,4))return false;
                address=Word(bytes);return true;
            }
        } ops{*this,frozen,f,r,reverse};
        CheckpointBuilder<Ops> builder(ops);
        assert(builder.Begin(frozen.tail,frozen.forward_count,reverse_bytes/4)==BuildResult::More);
        for(;;){auto step=builder.Step();if(step==BuildResult::Done)break;assert(step==BuildResult::More);}
        assert(f.Finish() && r.Finish());
        auto data=Payload(frozen.highwater,fs,f.Bytes(),f.Checksum(),rs,r.Bytes(),r.Checksum());
        CatalogSelection next;
        const auto published=catalog.Publish(selection,data.size(),Source{data},CheckExtent(),next);
        if(published!=CatalogResult::Ok){assert(live.EndMaintenance(frozen));return published;}
        forward=fs;forward_bytes=f.Bytes();reverse=rs;reverse_bytes=r.Bytes();selection=next;
        const auto frozen_answer=Text(1,1,&frozen);
        assert(live.Begin(next)==LiveResult::More);
        for(;;) {
            if(read_during_recovery)assert(Text(1,1,&frozen)==frozen_answer);
            auto status=live.RecoverStep();if(status==LiveResult::Ready)break;assert(status==LiveResult::More);
        }
        assert(live.TailCount()==0 && live.EndMaintenance(frozen));return CatalogResult::Ok;
    }
    bool Rollover() {
        SegmentMap map;uint32_t start=0;
        if(CollectOwnership(device,catalog,live.Root(),map)!=CatalogResult::Ok)return false;
        if(segments<255)map.Add(segments,255-segments);
        AllocationPlan plan(map);if(!plan.Reserve(1,start))return false;
        const uint64_t epoch=journals.back().second+1;
        assert(journal.Initialize(start,epoch)==JournalResult::Ok);
        journals.emplace_back(start,epoch);
        uint32_t fc=forward_bytes?Crc(0,nor.data.data()+forward*65536,forward_bytes):0;
        uint32_t rc=reverse_bytes?Crc(0,nor.data.data()+reverse*65536,reverse_bytes):0;
        uint8_t prefix[32];assert(device.Read(selection.root.address,prefix,32));
        const uint64_t through=uint64_t(Word(prefix+8)) | uint64_t(Word(prefix+12))<<32;
        auto data=Payload(through,forward,forward_bytes,fc,reverse,reverse_bytes,rc);
        CatalogSelection next;
        assert(catalog.Publish(selection,data.size(),Source{data},CheckExtent(),next)==CatalogResult::Ok);
        selection=next;Recover();return true;
    }
};
