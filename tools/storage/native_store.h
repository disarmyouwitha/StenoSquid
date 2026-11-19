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
    enum class Work { None, Checkpoint, Rollover } work=Work::None;
    bool opened=false;
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
        if(work==Work::None)return live.Snapshot(view)==LiveResult::Ok;
        const auto &input=work==Work::Checkpoint?checkpoint.Input():rollover.Input();
        uint32_t token=0;
        if(!catalog.AcquirePin(input.root,token))return false;
        view=input;view.token=token;return true;
    }
    bool Release(LiveView &view){return live.Release(view);}
    JobResult Start(Work kind) {
        if(!opened)return JobResult::ReadOnly;
        if(work!=Work::None)return JobResult::Busy;
        if(!catalog.CanWrite(selected.root))return JobResult::ReadOnly;
        const auto status=kind==Work::Checkpoint?checkpoint.Begin(selected):
            kind==Work::Rollover?rollover.Begin(selected):JobResult::Invalid;
        if(status==JobResult::More){work=kind;last=status;}
        return status;
    }
    template<class Check>
    JobResult Step(Check check) {
        if(work==Work::None)return JobResult::Invalid;
        const auto status=work==Work::Checkpoint?checkpoint.Step(check):rollover.Step(check);
        if(status==JobResult::More)return status;
        if(status==JobResult::Done)selected=work==Work::Checkpoint?checkpoint.Selection():rollover.Selection();
        last=status;
        if(status==JobResult::RecoveryRequired || status==JobResult::Corrupt || status==JobResult::IoError || status==JobResult::Invalid)opened=false;
        work=Work::None;return status;
    }
    LiveResult Edit(uint32_t id,uint8_t operation,const uint32_t *strokes,uint8_t count,const uint8_t *text,uint16_t bytes) {
        if(!opened)return LiveResult::ReadOnly;
        if(work!=Work::None)return LiveResult::Busy;
        const auto result=live.Append(id,operation,strokes,count,text,bytes);
        if(result==LiveResult::NeedCheckpoint || result==LiveResult::NeedJournal) {
            if(last==JobResult::NoSpace)return LiveResult::ReadOnly;
            const auto started=Start(result==LiveResult::NeedCheckpoint?Work::Checkpoint:Work::Rollover);
            return started==JobResult::More?LiveResult::Busy:LiveResult::ReadOnly;
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
        if(!opened || work!=Work::None || !id || pin.token)return CatalogResult::Busy;
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
        if(!opened || work!=Work::None || pin.token)return CatalogResult::Busy;
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
