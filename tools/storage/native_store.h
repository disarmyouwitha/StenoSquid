// Candidate-B store facade. One external engine/flash lock serializes calls.
// Maintenance leases survive between worker steps; readers use pinned inputs.
#pragma once
#include "native_checkpoint_job.h"
#include "native_rollover_job.h"

namespace candidate_b {
template <class Device>
class NativeStore {
public:
    Device &device;
    NativeCatalog<Device> catalog;
    LiveJournal<Device> live;
    NativeCheckpointJob<Device> checkpoint;
    NativeRolloverJob<Device> rollover;
    CatalogSelection selected{};
    enum class Work { None, Checkpoint, Rollover, PrepareCheckpoint, PrepareRollover } work=Work::None;
    bool opened=false;
    bool external_update=false;
    bool clean_checkpoint=false;
    JobResult last=JobResult::Done;
    explicit NativeStore(Device &value):device(value),catalog(value),live(value,catalog),
        checkpoint(value,catalog,live),rollover(value,catalog,live){}
    NativeStore(const NativeStore &)=delete;
    template<class Validate>
    CatalogResult Open(Validate validate) {
        if(work!=Work::None)return CatalogResult::Busy;
        opened=false;
        const auto result=catalog.OpenValidated(selected,validate);
        if(result!=CatalogResult::Ok && result!=CatalogResult::Quarantined)return result;
        if(live.Begin(selected)!=LiveResult::More)return CatalogResult::Corrupt;
        for(;;) {
            const auto step=live.RecoverStep();
            if(step==LiveResult::Ready){opened=true;last=JobResult::Done;return result;}
            if(step!=LiveResult::More)return step==LiveResult::IoError?CatalogResult::IoError:CatalogResult::Corrupt;
        }
    }
    bool Snapshot(LiveView &view) {
        if(!opened || view.token)return false;
        if(work==Work::None || work==Work::PrepareCheckpoint || work==Work::PrepareRollover)
            return live.Snapshot(view)==LiveResult::Ok;
        if(work==Work::Checkpoint && checkpoint.Published() && live.Ready())
            return live.Snapshot(view)==LiveResult::Ok;
        const auto &input=work==Work::Checkpoint?checkpoint.Input():rollover.Input();
        uint32_t token=0;
        if(!catalog.AcquirePin(input.root,token))return false;
        view=input;view.token=token;return true;
    }
    // An idle job may abandon unpublished index pages when typing resumes.
    bool CancelCheckpoint() {
        if(work==Work::Checkpoint && checkpoint.Cancel()!=JobResult::Cancelled)return false;
        if(work!=Work::PrepareCheckpoint && work!=Work::Checkpoint)return false;
        work=Work::None;last=JobResult::Cancelled;return true;
    }
    bool Release(LiveView &view){return live.Release(view);}
    JobResult Start(Work kind,bool compact=false) {
        if(!opened)return JobResult::ReadOnly;
        if(work!=Work::None || external_update)return JobResult::Busy;
        if(!catalog.CanWrite(selected.root))return JobResult::ReadOnly;
        if(kind!=Work::Checkpoint && kind!=Work::Rollover)return JobResult::Invalid;
        clean_checkpoint=compact;
        work=kind==Work::Checkpoint?Work::PrepareCheckpoint:Work::PrepareRollover;
        last=JobResult::More;return last;
    }
private:
    // Retire only ownership unique to the alternate catalog before allocating
    // output, only when staging cannot fit with both generations. Mirroring
    // the current payload retains two recoverable copies,
    // preserves sequence/tail cursors, and avoids staging a third index pair.
    template<class Check>
    JobResult Prepare(Check check) {
        const bool checkpoint_next=work==Work::PrepareCheckpoint;
        if(!catalog.CanWrite(selected.root))return JobResult::ReadOnly;
        if(catalog.DestinationPinned(selected))return JobResult::Busy;
        CatalogRoot alternate;
        const auto inspected=catalog.Inspect(uint8_t(1-selected.slot),alternate);
        if(inspected!=CatalogResult::Ok && inspected!=CatalogResult::Empty)return JobResult::RecoveryRequired;
        bool obsolete=false;
        if(inspected==CatalogResult::Ok) {
            SegmentMap current_map,alternate_map;uint32_t forward=0,reverse=0;
            if(RootOwnership(device,catalog,selected.root,current_map,&forward,&reverse)!=CatalogResult::Ok ||
               RootOwnership(device,catalog,alternate,alternate_map)!=CatalogResult::Ok)return JobResult::RecoveryRequired;
            for(unsigned i=0;i<8;++i)obsolete |= (alternate_map.words[i]&~current_map.words[i])!=0;
            if(obsolete) {
                const uint32_t tail=live.TailCount();
                if(checkpoint_next && (forward>kMaxEntries-tail || reverse>kMaxEntries-tail))return JobResult::NoSpace;
                auto room=[&](SegmentMap owned) {
                    AllocationPlan plan(owned);uint32_t start=0;
                    if(!checkpoint_next)return plan.Reserve(1,start);
                    const uint32_t f=(4*(forward+tail)+kSegmentBytes-1)/kSegmentBytes;
                    const uint32_t r=(4*(reverse+tail)+kSegmentBytes-1)/kSegmentBytes;
                    return plan.Reserve(f?f:1,start) && plan.Reserve(r?r:1,start);
                };
                auto both=current_map;both.Union(alternate_map);
                // Preserve the older generation without extra flash wear when
                // there is room. Retire it only to satisfy staging pressure.
                if(room(both))obsolete=false;
                else if(!room(current_map))return JobResult::NoSpace;
            }
        }
        if(obsolete) {
            const CatalogSelection previous=selected;
            auto source=[&](uint32_t at,uint8_t *out,uint32_t bytes) {
                return at<=previous.root.bytes && bytes<=previous.root.bytes-at &&
                    device.Read(previous.root.address+at,out,bytes);
            };
            CatalogSelection next;
            const auto status=catalog.Publish(previous,previous.root.bytes,source,check,next,&previous.root.crc);
            if(status==CatalogResult::Busy)return JobResult::Busy;
            if(status!=CatalogResult::Ok || !live.Retarget(next))return JobResult::RecoveryRequired;
            selected=next;
        }
        // The source root may have an older reader after mirroring. Do not
        // erase/program staged outputs only to discover this at publication.
        if(catalog.DestinationPinned(selected))return JobResult::Busy;
        const auto started=checkpoint_next?checkpoint.Begin(selected,{},clean_checkpoint):rollover.Begin(selected);
        if(started==JobResult::More)work=checkpoint_next?Work::Checkpoint:Work::Rollover;
        return started;
    }
public:
    template<class Check>
    JobResult Step(Check check) {
        if(work==Work::None)return JobResult::Invalid;
        if(work==Work::PrepareCheckpoint || work==Work::PrepareRollover) {
            const auto status=Prepare(check);
            if(status==JobResult::More)return status;
            last=status;work=Work::None;
            if(status==JobResult::RecoveryRequired || status==JobResult::Corrupt || status==JobResult::IoError || status==JobResult::Invalid) {
                opened=false;catalog.Invalidate();
            }
            return status;
        }
        const auto status=work==Work::Checkpoint?checkpoint.Step(check):rollover.Step(check);
        if(status==JobResult::More)return status;
        if(status==JobResult::Done)selected=work==Work::Checkpoint?checkpoint.Selection():rollover.Selection();
        last=status;
        if(status==JobResult::RecoveryRequired || status==JobResult::Corrupt || status==JobResult::IoError || status==JobResult::Invalid)opened=false;
        work=Work::None;return status;
    }
    LiveResult Edit(uint32_t id,uint8_t operation,const uint32_t *strokes,uint8_t count,const uint8_t *text,uint16_t bytes) {
        if(!opened)return LiveResult::ReadOnly;
        if(work!=Work::None || external_update)return LiveResult::Busy;
        const auto result=live.Append(id,operation,strokes,count,text,bytes);
        if(result==LiveResult::NeedCheckpoint || result==LiveResult::NeedJournal) {
            if(last==JobResult::NoSpace)return LiveResult::ReadOnly;
            const auto started=Start(result==LiveResult::NeedCheckpoint?Work::Checkpoint:Work::Rollover);
            return started==JobResult::More || started==JobResult::Busy?LiveResult::Busy:LiveResult::ReadOnly;
        }
        if(result==LiveResult::Corrupt || result==LiveResult::IoError || result==LiveResult::Stale)opened=false;
        return result;
    }
    bool Row(uint32_t index,uint8_t (&row)[96]) {
        uint8_t prefix[8];
        return opened && device.Read(selected.root.address,prefix,8) &&
            index<(uint32_t(prefix[4]) | uint32_t(prefix[5])<<8) &&
            device.Read(selected.root.address+32+index*96,row,96);
    }
    // Atomically clear an overlay-only logical dictionary by replacing its ID.
    // Old IDs are never reused; old records remain unreachable until cleaning.
    // Caller owns the scratch view and may not reuse a leased export/lookup view.
    template<class Check>
    CatalogResult Clear(uint32_t id,uint32_t &replacement,LiveView &pin,Check check) {
        if(!opened || work!=Work::None || external_update || !id || pin.token)return CatalogResult::Busy;
        if(!catalog.CanWrite(selected.root))return CatalogResult::Quarantined;
        uint8_t row[96]{},prefix[32];uint32_t index=0;
        for(;Row(index,row);++index)if(Word(row)==id)break;
        if(Word(row)!=id || row[8]!=255 || row[9]!=255 || !device.Read(selected.root.address,prefix,32))return CatalogResult::Invalid;
        const uint32_t next_id=Word(prefix);
        if(!next_id || next_id==UINT32_MAX)return CatalogResult::Invalid;
        if(!Snapshot(pin))return CatalogResult::Busy;
        auto source=[&](uint32_t at,uint8_t *out,uint32_t n) {
            if(at>pin.root.bytes || n>pin.root.bytes-at || !device.Read(pin.root.address+at,out,n))return false;
            for(uint32_t i=0;i<n;++i) {
                if(at+i<4)out[i]=uint8_t((next_id+1)>>(8*(at+i)));
                if(at+i>=32+index*96 && at+i<36+index*96)out[i]=uint8_t(next_id>>(8*(at+i-32-index*96)));
            }
            return true;
        };
        CatalogSelection next;
        const auto status=catalog.Publish(selected,selected.root.bytes,source,check,next);
        if(status==CatalogResult::Ok) {
            selected=next;replacement=next_id;
            if(live.Begin(next)!=LiveResult::More){opened=false;catalog.Invalidate();Release(pin);return CatalogResult::Corrupt;}
            for(;;){auto step=live.RecoverStep();if(step==LiveResult::Ready)break;if(step!=LiveResult::More){opened=false;catalog.Invalidate();Release(pin);return CatalogResult::Corrupt;}}
        } else if(status!=CatalogResult::Busy){opened=false;catalog.Invalidate();}
        Release(pin);return status;
    }

    // Catalog-only flag update. Extents, IDs, ordering and checkpoint sequence
    // stay byte-identical, so the live tail can be retargeted without replay.
    // An existing lease in caller-owned scratch is refused before any mutation.
    template<class Check>
    CatalogResult Enable(uint32_t id,bool enabled,LiveView &pin,Check check) {
        if(!opened || work!=Work::None || external_update || pin.token)return CatalogResult::Busy;
        if(!catalog.CanWrite(selected.root))return CatalogResult::Quarantined;
        if(!id)return CatalogResult::Invalid;
        uint8_t row[96]{};uint32_t index=0;
        for(;Row(index,row);++index)if(Word(row)==id)break;
        if(Word(row)!=id)return CatalogResult::Invalid;
        if(Word(row+4)==unsigned(enabled))return CatalogResult::Ok;
        if(!Snapshot(pin))return CatalogResult::Busy;
        const uint32_t patch=32+index*96+4;
        auto source=[&](uint32_t offset,uint8_t *out,uint32_t n) {
            if(offset>pin.root.bytes || n>pin.root.bytes-offset || !device.Read(pin.root.address+offset,out,n))return false;
            for(uint32_t i=0;i<n;++i)if(offset+i>=patch && offset+i<patch+4)out[i]=(offset+i==patch)?uint8_t(enabled):0;
            return true;
        };
        CatalogSelection next;
        const auto status=catalog.Publish(selected,selected.root.bytes,source,check,next);
        if(status==CatalogResult::Ok) {
            if(!live.Retarget(next)){opened=false;catalog.Invalidate();Release(pin);return CatalogResult::Corrupt;}
            selected=next;
        } else if(status!=CatalogResult::Busy){opened=false;catalog.Invalidate();}
        Release(pin);return status;
    }
};

// Enumerate current overlay records exactly once without a dictionary-sized map.
// Old checkpoint rows superseded by the tail are skipped; only newest tail rows
// survive. The cursor owns no pin: caller holds its LiveView for the whole walk.
struct OverlayCursor {uint32_t indexed=0,recent=0;};
template<class Device>
LiveResult NextOverlay(Device &device,LiveJournal<Device> &live,const LiveView &view,
                       OverlayCursor &cursor,MergeRecord &record,bool &more) {
    IndexSink<Device> unused_f(device),unused_r(device);
    NativeMergeOps<Device> ops(device,view,unused_f,unused_r);
    for(uint32_t examined=0;examined<64;++examined) {
        uint32_t address;
        if(cursor.indexed<view.forward_count) {
            uint8_t bytes[4];
            if(!device.Read(view.forward_address+cursor.indexed*4,bytes,4))return LiveResult::IoError;
            ++cursor.indexed;address=Word(bytes);
        } else if(cursor.recent<view.tail.count)address=view.tail.addresses[cursor.recent++];
        else {more=false;return LiveResult::Missing;}
        if(!ops.Record(address,record,true))return LiveResult::Corrupt;
        if(!ops.Active(record.identity))continue;
        RecordView latest;
        const auto result=live.FindSnapshot(view,record.identity,record.strokes,record.count,latest);
        if(result!=LiveResult::Ok && result!=LiveResult::Missing)return result;
        if(result==LiveResult::Ok && latest.operation==Operation::Set &&
           latest.text_address==record.text_address){more=true;return LiveResult::Ok;}
    }
    more=true;return LiveResult::More;
}
} // namespace candidate_b
