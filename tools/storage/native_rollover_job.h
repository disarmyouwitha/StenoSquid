// Reserve and initialize a new journal, publish ownership, then recover the live
// writer. Caller holds an exclusive maintenance lease across the whole job.
#pragma once
#include "native_live_journal.h"
#include "native_ownership.h"
#include "native_maintenance.h"

namespace candidate_b {
template <class Device>
class NativeRolloverJob {
    enum class Phase { Idle, Inspect, Plan, Initialize, Publish, Recover, Done, Failed };
    Device &device;
    NativeCatalog<Device> &catalog;
    LiveJournal<Device> &live;
    NativeJournal<Device> journal;
    LiveView frozen{};
    CatalogSelection current{},published{};
    SegmentMap unavailable{};
    uint8_t prefix[32]{},extent[24]{};
    uint32_t dictionaries=0,extents=0,position=0,start=0;
    uint64_t last_epoch=0;
    bool did_publish=false;
    Phase phase=Phase::Idle;
    bool Active()const{return phase!=Phase::Idle && phase!=Phase::Done && phase!=Phase::Failed;}
    JobResult Stop(JobResult result,bool uncertain=false) {
        if(uncertain)catalog.Invalidate();
        if(frozen.token && !live.EndMaintenance(frozen))result=JobResult::RecoveryRequired;
        phase=Phase::Failed;return result;
    }
    bool Source(uint32_t offset,uint8_t *out,uint32_t bytes) {
        const uint32_t original=frozen.root.bytes;
        if(offset>original+24 || bytes>original+24-offset)return false;
        while(bytes) {
            uint32_t n;
            if(offset<32) {
                n=32-offset;if(n>bytes)n=bytes;
                std::memcpy(out,prefix+offset,n);
            } else if(offset<original) {
                n=original-offset;if(n>bytes)n=bytes;
                if(!device.Read(frozen.root.address+offset,out,n))return false;
            } else {
                n=original+24-offset;if(n>bytes)n=bytes;
                std::memcpy(out,extent+(offset-original),n);
            }
            offset+=n;out+=n;bytes-=n;
        }
        return true;
    }
public:
    NativeRolloverJob(Device &value,NativeCatalog<Device> &roots,LiveJournal<Device> &state)
        :device(value),catalog(roots),live(state),journal(value){}
    NativeRolloverJob(const NativeRolloverJob &)=delete;
    NativeRolloverJob &operator=(const NativeRolloverJob &)=delete;
    bool Published()const{return did_publish;}
    const CatalogSelection &Selection()const{return did_publish?published:current;}
    const LiveView &Input()const{return frozen;}
    JobResult Begin(const CatalogSelection &selection,const SegmentMap &excluded=SegmentMap{}) {
        if(Active())return JobResult::Busy;
        const auto &root=live.Root();
        if(selection.quarantine || selection.root.generation!=root.generation || selection.root.address!=root.address ||
           selection.root.bytes!=root.bytes || selection.root.crc!=root.crc)return JobResult::Invalid;
        const auto status=live.BeginMaintenance(frozen);
        if(status!=LiveResult::Ok)return status==LiveResult::Busy?JobResult::Busy:JobResult::ReadOnly;
        current=selection;published={};unavailable=excluded;did_publish=false;position=start=0;last_epoch=0;
        if(!device.Read(root.address,prefix,32))return Stop(JobResult::IoError);
        dictionaries=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
        extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        if(dictionaries>64 || extents>253 || root.bytes!=32+dictionaries*96+extents*24)return Stop(JobResult::Corrupt);
        if(extents==253 || root.bytes+24>catalog.PayloadCapacity())return Stop(JobResult::NoSpace);
        phase=Phase::Inspect;return JobResult::More;
    }
    JobResult Cancel() {
        if(!Active())return JobResult::Invalid;
        if(did_publish)return JobResult::Busy;
        return Stop(JobResult::Cancelled);
    }
    template <class CheckExtent>
    JobResult Step(CheckExtent check) {
        if(phase==Phase::Done)return JobResult::Done;
        if(!Active())return JobResult::Invalid;
        if(!catalog.CanWrite(did_publish?published.root:frozen.root))return Stop(JobResult::RecoveryRequired,true);
        if(phase==Phase::Inspect) {
            if(position<extents) {
                uint8_t row[24];
                if(!device.Read(frozen.root.address+32+dictionaries*96+position++*24,row,24))return Stop(JobResult::IoError);
                if(Word(row+12)==2) {
                    const uint64_t epoch=uint64_t(Word(row+16)) | uint64_t(Word(row+20))<<32;
                    if(!epoch || epoch<=last_epoch)return Stop(JobResult::Corrupt);
                    last_epoch=epoch;
                }
                return JobResult::More;
            }
            if(last_epoch==UINT64_MAX)return Stop(JobResult::NoSpace);
            phase=Phase::Plan;return JobResult::More;
        }
        if(phase==Phase::Plan) {
            SegmentMap protected_segments;
            if(CollectOwnership(device,catalog,frozen.root,protected_segments)!=CatalogResult::Ok)return Stop(JobResult::RecoveryRequired,true);
            protected_segments.Union(unavailable);AllocationPlan plan(protected_segments, NativePoolEnd(device));
            if(!plan.Reserve(1,start))return Stop(JobResult::NoSpace);
            std::memset(extent,0,24);extent[0]=uint8_t(start);extent[1]=uint8_t(start>>8);extent[2]=1;
            PutWord(extent+4,kSegmentBytes);PutWord(extent+12,2);
            PutWord(extent+16,uint32_t(last_epoch+1));PutWord(extent+20,uint32_t((last_epoch+1)>>32));
            prefix[6]=uint8_t(extents+1);prefix[7]=uint8_t((extents+1)>>8);
            phase=Phase::Initialize;return JobResult::More;
        }
        if(phase==Phase::Initialize) {
            if(journal.Initialize(start,last_epoch+1)!=JournalResult::Ok)return Stop(JobResult::IoError);
            // Never append before catalog publication; a failed transaction only
            // leaves an unreferenced, empty journal which can later be reclaimed.
            phase=Phase::Publish;return JobResult::More;
        }
        if(phase==Phase::Publish) {
            const auto status=catalog.Publish(current,frozen.root.bytes+24,
                [&](uint32_t at,uint8_t *out,uint32_t n){return Source(at,out,n);},check,published);
            if(status==CatalogResult::Busy)return Stop(JobResult::Busy);
            if(status!=CatalogResult::Ok)return Stop(JobResult::RecoveryRequired,true);
            did_publish=true;
            if(live.Begin(published)!=LiveResult::More)return Stop(JobResult::RecoveryRequired,true);
            phase=Phase::Recover;return JobResult::More;
        }
        const auto status=live.RecoverStep();
        if(status==LiveResult::More)return JobResult::More;
        if(status!=LiveResult::Ready)return Stop(JobResult::RecoveryRequired,true);
        if(!live.EndMaintenance(frozen))return Stop(JobResult::RecoveryRequired,true);
        phase=Phase::Done;return JobResult::Done;
    }
};
} // namespace candidate_b
