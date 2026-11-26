// At boot, validate checksums, ownership and index structure in sequential
// passes. Publication and explicit Verify also cross-check index membership.
#pragma once
#include "native_merge_ops.h"
namespace candidate_b {
enum class ValidationStage { Extents, Journal, Forward, Reverse };
template<class Device>
class NativeValidator {
    Device &device;
    NativeCatalog<Device> &catalog;
    NativeJournal<Device> journal;
    LiveView view{};
    IndexSink<Device> unused_f,unused_r;
    NativeMergeOps<Device> ops;
    uint32_t reverse_address=0,reverse_count=0;
    bool MatchIndex(bool reverse,const MergeRecord &target) {
        auto read=[&](uint32_t offset,uint8_t *out,size_t n){return device.Read((reverse?reverse_address:view.forward_address)+offset,out,n);};
        auto compare=[&](uint32_t at){return ops.CompareTo(at,target,reverse);};
        uint32_t found=0;
        return FindOrdered(reverse?reverse_count:view.forward_count,read,compare,found)==Result::Found && found==target.record.address;
    }
public:
    NativeValidator(Device &value,NativeCatalog<Device> &roots):device(value),catalog(roots),journal(value),
        unused_f(value),unused_r(value),ops(value,view,unused_f,unused_r){}
    const LiveView &ValidatedView() const { return view; }
    bool Extent(uint32_t,const uint8_t *extent) {
        const uint32_t start=uint16_t(extent[0]) | uint16_t(extent[1])<<8,bytes=Word(extent+4),kind=Word(extent+12);
        if(kind==2)return journal.Open(start,uint64_t(Word(extent+16)) | uint64_t(Word(extent+20))<<32)==JournalResult::Ok;
        if(kind==1)return device.CheckBase(start*kSegmentBytes,bytes,Word(extent+8));
        uint32_t crc=0;
        return device.Checksum(start*kSegmentBytes,bytes,crc) && crc==Word(extent+8);
    }
    CatalogResult Validate(const CatalogRoot &root, bool cross_check = true,
                           void (*stage)(ValidationStage) = nullptr,
                           void (*extent_failed)(uint32_t, uint32_t) = nullptr) {
        if (stage) stage(ValidationStage::Extents);
        const auto schema = catalog.Validate(root, [&](uint32_t i, const uint8_t *row) {
            if (Extent(i, row)) return true;
            if (extent_failed) {
                const uint32_t segment = uint16_t(row[0]) | uint16_t(row[1]) << 8;
                extent_failed(Word(row + 12), segment * kSegmentBytes);
            }
            return false;
        });
        if(schema!=CatalogResult::Ok)return schema;
        view={};reverse_address=reverse_count=0;uint8_t prefix[32];
        if(!device.Read(root.address,prefix,32))return CatalogResult::IoError;
        view.root=root;view.dictionary_count=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
        const uint32_t extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        view.through=uint64_t(Word(prefix+8)) | uint64_t(Word(prefix+12))<<32;
        for(uint32_t i=0;i<view.dictionary_count;++i) {
            uint8_t id[4];if(!device.Read(root.address+32+i*96,id,4))return CatalogResult::IoError;
            view.identities[i]=Word(id);
        }
        if (stage) stage(ValidationStage::Journal);
        uint64_t last_epoch=0;bool boundary=!view.through;
        for(uint32_t i=0;i<extents;++i) {
            uint8_t row[24];if(!device.Read(root.address+32+view.dictionary_count*96+i*24,row,24))return CatalogResult::IoError;
            const uint32_t start=uint16_t(row[0]) | uint16_t(row[1])<<8,kind=Word(row+12);
            if(kind==3) {
                view.forward_address=start*kSegmentBytes;view.forward_count=Word(row+4)/4;
            }
            if(kind==4){reverse_address=start*kSegmentBytes;reverse_count=Word(row+4)/4;}
            if(kind!=2)continue;
            const uint64_t epoch=uint64_t(Word(row+16)) | uint64_t(Word(row+20))<<32;
            if(epoch<=last_epoch)return CatalogResult::Corrupt;
            last_epoch=epoch;view.journals.Add(start,1);JournalScan scan;JournalRecord record;
            if(journal.BeginScan(start,epoch,view.highwater,scan)!=JournalResult::Ok)return CatalogResult::Corrupt;
            JournalResult status;
            while((status=journal.Step(scan,record))==JournalResult::Ok) {
                view.highwater=record.sequence;if(record.sequence==view.through)boundary=true;
                // A retained older root can legitimately have >256 later edits.
                // Its immutable checkpoint is still valid. The selected live
                // root enforces its own replay bound in LiveJournal::Begin/Step.
            }
            if(status!=JournalResult::End && status!=JournalResult::Sealed)return status==JournalResult::IoError?CatalogResult::IoError:CatalogResult::Corrupt;
        }
        if(!boundary || view.through>view.highwater || view.forward_count>kMaxEntries || reverse_count>kMaxEntries)return CatalogResult::Corrupt;
        uint32_t translations = 0;
        for(unsigned reverse=0;reverse<2;++reverse) {
            if (stage) stage(reverse ? ValidationStage::Reverse : ValidationStage::Forward);
            MergeRecord previous;
            const uint32_t index_address = reverse ? reverse_address : view.forward_address;
            const uint32_t entries = reverse ? reverse_count : view.forward_count;
            uint8_t addresses[64];
            constexpr uint32_t batch = sizeof(addresses) / 4;
            for(uint32_t i=0;i<entries;++i) {
                const uint32_t slot = i % batch;
                if (!slot) {
                    const uint32_t remaining = entries - i;
                    if (!device.Read(index_address + i*4, addresses, 4 * (remaining < batch ? remaining : batch)))
                        return CatalogResult::Corrupt;
                }
                const uint32_t address = Word(addresses + slot*4);
                MergeRecord record;
                if(!ops.Record(address,record,cross_check) || record.identity>=Word(prefix) ||
                   record.record.sequence>view.through || record.operation==2 || (reverse && record.operation) ||
                   (i && (reverse?ops.ReverseKeys(previous,record):ops.Keys(previous,record))>=0) ||
                   (cross_check && (reverse?!MatchIndex(false,record):(!record.operation && !MatchIndex(true,record)))))return CatalogResult::Corrupt;
                if (!reverse && !record.operation) ++translations;
                previous=record;
            }
        }
        return translations == reverse_count ? CatalogResult::Ok : CatalogResult::Corrupt;
    }
};
}
