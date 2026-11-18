// Candidate-B checkpoint transaction coordinator. No heap, threads or automatic
// recovery erases. Caller serializes all methods and may yield between Step calls.
#pragma once
#include "native_ownership.h"
#include "native_merge_ops.h"
#include "native_checkpoint.h"
#include "native_maintenance.h"

namespace candidate_b {
template <class Device>
class NativeCheckpointJob {
    enum class Phase { Idle, Inspect, Plan, EraseForward, EraseReverse, Build,
                       FinishForward, FinishReverse, Forward, Reverse, Coverage,
                       Publish, Recover, Done, Failed };
    Device &device;
    NativeCatalog<Device> &catalog;
    LiveJournal<Device> &live;
    LiveView frozen{};
    IndexSink<Device> forward,reverse;
    NativeMergeOps<Device> ops;
    CheckpointBuilder<NativeMergeOps<Device>> builder;
    CatalogSelection current{},published{};
    SegmentMap unavailable{};
    uint8_t prefix[32]{},retained[253]{},remap[253]{};
    uint16_t dictionaries=0,old_extents=0,kept=0;
    uint32_t inspected=0,old_reverse_address=0,old_reverse_count=0;
    uint32_t starts[2]{},segments[2]{},erase_position=0,position=0,previous=0;
    bool did_publish=false;
    Phase phase=Phase::Idle;

    bool Active()const{return phase!=Phase::Idle && phase!=Phase::Done && phase!=Phase::Failed;}
    JobResult Stop(JobResult result,bool uncertain=false) {
        builder.Cancel();forward.Cancel();reverse.Cancel();
        if(uncertain)catalog.Invalidate();
        if(frozen.token && !live.EndMaintenance(frozen))result=JobResult::RecoveryRequired;
        phase=Phase::Failed;return result;
    }
    static bool Same(const CatalogRoot &a,const CatalogRoot &b) {
        return a.generation==b.generation && a.address==b.address && a.bytes==b.bytes && a.crc==b.crc;
    }
    bool IndexAddress(bool rev,uint32_t i,uint32_t &out) {
        if(i>=(rev?reverse.Count():forward.Count()))return false;
        uint8_t bytes[4];if(!device.Read(starts[rev]*kSegmentBytes+i*4,bytes,4))return false;
        out=Word(bytes);return true;
    }
    Result Search(bool rev,uint32_t address,uint32_t &found) {
        auto read=[&](uint32_t offset,uint8_t *out,size_t n) {
            const uint32_t bytes=rev?reverse.Bytes():forward.Bytes();
            return offset<=bytes && n<=bytes-offset && device.Read(starts[rev]*kSegmentBytes+offset,out,n);
        };
        auto compare=[&](uint32_t candidate){return rev?ops.CompareReverse(candidate,address):ops.CompareKey(candidate,address);};
        return FindOrdered(rev?reverse.Count():forward.Count(),read,compare,found);
    }
    bool Latest(const MergeRecord &record,uint32_t &address,bool &needed) {
        RecordView value;
        const auto status=live.FindSnapshot(frozen,record.identity,record.strokes,record.count,value);
        if(status==LiveResult::Missing){needed=false;return true;}
        if(status!=LiveResult::Ok)return false;
        needed=value.operation!=Operation::Reset;
        if(!needed)return true;
        // Address of the record which supplied the exact matching outline.
        address=value.text_address-40u-record.count*4u;
        return true;
    }
    uint32_t SourceBytes()const {
        return 32u+dictionaries*96u+(kept+unsigned(forward.Bytes()!=0)+unsigned(reverse.Bytes()!=0))*24u;
    }
    bool Source(uint32_t offset,uint8_t *out,uint32_t bytes) {
        const uint32_t total=SourceBytes();
        if(offset>total || bytes>total-offset)return false;
        uint8_t row[96];
        while(bytes) {
            uint32_t start,size;
            if(offset<32) {
                start=0;size=32;std::memcpy(row,prefix,32);
                const uint16_t extents=kept+unsigned(forward.Bytes()!=0)+unsigned(reverse.Bytes()!=0);
                row[6]=uint8_t(extents);row[7]=uint8_t(extents>>8);
                PutWord(row+8,uint32_t(frozen.highwater));PutWord(row+12,uint32_t(frozen.highwater>>32));
            } else if(offset<32u+dictionaries*96u) {
                const uint32_t index=(offset-32)/96;start=32+index*96;size=96;
                if(!device.Read(frozen.root.address+start,row,96))return false;
                const uint16_t base=uint16_t(row[8]) | uint16_t(row[9])<<8;
                if(base!=kNoExtent) {
                    if(base>=old_extents || remap[base]==255)return false;
                    row[8]=remap[base];row[9]=0;
                }
            } else {
                const uint32_t extents_start=32u+dictionaries*96u,index=(offset-extents_start)/24;
                start=extents_start+index*24;size=24;
                if(index<kept) {
                    if(!device.Read(frozen.root.address+extents_start+retained[index]*24,row,24))return false;
                } else {
                    const bool rev=forward.Bytes()?index!=kept:true;
                    std::memset(row,0,24);row[0]=uint8_t(starts[rev]);row[1]=uint8_t(starts[rev]>>8);
                    row[2]=uint8_t(segments[rev]);row[3]=uint8_t(segments[rev]>>8);
                    PutWord(row+4,rev?reverse.Bytes():forward.Bytes());
                    PutWord(row+8,rev?reverse.Checksum():forward.Checksum());PutWord(row+12,rev?4:3);
                }
            }
            uint32_t n=size-(offset-start);if(n>bytes)n=bytes;
            std::memcpy(out,row+(offset-start),n);offset+=n;out+=n;bytes-=n;
        }
        return true;
    }
public:
    NativeCheckpointJob(Device &value,NativeCatalog<Device> &roots,LiveJournal<Device> &state)
        :device(value),catalog(roots),live(state),forward(value),reverse(value),
         ops(value,frozen,forward,reverse),builder(ops){}
    NativeCheckpointJob(const NativeCheckpointJob &)=delete;
    NativeCheckpointJob &operator=(const NativeCheckpointJob &)=delete;
    bool Published()const{return did_publish;}
    const CatalogSelection &Selection()const{return did_publish?published:current;}
    const LiveView &Input()const{return frozen;}
    JobResult Begin(const CatalogSelection &selection,const SegmentMap &excluded=SegmentMap{}) {
        if(Active())return JobResult::Busy;
        if(!Same(selection.root,live.Root()) || selection.quarantine)return JobResult::Invalid;
        const auto status=live.BeginMaintenance(frozen);
        if(status!=LiveResult::Ok)return status==LiveResult::Busy?JobResult::Busy:JobResult::ReadOnly;
        current=selection;published={};unavailable=excluded;did_publish=false;
        inspected=kept=position=previous=erase_position=old_reverse_address=old_reverse_count=0;
        std::memset(remap,255,sizeof(remap));
        if(!device.Read(frozen.root.address,prefix,32))return Stop(JobResult::IoError);
        dictionaries=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
        old_extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        if(dictionaries>64 || old_extents>253 || frozen.root.bytes!=32u+dictionaries*96u+old_extents*24u)
            return Stop(JobResult::Corrupt);
        phase=Phase::Inspect;return JobResult::More;
    }
    JobResult Cancel() {
        if(!Active())return JobResult::Invalid;
        // Once the root is committed, finish recovery; cancellation cannot roll
        // back a durable publication or resume an obsolete writer cursor.
        if(did_publish)return JobResult::Busy;
        return Stop(JobResult::Cancelled);
    }
    template <class CheckExtent>
    JobResult Step(CheckExtent check) {
        if(phase==Phase::Done)return JobResult::Done;
        if(!Active())return JobResult::Invalid;
        if(!catalog.CanWrite(did_publish?published.root:frozen.root))return Stop(JobResult::RecoveryRequired,true);
        if(phase==Phase::Inspect) {
            if(inspected<old_extents) {
                uint8_t row[24];
                if(!device.Read(frozen.root.address+32+dictionaries*96+inspected*24,row,24))return Stop(JobResult::IoError);
                const uint32_t kind=Word(row+12);
                if(kind==4) {
                    old_reverse_address=(uint16_t(row[0]) | uint16_t(row[1])<<8)*kSegmentBytes;
                    old_reverse_count=Word(row+4)/4;
                    if(Word(row+4)%4 || old_reverse_count>kMaxEntries)return Stop(JobResult::Corrupt);
                }
                if(kind==1 || kind==2){remap[inspected]=uint8_t(kept);retained[kept++]=uint8_t(inspected);}
                else if(kind!=3 && kind!=4)return Stop(JobResult::Corrupt);
                ++inspected;return JobResult::More;
            }
            if(kept>251 || frozen.forward_count>kMaxEntries-frozen.tail.count ||
               old_reverse_count>kMaxEntries-frozen.tail.count)return Stop(JobResult::NoSpace);
            ops.ReverseInput(old_reverse_address,old_reverse_count);phase=Phase::Plan;return JobResult::More;
        }
        if(phase==Phase::Plan) {
            SegmentMap protected_segments;
            if(CollectOwnership(device,catalog,frozen.root,protected_segments)!=CatalogResult::Ok)return Stop(JobResult::RecoveryRequired,true);
            protected_segments.Union(unavailable);AllocationPlan plan(protected_segments);
            for(unsigned i=0;i<2;++i) {
                const uint32_t count=(i?old_reverse_count:frozen.forward_count)+frozen.tail.count;
                segments[i]=(count*4+kSegmentBytes-1)/kSegmentBytes;if(!segments[i])segments[i]=1;
                if(!plan.Reserve(segments[i],starts[i]))return Stop(JobResult::NoSpace);
            }
            phase=Phase::EraseForward;return JobResult::More;
        }
        if(phase==Phase::EraseForward || phase==Phase::EraseReverse) {
            const bool rev=phase==Phase::EraseReverse;
            if(erase_position<segments[rev]) {
                if(!device.Erase(starts[rev]+erase_position++))return Stop(JobResult::IoError);
                return JobResult::More;
            }
            if(!(rev?reverse:forward).Begin(starts[rev]*kSegmentBytes,segments[rev]*kSegmentBytes))return Stop(JobResult::Invalid);
            erase_position=0;
            if(!rev){phase=Phase::EraseReverse;return JobResult::More;}
            if(builder.Begin(frozen.tail,frozen.forward_count,old_reverse_count)!=BuildResult::More)return Stop(JobResult::Invalid);
            phase=Phase::Build;return JobResult::More;
        }
        if(phase==Phase::Build) {
            const auto status=builder.Step();
            if(status==BuildResult::More)return JobResult::More;
            if(status!=BuildResult::Done)return Stop(status==BuildResult::IoError?JobResult::IoError:JobResult::Corrupt);
            phase=Phase::FinishForward;return JobResult::More;
        }
        if(phase==Phase::FinishForward || phase==Phase::FinishReverse) {
            const bool rev=phase==Phase::FinishReverse;
            if(!(rev?reverse:forward).Finish())return Stop(JobResult::IoError);
            phase=rev?Phase::Forward:Phase::FinishReverse;return JobResult::More;
        }
        if(phase==Phase::Forward || phase==Phase::Reverse) {
            const bool rev=phase==Phase::Reverse;
            if(position==(rev?reverse.Count():forward.Count())) {
                position=previous=0;phase=rev?Phase::Coverage:Phase::Reverse;return JobResult::More;
            }
            uint32_t address,found=0;MergeRecord record;
            if(!IndexAddress(rev,position,address) || !ops.Record(address,record,true) || !ops.Active(record.identity) ||
               record.operation==2 || (rev && record.operation) ||
               (previous && (rev?ops.CompareReverse(previous,address):ops.CompareKey(previous,address))>=0))return Stop(JobResult::Corrupt);
            if(rev) {
                if(Search(false,address,found)!=Result::Found || found!=address)return Stop(JobResult::Corrupt);
            } else {
                uint32_t latest=0;bool needed=false;
                if(!Latest(record,latest,needed) || !needed || latest!=address)return Stop(JobResult::Corrupt);
                if(!record.operation && (Search(true,address,found)!=Result::Found || found!=address))return Stop(JobResult::Corrupt);
            }
            previous=address;++position;return JobResult::More;
        }
        if(phase==Phase::Coverage) {
            if(position==frozen.forward_count+frozen.tail.count){phase=Phase::Publish;return JobResult::More;}
            uint32_t address;
            if(position<frozen.forward_count) {
                if(!ops.ReadIndex(false,position,address))return Stop(JobResult::Corrupt);
            } else address=frozen.tail.addresses[position-frozen.forward_count];
            ++position;MergeRecord record;
            if(!ops.Record(address,record,true))return Stop(JobResult::Corrupt);
            uint32_t latest=0,found=0;bool needed=false;
            if(ops.Active(record.identity) && !Latest(record,latest,needed))return Stop(JobResult::Corrupt);
            const auto status=Search(false,address,found);
            if(needed?(status!=Result::Found || found!=latest):status!=Result::Missing)return Stop(JobResult::Corrupt);
            return JobResult::More;
        }
        if(phase==Phase::Publish) {
            const auto status=catalog.Publish(current,SourceBytes(),
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
