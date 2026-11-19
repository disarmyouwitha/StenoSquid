// Candidate-B live overlay state. Caller serializes every method with catalog
// publication and the engine. Does not allocate, erase, format or clean journals.
#pragma once
#include "native_catalog.h"
#include "packed_index.h"
#include "binary_record.h"

namespace candidate_b {
enum class LiveResult { More, Ready, Ok, Missing, Invalid, IoError, Corrupt,
                        NeedCheckpoint, NeedJournal, NeedRebuild, ReadOnly, Stale, Busy };
struct LiveView {
    CatalogRoot root{};
    // RAM-only key order; equal outlines retain sequence order (newest last).
    // Committed flash journals remain chronological and keep the same format.
    Tail tail{};
    SegmentMap journals{};
    uint32_t identities[64]{};
    uint16_t outline_lengths[64]{};
    uint32_t dictionary_count=0,forward_address=0,forward_count=0,token=0,highwater_address=0;
    uint64_t through=0,highwater=0;
};

template <class Device>
class LiveJournal {
    Device &device;
    NativeCatalog<Device> &catalog;
    NativeJournal<Device> journal;
    LiveView live;
    CatalogShape shape{};
    uint32_t dictionary_position=0,extent_position=0;
    uint32_t append_address=0,append_end=0;
    uint32_t maintenance_token=0;
    uint64_t last_epoch=0;
    JournalScan scan{};
    bool recovering=false,scanning=false,ready=false,sealed=false,quarantine=false,boundary_seen=false,maintenance=false;

    LiveResult Fail(LiveResult result){recovering=ready=false;return result;}
    bool HasIdentity(const LiveView &view,uint32_t id)const {
        for(uint32_t i=0;i<view.dictionary_count;++i)if(view.identities[i]==id)return true;
        return false;
    }
    static bool HasLength(const LiveView &view,uint32_t id,uint8_t count) {
        for(uint32_t i=0;i<view.dictionary_count;++i)
            if(view.identities[i]==id)return view.outline_lengths[i]&(uint16_t(1)<<(count-1));
        return false;
    }
    void Observe(uint32_t id,uint8_t count) {
        for(uint32_t i=0;i<live.dictionary_count;++i)if(live.identities[i]==id) {
            live.outline_lengths[i]|=uint16_t(1)<<(count-1);return;
        }
    }
    bool Owns(const LiveView &view,uint32_t at)const {
        return at/kSegmentBytes<255 && at%kSegmentBytes>=256 && !(at%4) && view.journals.Contains(at/kSegmentBytes);
    }
    LiveResult AppendTail(uint32_t address,uint32_t id,const uint32_t *strokes,uint8_t count) {
        if(live.tail.count==Tail::kCapacity)return LiveResult::NeedRebuild;
        uint32_t low=0,high=live.tail.count;
        while(low<high) {
            const uint32_t mid=low+(high-low)/2;JournalRecord record;
            if(!Owns(live,live.tail.addresses[mid]))return LiveResult::Corrupt;
            const auto status=journal.ReadKey(live.tail.addresses[mid],record);
            if(status==JournalResult::IoError)return LiveResult::IoError;
            if(status!=JournalResult::Ok || record.sequence>live.highwater || record.sequence<=live.through)return LiveResult::Corrupt;
            RecordView ignored;bool io=false;
            const int order=CompareOutline(record.address+32,record.payload_bytes,id,strokes,count,
                [&](uint32_t at,uint8_t *out,size_t n){if(!device.Read(at,out,n)){io=true;return false;}return true;},ignored);
            if(io)return LiveResult::IoError;
            if(order<-1 || order>1)return LiveResult::Corrupt;
            if(order<=0)low=mid+1;else high=mid;
        }
        for(uint32_t i=live.tail.count;i>low;--i)live.tail.addresses[i]=live.tail.addresses[i-1];
        live.tail.addresses[low]=address;++live.tail.count;return LiveResult::Ok;
    }
    LiveResult FindIn(const LiveView &view,uint32_t id,const uint32_t *strokes,uint8_t count,
                      RecordView &result) {
        if(!id || !strokes || !count || count>16 || view.tail.count>256 || view.dictionary_count>64 ||
           view.forward_count>kMaxEntries || !HasIdentity(view,id))return LiveResult::Invalid;
        for(uint8_t i=0;i<count;++i)if(!strokes[i] || strokes[i]>>25)return LiveResult::Invalid;
        if(!HasLength(view,id,count))return LiveResult::Missing;
        bool io=false,corrupt=false,indexed=false;
        auto compare=[&](uint32_t address) {
            if(!Owns(view,address)){corrupt=true;return 2;}
            JournalRecord record;
            const auto status=journal.ReadKey(address,record);
            if(status==JournalResult::IoError){io=true;return 2;}
            if(status!=JournalResult::Ok || record.sequence>view.highwater ||
               (indexed?record.sequence>view.through:record.sequence<=view.through)){corrupt=true;return 2;}
            const auto comparison=CompareOutline(address+32,record.payload_bytes,id,strokes,count,
                [&](uint32_t at,uint8_t *out,size_t n){if(!device.Read(at,out,n)){io=true;return false;}return true;},result);
            if(comparison<-1 || comparison>1)corrupt=true;
            return comparison;
        };
        auto read=[&](uint32_t offset,uint8_t *out,size_t n) {
            indexed=true;
            if(offset>view.forward_count*4 || n>view.forward_count*4-offset){corrupt=true;return false;}
            if(!device.Read(view.forward_address+offset,out,n)){io=true;return false;}return true;
        };
        uint32_t address=0;
        uint32_t low=0,high=view.tail.count;
        while(low<high) {
            const uint32_t mid=low+(high-low)/2;
            const int order=compare(view.tail.addresses[mid]);
            if(order<-1 || order>1)break;
            if(order<=0)low=mid+1;else high=mid;
        }
        Result found=Result::Missing;
        if(!io && !corrupt && low && compare(view.tail.addresses[low-1])==0) {
            address=view.tail.addresses[low-1];found=Result::Found;
        }
        if(!io && !corrupt && found==Result::Missing)found=FindOrdered(view.forward_count,read,compare,address);
        if(found==Result::Found && !io && !corrupt) {
            // Re-read the selected key so result describes the newest matching
            // row, then CRC-check its full payload once, not at every probe.
            if(compare(address)!=0)corrupt=true;
            else {
                JournalRecord record;const auto checked=journal.Read(address,record);
                if(checked==JournalResult::IoError)io=true;
                else if(checked!=JournalResult::Ok)corrupt=true;
            }
        }
        if(io)return LiveResult::IoError;
        if(corrupt || found==Result::Corrupt || found==Result::IoError)return LiveResult::Corrupt;
        return found==Result::Found?LiveResult::Ok:LiveResult::Missing;
    }
public:
    LiveJournal(Device &value,NativeCatalog<Device> &roots):device(value),catalog(roots),journal(value){}
    bool Ready()const{return ready;}
    uint64_t Highwater()const{return live.highwater;}
    uint32_t TailCount()const{return live.tail.count;}
    const CatalogRoot &Root()const{return live.root;}
    uint8_t MaximumOutline(uint32_t id)const {
        for(uint32_t i=0;i<live.dictionary_count;++i)if(live.identities[i]==id) {
            const auto bits=live.outline_lengths[i];
            return bits?uint8_t(32-__builtin_clz(unsigned(bits))):0;
        }
        return 0;
    }

    // Attach only a fully validated selection from this catalog instance.
    // Opening/recovery is read-only and advances one metadata row or record/call.
    LiveResult Begin(const CatalogSelection &selection) {
        ready=recovering=scanning=false;live={};scan={};
        dictionary_position=extent_position=append_address=append_end=0;last_epoch=0;sealed=false;
        if(!catalog.IsTrusted(selection.root))return LiveResult::Invalid;
        uint8_t prefix[32];
        if(!device.Read(selection.root.address,prefix,32))return LiveResult::IoError;
        shape.next_id=Word(prefix);shape.dictionaries=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
        shape.extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        shape.through=uint64_t(Word(prefix+8)) | uint64_t(Word(prefix+12))<<32;
        if(!shape.next_id || shape.dictionaries>64 || shape.extents>253 ||
           selection.root.bytes!=32u+shape.dictionaries*96u+shape.extents*24u)return LiveResult::Corrupt;
        live.root=selection.root;live.through=shape.through;quarantine=selection.quarantine;
        boundary_seen=shape.through==0;
        recovering=true;return LiveResult::More;
    }

    LiveResult RecoverStep() {
        if(ready)return LiveResult::Ready;
        if(!recovering)return LiveResult::Invalid;
        if(!catalog.IsLatestTrusted(live.root))return Fail(LiveResult::Stale);
        if(dictionary_position<shape.dictionaries) {
            uint8_t row[4];
            if(!device.Read(live.root.address+32+dictionary_position*96,row,4))return Fail(LiveResult::IoError);
            uint32_t id=Word(row);
            if(!id || id>=shape.next_id || HasIdentity(live,id))return Fail(LiveResult::Corrupt);
            live.identities[live.dictionary_count++]=id;++dictionary_position;return LiveResult::More;
        }
        if(scanning) {
            JournalRecord record;const auto status=journal.Step(scan,record);
            if(status==JournalResult::IoError)return Fail(LiveResult::IoError);
            if(status==JournalResult::Ok) {
                uint8_t key[8];
                if(!device.Read(record.address+32,key,8))return Fail(LiveResult::IoError);
                if(!Word(key) || key[5]<1 || key[5]>16)return Fail(LiveResult::Corrupt);
                Observe(Word(key),key[5]);
                live.highwater=record.sequence;live.highwater_address=record.address;
                if(record.sequence==live.through)boundary_seen=true;
                if(record.sequence>live.through) {
                    uint8_t bytes[64];uint32_t strokes[16];
                    if(!device.Read(record.address+40,bytes,key[5]*4))return Fail(LiveResult::IoError);
                    for(unsigned i=0;i<key[5];++i)strokes[i]=Word(bytes+i*4);
                    const auto added=AppendTail(record.address,Word(key),strokes,key[5]);
                    if(added!=LiveResult::Ok)return Fail(added);
                }
                return LiveResult::More;
            }
            if(status!=JournalResult::End && status!=JournalResult::Sealed)return Fail(LiveResult::Corrupt);
            sealed=status==JournalResult::Sealed;append_address=scan.next;append_end=scan.end;
            scanning=false;return LiveResult::More;
        }
        if(extent_position<shape.extents) {
            uint8_t extent[24];
            uint32_t at=live.root.address+32+shape.dictionaries*96+extent_position++*24;
            if(!device.Read(at,extent,24))return Fail(LiveResult::IoError);
            uint32_t start=uint16_t(extent[0]) | uint16_t(extent[1])<<8,kind=Word(extent+12);
            if(kind==uint32_t(ExtentKind::Forward)) {
                uint32_t bytes=Word(extent+4);
                if(live.forward_count || bytes%4 || bytes/4>kMaxEntries)return Fail(LiveResult::Corrupt);
                live.forward_address=start*kSegmentBytes;live.forward_count=bytes/4;
            }
            if(kind!=uint32_t(ExtentKind::Journal))return LiveResult::More;
            const uint64_t epoch=uint64_t(Word(extent+16)) | uint64_t(Word(extent+20))<<32;
            if(!epoch || epoch<=last_epoch || start<2 || start>=255 || live.journals.Contains(start))return Fail(LiveResult::Corrupt);
            const auto status=journal.BeginScan(start,epoch,live.highwater,scan);
            if(status!=JournalResult::Ok)return Fail(status==JournalResult::IoError?LiveResult::IoError:LiveResult::Corrupt);
            live.journals.Add(start,1);last_epoch=epoch;scanning=true;return LiveResult::More;
        }
        if(!boundary_seen || live.through>live.highwater || (live.forward_count && !live.through))return Fail(LiveResult::Corrupt);
        recovering=false;ready=true;return LiveResult::Ready;
    }

    // Caller has published a metadata-only edit under the same exclusive lease.
    // Compare immutable ownership and identity order before retaining live cursors.
    bool Retarget(const CatalogSelection &next) {
        if(!ready || maintenance || next.quarantine || !catalog.CanWrite(next.root))return false;
        uint8_t old_prefix[32],new_prefix[32];
        if(!device.Read(live.root.address,old_prefix,32) || !device.Read(next.root.address,new_prefix,32) ||
           std::memcmp(old_prefix,new_prefix,32) || next.root.bytes!=live.root.bytes)return false;
        for(uint32_t i=0;i<live.dictionary_count;++i) {
            uint8_t id[4];
            if(!device.Read(next.root.address+32+i*96,id,4) || Word(id)!=live.identities[i])return false;
        }
        for(uint32_t i=0;i<shape.extents;++i) {
            uint8_t a[24],b[24];const uint32_t offset=32+shape.dictionaries*96+i*24;
            if(!device.Read(live.root.address+offset,a,24) || !device.Read(next.root.address+offset,b,24) ||
               std::memcmp(a,b,24))return false;
        }
        live.root=next.root;return true;
    }

    LiveResult Append(uint32_t id,uint8_t operation,const uint32_t *strokes,uint8_t count,
                       const uint8_t *text,uint16_t bytes) {
        if(!ready)return LiveResult::Invalid;
        if(maintenance)return LiveResult::Busy;
        if(quarantine || !catalog.CanWrite(live.root))return catalog.IsLatestTrusted(live.root)?LiveResult::ReadOnly:LiveResult::Stale;
        if(!HasIdentity(live,id) || !ValidEdit(id,operation,strokes,count,text,bytes))return LiveResult::Invalid;
        if(live.tail.count==256)return LiveResult::NeedCheckpoint;
        const uint32_t span=(32u+8u+4u*count+bytes+3)&~3u;
        if(sealed || !append_address || append_address>append_end ||
           span>append_end-append_address)return LiveResult::NeedJournal;
        if(live.highwater==UINT64_MAX)return LiveResult::Corrupt;
        const auto status=journal.Append(append_address,live.highwater+1,id,operation,strokes,count,text,bytes);
        if(status==JournalResult::Invalid)return LiveResult::Invalid;
        if(status==JournalResult::Full)return LiveResult::NeedJournal;
        // A write/readback failure has an ambiguous commit outcome. Stop writes
        // and queries until recovery reconstructs it; never retry at this cursor.
        if(status!=JournalResult::Ok)return Fail(status==JournalResult::IoError?LiveResult::IoError:LiveResult::Corrupt);
        const auto added=AppendTail(append_address,id,strokes,count);
        if(added!=LiveResult::Ok)return Fail(added==LiveResult::NeedRebuild?LiveResult::Corrupt:added);
        Observe(id,count);
        ++live.highwater;live.highwater_address=append_address;
        append_address+=span;
        return LiveResult::Ok;
    }

    LiveResult Snapshot(LiveView &view) {
        if(view.token || !ready)return LiveResult::Invalid;
        uint32_t token=0;
        if(!catalog.AcquirePin(live.root,token))return LiveResult::Busy;
        view=live;view.token=token;return LiveResult::Ok;
    }
    LiveResult BeginMaintenance(LiveView &view) {
        if(maintenance)return LiveResult::Busy;
        if(!ready || !catalog.CanWrite(live.root))return LiveResult::ReadOnly;
        const auto result=Snapshot(view);
        if(result==LiveResult::Ok){maintenance=true;maintenance_token=view.token;}
        return result;
    }
    bool EndMaintenance(LiveView &view) {
        if(!maintenance || !view.token || view.token!=maintenance_token)return false;
        if(!catalog.ReleasePin(view.token))return false;
        view={};maintenance=false;maintenance_token=0;return true;
    }
    bool Release(LiveView &view) {
        if(!view.token || view.token==maintenance_token)return false;
        const bool released=catalog.ReleasePin(view.token);view={};return released;
    }
    LiveResult FindLive(uint32_t id,const uint32_t *strokes,uint8_t count,RecordView &result) {
        if(!ready)return LiveResult::Invalid;
        if(!catalog.IsLatestTrusted(live.root))return LiveResult::Stale;
        const auto status=FindIn(live,id,strokes,count,result);
        return status==LiveResult::IoError || status==LiveResult::Corrupt?Fail(status):status;
    }
    LiveResult FindSnapshot(const LiveView &view,uint32_t id,const uint32_t *strokes,uint8_t count,RecordView &result) {
        if(!catalog.HasPin(view.token,view.root))return LiveResult::Invalid;
        return FindIn(view,id,strokes,count,result);
    }
    LiveResult ReadText(const LiveView &view,const RecordView &text,uint8_t *out,uint16_t bytes) {
        if(!catalog.HasPin(view.token,view.root) || bytes!=text.text_bytes || text.operation!=Operation::Set ||
           bytes>2047 || (bytes && !out))return LiveResult::Invalid;
        if(!bytes)return LiveResult::Ok;
        if(!Owns(view,text.text_address) || bytes>kSegmentBytes-text.text_address%kSegmentBytes)return LiveResult::Corrupt;
        return device.Read(text.text_address,out,bytes)?LiveResult::Ok:LiveResult::IoError;
    }
};
} // namespace candidate_b
