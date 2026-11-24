// Native dictionary engine and QSPI command handlers.
#include "native_store_runtime.h"
#include "storage_probe.h"
#include "native_input.h"
#include "native_warm_resume.h"
#include "clock.h"
#include "../../tools/storage/native_work_slice.h"
#include "native_collection_sync.h"
#include "native_dictionary_adapter.h"
#include "dictionary_benchmark.h"
#include "native_engine_backend.h"
#include "../../tools/storage/native_store.h"
#include "../../tools/storage/native_validator.h"
#include "../../tools/storage/native_provisioner.h"
#include "../../tools/storage/native_stage.h"
#include "../../tools/storage/native_entry_stager.h"
#include "../../tools/storage/native_layout_map.h"
#include "../../tools/storage/native_overlay_usage.h"
#include "../../tools/storage/reverse_cursor.h"
#include "dictionary/compact_map_dictionary.h"
#include "dictionary/dictionary_definition.h"
#include "lapwing_dictionary.h"
#include "user_dictionary_store.h"
#include "dictionary_flash.h"
#include "javelin_engine_lock.h"
#include "javelin_hid_transport.h"
#include "engine_work.h"
#include "engine_stream.h"
#include "hint_index.h"
#include "stroke_list_parser.h"
#include "split_status.h"
#include "w25q_quad.h"
#include "xip_load.h"
#include <zephyr/devicetree.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/usb/usb_device.h>
#include <zephyr/usb/class/usb_hid.h>
#include <zmk/keys.h>
#include <zmk/hid.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/base64.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/settings/settings.h>
#include <new>
#include <cstdlib>
#include <cstring>

extern "C" {
#include <zmk/activity.h>
#include <zmk/usb.h>
extern struct k_mutex flash_lock;
extern atomic_t lf_mapped_ready,lf_checked;
extern bool lf_suspended,lf_provisioning_ready;
extern uint32_t lf_flash_bytes;
}
namespace {
using namespace candidate_b;
constexpr uintptr_t Xip=DT_REG_ADDR_BY_NAME(DT_NODELABEL(qspi),qspi_mm);
static_assert(DT_REG_SIZE_BY_NAME(DT_NODELABEL(qspi),qspi_mm) >= kMaxSegments*kSegmentBytes);
bool ForgetBaseTrust();
extern int64_t scrub_due;
constexpr size_t MaxDictionaries=CONFIG_ZMK_STENO_SQUID_NATIVE_DICTIONARIES;
struct FlashDevice {
    bool mapped=false,trust_bases=false;
    int64_t cold_read_yield_at=0;
    alignas(4) uint8_t dma[256];
    struct Cached {uint32_t at=0,bytes=0,crc=0;} cached[32];
    unsigned cache_next=0;
    uint32_t PoolEnd() const { return lf_flash_bytes ? lf_flash_bytes/kSegmentBytes-1 : 0; }
    uint32_t Limit() const { return PoolEnd()*kSegmentBytes; }
    void Invalidate(){for(auto &c:cached)c.bytes=0;}
    void Dirty(uint32_t at,uint32_t n){for(auto &c:cached)if(c.bytes && at<c.at+c.bytes && c.at<at+n)c.bytes=0;}
    bool Read(uint32_t at,uint8_t *out,uint32_t n) {
        if(at>lf_flash_bytes || n>lf_flash_bytes-at)return false;
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
        if (squid_bench_count_reads) {
            ++squid_bench_personal_reads;
            squid_bench_personal_bytes += n;
        }
#endif
        if (cold_read_yield_at && k_uptime_get() >= cold_read_yield_at) {
            k_sleep(K_TICKS(1));
            cold_read_yield_at = k_uptime_get() + 5;
        }
        if(mapped){copy_xip_bytes(out,reinterpret_cast<const void *>(Xip+at),n);return true;}
        while(n) {
            uint32_t skip=at%4,count=n<256-skip?n:256-skip;
            if(nrfx_qspi_read(dma,(skip+count+3)&~3u,at-skip)!=NRFX_SUCCESS)return false;
            std::memmove(out,dma+skip,count);at+=count;out+=count;n-=count;
        }
        return true;
    }
    bool Program(uint32_t at,const uint8_t *data,uint32_t n) {
        if(mapped || !n || n>256 || at%4 || at>=Limit() || n>Limit()-at || at/256!=(at+n-1)/256)return false;
        Dirty(at,(n+3)&~3u);std::memset(dma,255,256);std::memcpy(dma,data,n);
        return nrfx_qspi_write(dma,(n+3)&~3u,at)==NRFX_SUCCESS && squid_qspi_wait_ready(2000)==NRFX_SUCCESS;
    }
    bool EraseBlock(uint32_t at, uint32_t bytes) {
        if (mapped || at >= Limit() || bytes > Limit()-at ||
            (bytes != kEraseSectorBytes && bytes != kSegmentBytes)) return false;
        Dirty(at, bytes);
        const auto length = bytes == kEraseSectorBytes ? NRF_QSPI_ERASE_LEN_4KB
                                                      : NRF_QSPI_ERASE_LEN_64KB;
        return nrfx_qspi_erase(length, at) == NRFX_SUCCESS &&
               squid_qspi_wait_ready(4000) == NRFX_SUCCESS;
    }
    bool Erase(uint32_t segment) { return EraseIndex(segment, kSegmentBytes); }
    bool EraseIndex(uint32_t segment, uint32_t bytes) {
        return segment >= 2 && segment < PoolEnd() && EraseBlock(segment * kSegmentBytes, bytes);
    }
    bool ErasePoolSector(uint32_t at) {
        return at >= 2 * kSegmentBytes && at < Limit() && !(at % kEraseSectorBytes) &&
               EraseBlock(at, kEraseSectorBytes);
    }
    bool EraseCatalog(uint32_t at, uint32_t bytes) {
        return (at == 0 || at == kSegmentBytes) && EraseBlock(at, bytes);
    }
    bool CheckBase(uint32_t at,uint32_t bytes,uint32_t expected) {
        if(trust_bases){cached[cache_next++%32]={at,bytes,expected};return true;}
        uint32_t crc;return Checksum(at,bytes,crc) && crc==expected;
    }
    bool Checksum(uint32_t at,uint32_t bytes,uint32_t &crc) {
        for(const auto &c:cached)if(c.bytes==bytes && c.at==at){crc=c.crc;return true;}
        alignas(4) uint8_t chunk[256];crc=0;
        for(uint32_t offset=0;offset<bytes;) {
            uint32_t n=bytes-offset;if(n>sizeof(chunk))n=sizeof(chunk);
            if(!Read(at+offset,chunk,n))return false;
            crc=crc32_ieee_update(crc,chunk,n);offset+=n;if(!(offset%4096))k_yield();
        }
        cached[cache_next++%32]={at,bytes,crc};return true;
    }
} flash_device;
NativeStore<FlashDevice> store(flash_device);
NativeValidator<FlashDevice> validator(flash_device,store.catalog);
bool healthy=false,loaded=false,exporting=false,maintenance_hold=false;
bool snapshot_format_rejected=false;
uint32_t maintenance_steps=0,warm_resumes=0,full_opens=0;
uint32_t maintenance_max_slice_us=0;
using Maintenance = NativeStore<FlashDevice>::Work;
constexpr uint32_t IdleCheckpointTail = 64;
bool automatic_maintenance = false;
atomic_t idle_maintenance_pending;
uint32_t automatic_checkpoints = 0, automatic_reclaimed = 0;
JobResult automatic_result = JobResult::Done;
StenoDictionary *parent=nullptr;
LiveView export_view;
OverlayCursor export_cursor;
alignas(4) char export_text[2048];

class BaseProvider final : public NativeBaseProvider {
public:
    bool Ready()const override{return healthy;}
    void Fault()override {
        (void)ForgetBaseTrust();healthy=store.opened=false;atomic_clear(&lf_mapped_ready);atomic_set(&lf_checked,1);dictionary_flash_ready_changed();
    }
    const StenoDictionary *FindBase(uint32_t) const override;
} base_provider;
NativeEngineBackend<FlashDevice> backend(store,base_provider);
class Cell final : public StenoDictionary {
public:
    uint32_t identity=0,base_offset=0,base_bytes=0,base_crc=0;
    bool enabled=false, user_snapshot=false, factory_absent=false;
    uint8_t base_member = 0;
    char name[45]{};
    StenoCompactMapDictionaryDefinition definition{};
    StenoCompactMapDictionaryStrokesDefinition strokes[16]{};
    StenoCompactMapDictionary *base=nullptr;
    alignas(NativeOverlayDictionary) uint8_t overlay_storage[sizeof(NativeOverlayDictionary)];
    alignas(StenoSquidHintIndex) uint8_t hint_storage[sizeof(StenoSquidHintIndex)];
    StenoSquidHintIndex *hint=nullptr;
    Cell():StenoDictionary(16){}
    bool Probe()const;
    void Refresh() {
        maximumOutlineLength=store.live.MaximumOutline(identity);
        if(base && definition.maximumOutlineLength>maximumOutlineLength)maximumOutlineLength=definition.maximumOutlineLength;
    }
    void Bind(){new (overlay_storage) NativeOverlayDictionary(backend,identity,name);Refresh();}
    const char *GetName()const override{return name;}
    StenoDictionaryLookupResult Lookup(const StenoDictionaryLookup &lookup)const override {
        if(factory_absent || lookup.length>maximumOutlineLength)return StenoDictionaryLookupResult::CreateInvalid();
        return reinterpret_cast<const NativeOverlayDictionary *>(overlay_storage)->Lookup(lookup);
    }
    bool CanRemove()const override{return !factory_absent;}
    bool Remove(const char *label,const StenoStroke *outline,size_t count)override;
    void ReverseLookup(StenoReverseDictionaryLookup &lookup)const override;
    void PrintDictionary(PrintDictionaryContext &context)const override;
    void PrintEntriesWithPrefix(PrintPrefixContext &context)const override;
    void PrintEntriesWithPartialOutline(PrintPartialOutlineContext &context)const override;
    bool LoadBase(uint32_t at, uint32_t bytes, uint32_t crc, uint8_t member) {
        if (base) return base_offset == at && base_bytes == bytes && base_crc == crc &&
                         base_member == member;
        base_offset=at;base_bytes=bytes;base_crc=crc;base_member=member;
        const uintptr_t address=Xip+at;
        auto in=[&](uintptr_t p,size_t n){return p>=address && p-address<=bytes && n<=bytes-(p-address);};
        const auto *collection=reinterpret_cast<const uint8_t *>(address);
        constexpr size_t pointers = offsetof(StenoDictionaryCollection, dictionaries);
        if (bytes < pointers || xip_load_word(collection) != STENO_MAP_DICTIONARY_COLLECTION_MAGIC)
            return false;
        const uint32_t count = xip_load_word(collection + offsetof(StenoDictionaryCollection, dictionaryCount));
        if (count != 1 || member || bytes - pointers < sizeof(uint32_t))
            return false;
        const uintptr_t map = xip_load_word(collection + pointers + member * sizeof(uint32_t));
        if(map%4 || !in(map,sizeof(definition)))return false;
        copy_xip_bytes(&definition,reinterpret_cast<const void *>(map),sizeof(definition));
        const uintptr_t table=reinterpret_cast<uintptr_t>(definition.strokes);
        if(definition.type!=StenoDictionaryType::COMPACT_MAP || !definition.maximumOutlineLength ||
           definition.maximumOutlineLength>16 || table%4 || !in(table,definition.maximumOutlineLength*sizeof(strokes[0])))return false;
        copy_xip_bytes(strokes,definition.strokes,definition.maximumOutlineLength*sizeof(strokes[0]));
        for(unsigned i=0;i<definition.maximumOutlineLength;++i) {
            const auto data=reinterpret_cast<uintptr_t>(strokes[i].data),hash=reinterpret_cast<uintptr_t>(strokes[i].offsets);
            const uint32_t mask=strokes[i].hashMapMask;
            if(!mask)continue;
            if(!in(data,1) || hash%4 || mask<127 || (mask&(mask+1)) || mask>0x7fffff || !in(hash,(uint64_t(mask)+1+127)/128*sizeof(StenoCompactHashMapEntryBlock)))return false;
        }
        if(!in(reinterpret_cast<uintptr_t>(definition.textBlock),1))return false;
        const uint32_t text_bytes=xip_load_word(collection+offsetof(StenoDictionaryCollection,textBlock));
        hint=new (hint_storage) StenoSquidHintIndex(address,bytes,member,count,reinterpret_cast<uintptr_t>(definition.textBlock),text_bytes,xip_load_word,xip_load_byte);
        if (user_snapshot && !hint->Available()) return false;
        definition.strokes=strokes;definition.name=name;
        base=new (definition) StenoCompactMapDictionary(definition);
        return base!=nullptr;
    }
    uint32_t GroupEntries(const StenoCompactMapDictionaryStrokesDefinition &group) const {
        const auto last = reinterpret_cast<uintptr_t>(group.offsets) +
            (group.hashMapMask / 128) * sizeof(StenoCompactHashMapEntryBlock);
        // Compact hash blocks store the preceding entry's zero-based index.
        uint32_t count = xip_load_word(reinterpret_cast<const void *>(last + 16)) + 1u;
        for (unsigned offset = 0; offset < 16; offset += 4)
            count += __builtin_popcount(xip_load_word(reinterpret_cast<const void *>(last + offset)));
        return count;
    }
} cells[MaxDictionaries];
size_t cell_count=0;
Cell *ById(uint32_t id){for(size_t i=0;i<cell_count;++i)if(cells[i].identity==id)return &cells[i];return nullptr;}
Cell *ByName(const char *name){for(size_t i=0;i<cell_count;++i)if(!std::strcmp(cells[i].name,name))return &cells[i];return nullptr;}
bool Cell::Probe()const {
    if(!base)return true;
    for(unsigned length=1;length<=definition.maximumOutlineLength;++length) {
        const auto &group=strokes[length-1];if(!group.hashMapMask)continue;
        const uintptr_t at=reinterpret_cast<uintptr_t>(group.data),end=reinterpret_cast<uintptr_t>(group.offsets);
        if(end<at || 3u*(length+1)>end-at)return false;
        StenoStroke outline[16];
        for(unsigned i=0;i<length;++i) {
            const uint32_t mask=xip_load_u24(group.data+3+3*i);if(!mask)return false;outline[i]=StenoStroke(mask);
        }
        auto expected=lapwing_copy_lookup_text(definition.textBlock+xip_load_u24(group.data));
        auto actual=base->Lookup(StenoDictionaryLookup(outline,length));
        const bool good=expected.IsValid() && actual.IsValid() && !std::strcmp(expected.GetText(),actual.GetText());
        expected.Destroy();actual.Destroy();return good;
    }
    return false;
}
const StenoDictionary *BaseProvider::FindBase(uint32_t id) const {
    const auto *cell=ById(id);
    return cell ? cell->base : nullptr;
}
void StepWork(k_work *);
K_WORK_DELAYABLE_DEFINE(maintenance_work,StepWork);
void FinishMaintenance() {
    if (maintenance_hold) {
        dictionary_flash_read_session(false);
        maintenance_hold = false;
        steno_squid_split_maintenance(false);
    }
}
void IdleMaintenance(k_work *);
K_WORK_DELAYABLE_DEFINE(idle_maintenance_work, IdleMaintenance);
void QueueIdleMaintenance(k_timeout_t delay = K_SECONDS(10)) {
    if (atomic_get(&idle_maintenance_pending) && zmk_usb_is_powered())
        k_work_schedule_for_queue(&steno_squid_engine_queue, &idle_maintenance_work, delay);
}
void NoteIdleMaintenance() {
    if (store.live.TailCount() >= IdleCheckpointTail) {
        atomic_set(&idle_maintenance_pending, 1);
        QueueIdleMaintenance();
    }
}
bool IdleMaintenanceEnvironment() {
    return zmk_usb_is_powered() && zmk_activity_get_state() == ZMK_ACTIVITY_IDLE;
}
bool IdleMaintenanceAllowed() {
    return IdleMaintenanceEnvironment() && !steno_squid_native_input_pending();
}
void Kick() {
    if (store.work == NativeStore<FlashDevice>::Work::None) return;
    if (!maintenance_hold) {
        dictionary_flash_read_session(true);
        maintenance_hold = true;
        steno_squid_split_maintenance(true);
    }
    k_work_schedule_for_queue(&steno_squid_engine_queue, &maintenance_work, K_MSEC(2));
}
void IdleMaintenance(k_work *) {
    if (!IdleMaintenanceAllowed()) {
        if (zmk_usb_is_powered()) QueueIdleMaintenance();
        return;
    }
    // Only a pending job on external power can wake an idle-gated flash.
    if (dictionary_flash_status() != DICTIONARY_FLASH_READY) return;
    const JavelinEngineLock lock;
    if (!lock || !healthy) return;
    if (store.live.TailCount() < IdleCheckpointTail) {
        atomic_clear(&idle_maintenance_pending);
        return;
    }
    if (exporting || store.work != Maintenance::None || store.external_update) {
        QueueIdleMaintenance();
        return;
    }
    automatic_result = store.Start(Maintenance::Checkpoint,true);
    if (automatic_result == JobResult::More) {
        automatic_maintenance = true;
        Kick();
    }
}
void StepWork(k_work *) {
    const JavelinEngineLock lock;
    if (!lock) {
        if (!healthy) FinishMaintenance();
        else Kick();
        return;
    }
    if (automatic_maintenance && !IdleMaintenanceAllowed()) {
        store.checkpoint.SkipMirror();
        if (store.CancelCheckpoint()) {
            automatic_maintenance = false;
            automatic_result = JobResult::Cancelled;
            FinishMaintenance();
            QueueIdleMaintenance(K_SECONDS(60));
            return;
        }
    }
    if (steno_squid_native_input_pending()) { Kick(); return; }
    const NativeInputPause input_pause;
    flash_device.mapped=false;
    uint32_t steps=0;
    const uint32_t slice_started = Clock::GetMicroseconds();
    const auto result=NativeWorkSlice(
        []{
            const auto status=store.Step([](uint32_t i,const uint8_t *row){return validator.Extent(i,row);});
            if(status==JobResult::More || status==JobResult::Done)++maintenance_steps;
            return status;
        },
        []{return Clock::GetMicroseconds();},
        [] {
            // The job holds an input pause here. Check real queued input,
            // not the pause itself, when deciding whether this slice may run.
            return steno_squid_native_input_waiting() ||
                   (automatic_maintenance && !store.checkpoint.Published() && !IdleMaintenanceEnvironment());
        },steps);
    flash_device.mapped=true;
    const uint32_t elapsed = Clock::GetMicroseconds() - slice_started;
    if (steps && elapsed > maintenance_max_slice_us) maintenance_max_slice_us = elapsed;
    if(result==JobResult::More){Kick();return;}
    if (automatic_maintenance) {
        automatic_maintenance = false;
        automatic_result = result;
        if (result == JobResult::Done) {
            ++automatic_checkpoints;
            automatic_reclaimed += store.checkpoint.ReclaimedJournals();
        }
        if (result == JobResult::Busy) QueueIdleMaintenance(K_SECONDS(60));
        else atomic_clear(&idle_maintenance_pending);
    }
    if(!store.opened)base_provider.Fault();
    else if(result==JobResult::Done){for(size_t i=0;i<cell_count;++i)cells[i].Refresh();if(parent)parent->OnLookupDataChanged();}
    FinishMaintenance();
}
LiveResult Edit(Cell &cell,uint8_t operation,const StenoStroke *outline,size_t count,const char *text,size_t bytes) {
    if (cell.factory_absent) return LiveResult::ReadOnly;
    if(!healthy || !count || count>16 || bytes>2047)return LiveResult::Invalid;
    if(steno_squid_native_input_pending())return LiveResult::Busy;
    const NativeInputPause input_pause;
    uint32_t strokes[16];for(size_t i=0;i<count;++i)strokes[i]=outline[i].GetKeyState();
    flash_device.mapped=false;
    const auto result=store.Edit(cell.identity,operation,strokes,count,reinterpret_cast<const uint8_t *>(text),bytes);
    flash_device.mapped=true;
    if(result==LiveResult::Ok){
        cell.Refresh();if(parent)parent->OnLookupDataChanged();
        NoteIdleMaintenance();
    }
    if(!store.opened)base_provider.Fault();
    Kick();return result;
}
bool Cell::Remove(const char *label,const StenoStroke *outline,size_t count) {
    return (!label || !std::strcmp(label,name)) && Edit(*this,1,outline,count,nullptr,0)==LiveResult::Ok;
}
struct ExportGuard {
    bool ok=false;
    ExportGuard(){if(!exporting && healthy && store.Snapshot(export_view)){exporting=true;dictionary_flash_read_session(true);export_cursor={};ok=true;}}
    ~ExportGuard(){if(ok){store.Release(export_view);exporting=false;dictionary_flash_read_session(false);}}
};
// Iteration scratch stays off the HID stack.
IndexSink<FlashDevice> iter_f(flash_device),iter_r(flash_device);
NativeMergeOps<FlashDevice> iter_ops(flash_device,export_view,iter_f,iter_r);
bool SeekOverlay(uint32_t identity) {
    uint32_t first = 0, last = export_view.forward_count;
    while (first < last) {
        const uint32_t mid = first + (last - first) / 2;
        MergeRecord record;
        uint32_t address;
        if (!iter_ops.ReadIndex(false, mid, address) || !iter_ops.Record(address, record)) {
            base_provider.Fault(); return false;
        }
        if (record.identity < identity) first = mid + 1;
        else last = mid;
    }
    export_cursor.indexed = first;
    return true;
}
bool Next(MergeRecord &record, uint32_t identity = 0) {
    for(;;) {
        uint32_t address;
        const bool indexed = export_cursor.indexed < export_view.forward_count;
        if(indexed) {
            uint8_t word[4];if(!flash_device.Read(export_view.forward_address+export_cursor.indexed++*4,word,4)){base_provider.Fault();return false;}
            address=Word(word);
        } else if(export_cursor.recent<export_view.tail.count)address=export_view.tail.addresses[export_cursor.recent++];
        else return false;
        if(!iter_ops.Record(address,record)){base_provider.Fault();return false;}
        if (identity && record.identity != identity) {
            if (indexed && record.identity > identity) export_cursor.indexed = export_view.forward_count;
            continue;
        }
        if(!iter_ops.Active(record.identity))continue;
        RecordView newest;
        const auto result=store.live.FindSnapshot(export_view,record.identity,record.strokes,record.count,newest);
        if(result!=LiveResult::Ok && result!=LiveResult::Missing){base_provider.Fault();return false;}
        if(result==LiveResult::Ok && newest.operation==Operation::Set && newest.text_address==record.text_address) {
            if (!iter_ops.Record(address, record, true)) { base_provider.Fault(); return false; }
            return true;
        }
    }
}
void Cell::PrintDictionary(PrintDictionaryContext &context)const {
    if(context.HasName() && std::strcmp(context.GetName(),name))return;
    ExportGuard guard;if(!guard.ok)return;
    StenoSquidExportWriter writer(steno_squid_javelin_response_active());MergeRecord record;
    while(!writer.Failed() && !steno_squid_javelin_response_cancelled() && Next(record)) {
        if(record.identity!=identity)continue;
        if(!flash_device.Read(record.text_address,reinterpret_cast<uint8_t *>(export_text),record.text_bytes)){base_provider.Fault();return;}
        export_text[record.text_bytes]=0;StenoStroke outline[16];
        for(unsigned i=0;i<record.count;++i)outline[i]=StenoStroke(record.strokes[i]);
        context.Print(outline,record.count,export_text);
    }
    if(!base || !healthy || writer.Failed())return;
    struct Filter final : PrintDictionaryContext {
        const Cell &cell;PrintDictionaryContext &output;
        Filter(const Cell &c,PrintDictionaryContext &o):cell(c),output(o){}
        bool HasName()const override{return false;}
        const char *GetName()const override{return nullptr;}
        void Print(const StenoStroke *s,size_t count,const char *text)override {
            if(!healthy || steno_squid_javelin_response_cancelled() || count>16)return;
            uint32_t masks[16];for(size_t i=0;i<count;++i)masks[i]=s[i].GetKeyState();
            RecordView record;const auto status=store.live.FindSnapshot(export_view,cell.identity,masks,count,record);
            if(status==LiveResult::Missing || (status==LiveResult::Ok && record.operation==Operation::Reset))output.Print(s,count,text);
            else if(status!=LiveResult::Ok)base_provider.Fault();
        }
    } filter(*this,context);
    // Upstream's generic exporter uses byte loads on packed QSPI entries.
    // Walk the validated RAM definitions with the erratum-safe accessors instead.
    for(unsigned length=1;length<=definition.maximumOutlineLength;++length) {
        const auto &group=strokes[length-1];if(!group.hashMapMask)continue;
        const uint32_t entries=group.GetEntryCount(),stride=3*(length+1);
        const auto start=reinterpret_cast<uintptr_t>(group.data),end=reinterpret_cast<uintptr_t>(group.offsets);
        if(end<start || uint64_t(entries)*stride>end-start){base_provider.Fault();return;}
        for(uint32_t i=0;i<entries;++i) {
            if(!healthy || writer.Failed() || steno_squid_javelin_response_cancelled())return;
            const auto *entry=group.data+i*stride;StenoStroke outline[16];
            for(unsigned j=0;j<length;++j)outline[j]=StenoStroke(xip_load_u24(entry+3+3*j));
            const uint32_t offset=xip_load_u24(entry);
            auto text=lapwing_copy_lookup_text(definition.textBlock+offset);
            if(!text.IsValid()){base_provider.Fault();return;}
            filter.Print(outline,length,text.GetText());text.Destroy();
        }
    }
}
void Cell::PrintEntriesWithPrefix(PrintPrefixContext &context)const {
    ExportGuard guard;if(!guard.ok)return;MergeRecord record;
    if (user_snapshot && !enabled) return;
    if (!SeekOverlay(identity)) return;
    while(!context.IsDone() && !steno_squid_javelin_response_cancelled() && Next(record,identity)) {
        if(record.text_bytes<=context.prefixLength)continue;
        if(!flash_device.Read(record.text_address,reinterpret_cast<uint8_t *>(export_text),record.text_bytes)){base_provider.Fault();return;}
        export_text[record.text_bytes]=0;
        if(std::strncmp(export_text,context.prefix,context.prefixLength))continue;
        StenoStroke outline[16];for(unsigned i=0;i<record.count;++i)outline[i]=StenoStroke(record.strokes[i]);
        if (user_snapshot && parent && parent->GetDictionaryForOutline(outline, record.count) != this) continue;
        context.Add(outline,record.count,export_text,this);
    }
}
void Cell::PrintEntriesWithPartialOutline(PrintPartialOutlineContext &context)const {
    ExportGuard guard;if(!guard.ok)return;MergeRecord record;
    if (!context.length || (user_snapshot && !enabled)) return;
    if (!SeekOverlay(identity)) return;
    while(!context.IsDone() && !steno_squid_javelin_response_cancelled() && Next(record,identity)) {
        if(record.count<=context.length)continue;
        StenoStroke outline[16];for(unsigned i=0;i<record.count;++i)outline[i]=StenoStroke(record.strokes[i]);
        if(!StenoStroke::HasPartialOutline(outline,context.strokes,context.length,record.count-context.length))continue;
        if(!flash_device.Read(record.text_address,reinterpret_cast<uint8_t *>(export_text),record.text_bytes)){base_provider.Fault();return;}
        if (user_snapshot && parent && parent->GetDictionaryForOutline(outline, record.count) != this) continue;
        export_text[record.text_bytes]=0;context.Add(outline,record.count,export_text,this);
    }
}
void Cell::ReverseLookup(StenoReverseDictionaryLookup &lookup)const {
    if (factory_absent || (user_snapshot && !enabled)) return;
    bool yielded=false;
    auto stop=[&] {
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_HINT
        yielded = yielded || steno_squid_hint_should_yield();
#endif
        return yielded;
    };
    if (stop()) return;
    ExportGuard guard;if(!guard.ok || lookup.definitionLength>2047)return;
    auto matches=[&](uint32_t address) {
        if (stop()) return 0;
        MergeRecord record;if(!iter_ops.Record(address,record))return -1;
        if(record.identity!=identity || record.operation || record.count>=lookup.ignoreStrokeThreshold)return 0;
        RecordView current;
        const auto result=store.live.FindSnapshot(export_view,identity,record.strokes,record.count,current);
        if(result!=LiveResult::Ok && result!=LiveResult::Missing)return -1;
        if(result!=LiveResult::Ok || current.operation!=Operation::Set || current.text_address!=record.text_address)return 0;
        if (stop()) return 0;
        StenoStroke outline[16];for(unsigned i=0;i<record.count;++i)outline[i]=StenoStroke(record.strokes[i]);
        return !parent || parent->GetDictionaryForOutline(outline,record.count)==this?1:0;
    };
    auto compare=[&](uint32_t address) {
        if (stop()) return 2;
        MergeRecord record;if(!iter_ops.Record(address,record))return 2;
        return CompareText(address+32,record.record.payload_bytes,
            reinterpret_cast<const uint8_t *>(lookup.definition),lookup.definitionLength,
            [&](uint32_t at,uint8_t *out,size_t n){return !stop() && flash_device.Read(at,out,n);});
    };
    auto emit=[&](uint32_t address) {
        MergeRecord record;if(!iter_ops.Record(address,record)){base_provider.Fault();return;}
        StenoStroke outline[16];for(unsigned i=0;i<record.count;++i)outline[i]=StenoStroke(record.strokes[i]);
        lookup.AddResult(outline,record.count,this);
    };
    for(unsigned i=0;i<export_view.tail.count;++i) {
        const uint32_t address=export_view.tail.addresses[i];const int order=compare(address);
        if (yielded) return;
        // Only sets belong in a reverse search. Remove/reset records are valid
        // tail inputs but CompareText intentionally rejects their empty payload.
        const int op=iter_ops.Operation(address);if(op<0){base_provider.Fault();return;}if(op)continue;
        if(order==2){base_provider.Fault();return;}if(order)continue;
        const int accepted=matches(address);if(accepted<0){base_provider.Fault();return;}if(accepted)emit(address);
    }
    if (stop()) return;
    uint8_t prefix[8];if(!flash_device.Read(export_view.root.address,prefix,8)){base_provider.Fault();return;}
    uint32_t reverse=0,count=0;const uint32_t extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
    for(unsigned i=0;i<extents;++i) {
        if (stop()) return;
        uint8_t row[24];if(!flash_device.Read(export_view.root.address+32+export_view.dictionary_count*96+i*24,row,24)){base_provider.Fault();return;}
        if(Word(row+12)==4){reverse=(uint16_t(row[0]) | uint16_t(row[1])<<8)*kSegmentBytes;count=Word(row+4)/4;}
    }
    auto read=[&](uint32_t at,uint8_t *out,size_t n){return !stop() && at<=count*4 && n<=count*4-at && flash_device.Read(reverse+at,out,n);};
    ReverseCursor cursor;
    if(SeekReverse(count,read,compare,cursor)!=Result::Found){if(!yielded)base_provider.Fault();return;}
    while(cursor.next<cursor.end) {
        uint32_t page[16];unsigned used=0;
        const auto result=NextReverse(cursor,16,read,compare,matches,[&](uint32_t address){page[used++]=address;});
        if (yielded) return;
        if(result==PageResult::Corrupt || result==PageResult::IoError){base_provider.Fault();return;}
        for(unsigned i=0;i<used;++i)emit(page[i]);
        if(result==PageResult::Done)break;
    }
    uint32_t mask;
    if(!stop() && healthy && hint && lookup.ignoreStrokeThreshold>1 && hint->Find(lookup.definition,mask)) {
        const StenoStroke stroke(mask);
        if (!stop() && NativeBaselineVisible(store.live, export_view, identity, &mask, 1, enabled,
                [&] { return !parent || parent->GetDictionaryForOutline(&stroke,1)==this; },
                [] { base_provider.Fault(); }))
            if (!stop()) lookup.AddResult(&stroke,1,this);
    }
}
void ReplyEdit(LiveResult result) {
    if(result==LiveResult::Ok)Console::SendOk();
    else Console::Printf("%s",result==LiveResult::Busy?"ERR Native store busy; retry shortly\n\n":"ERR Native edit refused\n\n");
}
void add_entry(void *,const char *line) {
    auto *personal=ByName("user_dictionary");StrokeListParser parser;const char *outline=std::strchr(line,' ');
    if(!personal || !outline || !parser.Parse(outline+1) || !parser.GetCount() || !*parser.failureOrEnd){Console::Printf("ERR Invalid outline or translation\n\n");return;}
    ReplyEdit(Edit(*personal,0,parser.GetData(),parser.GetCount(),parser.failureOrEnd,std::strlen(parser.failureOrEnd)));
}
void remove_entry(void *,const char *line) {
    auto *personal=ByName("user_dictionary");StrokeListParser parser;const char *outline=std::strchr(line,' ');
    if(!personal || !outline || !parser.Parse(outline+1) || !parser.GetCount() || *parser.failureOrEnd){Console::Printf("ERR Invalid outline\n\n");return;}
    ReplyEdit(Edit(*personal,1,parser.GetData(),parser.GetCount(),nullptr,0));
}
void native_catalog(void *, const char *) {
    Console::Printf("[");
    for (size_t i = 0; i < cell_count; ++i) {
        if (steno_squid_javelin_response_cancelled()) return;
        const auto &cell = cells[i];
        uint8_t row[96];
        if (!store.Row(i, row)) return;
        char digest[65];
        for (unsigned byte = 0; byte < 32; ++byte)
            snprintk(digest + byte * 2, 3, "%02x", unsigned(row[64 + byte]));
        uint32_t base_entries = 0;
        if (cell.base) {
            for (unsigned length = 0; length < cell.definition.maximumOutlineLength; ++length) {
                const auto &group = cell.strokes[length];
                if (group.hashMapMask) base_entries += cell.GroupEntries(group);
            }
        }
        Console::Printf("%s{\"id\":%u,\"name\":\"%s\",\"enabled\":%s,\"kind\":\"%s\","
                        "\"base_entries\":%u,\"image_bytes\":%u,\"image_crc\":%u,\"source_sha256\":\"%s\"}",
                        i ? "," : "", cell.identity, cell.name, cell.enabled ? "true" : "false",
                        cell.factory_absent ? "uninstalled" : cell.user_snapshot ? "snapshot" : cell.base ? "base" :
                            !std::strcmp(cell.name, "user_dictionary") ? "personal" : "named",
                        base_entries, cell.base_bytes, cell.base_crc, digest);
    }
    Console::Printf("]\n\n");
}
void lookup_outline(void *,const char *line) {
    StrokeListParser parser;const char *outline=std::strchr(line,' ');
    if(!parent || !outline || !parser.Parse(outline+1) || !parser.GetCount()){Console::Printf("ERR Invalid outline\n\n");return;}
    auto result=parent->Lookup(StenoDictionaryLookup(parser.GetData(),parser.GetCount()));
    if(result.IsValid()){Console::Printf("%s\n\n",result.GetText());result.Destroy();}
    else Console::Printf("ERR No translation\n\n");
}
void store_status(void *,const char *) {
    const auto *personal=ByName("user_dictionary");
    Console::Printf(
        "{\"format\":\"SQBCAT02\",\"ready\":%s,\"read_only\":%s,\"generation\":%u,"
        "\"tail\":%u,\"maintenance\":%u,\"warm_resumes\":%u,\"full_opens\":%u,"
        "\"work_steps\":%u,\"max_maintenance_slice_us\":%u,\"input_highwater\":%u,"
        "\"input_overflows\":%u,\"max_replay_delay_ms\":%u,\"personal_id\":%u,"
        "\"auto_checkpoints\":%u,\"auto_checkpoint_result\":%u,\"auto_reclaimed\":%u,"
        "\"dictionaries\":%u,\"ids\":[",
        healthy ? "true" : "false", store.selected.quarantine ? "true" : "false",
        unsigned(store.selected.root.generation), store.live.TailCount(), unsigned(store.work),
        warm_resumes, full_opens, maintenance_steps, maintenance_max_slice_us,
        steno_squid_native_input_highwater(), steno_squid_native_input_overflows(),
        steno_squid_native_input_max_delay_ms(), personal ? personal->identity : 0,
        automatic_checkpoints, unsigned(automatic_result), automatic_reclaimed, unsigned(cell_count));
    for(size_t i=0;i<cell_count;++i)Console::Printf("%s%u",i?",":"",cells[i].identity);
    Console::Printf("]}\n\n");
}
void store_verify(void *,const char *) {
    if(store.work!=NativeStore<FlashDevice>::Work::None){Console::Printf("ERR Native maintenance busy\n\n");return;}
    if(steno_squid_native_input_pending()){Console::Printf("ERR Input replay busy\n\n");return;}
    steno_squid_upload_status(STENO_SQUID_MODE_VERIFY, 180000);
    steno_squid_native_input_pause();
    flash_device.Invalidate();
    flash_device.mapped = false;
    const bool good = validator.Validate(store.selected.root) == CatalogResult::Ok;
    flash_device.mapped = true;
    if (good) scrub_due = k_uptime_get() + 24 * 60 * 60 * 1000LL;
    else base_provider.Fault();
    // This explicit scan is an exclusive operation. Cancel any complete or
    // partial steno chord entered during it before normal delivery resumes.
    steno_squid_native_input_resume_discard();
    steno_squid_upload_status(STENO_SQUID_MODE_VERIFY, 0);
    if (good) Console::SendOk();
    else Console::Printf("ERR Native integrity failure\n\n");
}
void store_rebuild(void *,const char *) {
    const auto result=store.Start(NativeStore<FlashDevice>::Work::Checkpoint);Kick();
    if(result==JobResult::More)Console::SendOk();else Console::Printf("ERR Native maintenance unavailable\n\n");
}
void reset_entries(void *,const char *) {
    auto *cell=ByName("user_dictionary");uint32_t replacement=0;
    if(!cell || !healthy || exporting || steno_squid_native_input_pending()){Console::Printf("ERR Native reset unavailable\n\n");return;}
    const NativeInputPause input_pause;
    flash_device.mapped=false;
    const auto result=store.Clear(cell->identity,replacement,export_view,[](uint32_t i,const uint8_t *row){return validator.Extent(i,row);});
    flash_device.mapped=true;
    if(result!=CatalogResult::Ok){if(!store.opened)base_provider.Fault();Console::Printf("ERR Native reset refused\n\n");return;}
    cell->identity=replacement;cell->Bind();if(parent)parent->OnLookupDataChanged();Console::SendOk();
}
bool Number(const char *&p,uint32_t &out);
NativeEntryStager entry_transfer;
alignas(4) uint8_t entry_chunk[512];
void native_entry(void *,const char *line) {
    constexpr char prefix[]="native_entry ";
    if(std::strncmp(line,prefix,sizeof(prefix)-1)){Console::Printf("ERR Missing entry operation\n\n");return;}
    const char *p=line+sizeof(prefix)-1;
    static uint32_t entry_owner;
    static int64_t entry_expires;
    static bool entry_committed;
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
    const auto session = steno_squid_javelin_bluetooth_session();
#else
    constexpr uint32_t session = 0;
#endif
    const auto now = k_uptime_get();
    bool disconnected = false;
#ifdef CONFIG_ZMK_JAVELIN_STENO_BLE_HID
    disconnected = entry_owner && !steno_squid_javelin_ble_session_valid(entry_owner);
#endif
    if (entry_expires && (now >= entry_expires || disconnected)) {
        entry_transfer.Abort();
        entry_expires = 0;
    }
    const bool starts_entry = !std::strncmp(p, "begin ", 6) || !std::strcmp(p, "abort");
    if (entry_expires && entry_owner != session && (!entry_committed || !starts_entry)) {
        Console::Printf("ERR Entry transfer belongs to another connection\n\n");
        return;
    }
    if (!std::strcmp(p, "abort")) {
        entry_transfer.Abort();
        entry_expires = 0;
        Console::SendOk();
        return;
    }
    if (entry_expires) entry_expires = now + 60000;
    if(!std::strncmp(p,"begin ",6)) {
        p+=6;uint32_t id,bytes;
        if(!Number(p,id) || *p++!=' ' || !Number(p,bytes) || bytes>2047 || *p++!=' ' || !ById(id)){Console::Printf("ERR Invalid native entry target\n\n");return;}
        StrokeListParser parser;
        if(!parser.Parse(p) || !parser.GetCount() || parser.GetCount()>16 || *parser.failureOrEnd){Console::Printf("ERR Invalid native outline\n\n");return;}
        uint32_t masks[16];for(unsigned i=0;i<parser.GetCount();++i)masks[i]=parser.GetData()[i].GetKeyState();
        const auto result = entry_transfer.Begin(id, masks, parser.GetCount(), bytes);
        if (result == LiveResult::Ok) {
            entry_owner = session;
            entry_committed = false;
            entry_expires = now + 60000;
        }
        ReplyEdit(result);
        return;
    }
    if(!std::strncmp(p,"data ",5)) {
        p+=5;uint32_t offset;size_t decoded=0;
        if(!Number(p,offset) || *p++!=' ' || base64_decode(entry_chunk,sizeof(entry_chunk),&decoded,reinterpret_cast<const uint8_t *>(p),std::strlen(p))){Console::Printf("ERR Invalid entry chunk\n\n");return;}
        ReplyEdit(entry_transfer.Write(offset,entry_chunk,decoded));return;
    }
    if(!std::strcmp(p,"commit")) {
        const auto result = entry_transfer.Commit([](uint32_t id,const uint32_t *masks,uint8_t count,const uint8_t *text,uint16_t bytes) {
            auto *cell=ById(id);if(!cell)return LiveResult::Invalid;
            StenoStroke strokes[16];for(unsigned i=0;i<count;++i)strokes[i]=StenoStroke(masks[i]);
            return Edit(*cell,0,strokes,count,reinterpret_cast<const char *>(text),bytes);
        });
        // Keep same-session retry semantics without blocking the other transport.
        if (result == LiveResult::Ok) entry_committed = true;
        ReplyEdit(result);
        return;
    }
    Console::Printf("ERR Unknown entry operation\n\n");
}
void native_edit_key(void *,const char *line) {
    const char *p=std::strchr(line,' ');uint32_t id;
    if(!p || !Number(++p,id) || *p++!=' '){Console::Printf("ERR Invalid dictionary ID\n\n");return;}
    auto *cell=ById(id);StrokeListParser parser;
    if(!cell || !parser.Parse(p) || !parser.GetCount() || *parser.failureOrEnd){Console::Printf("ERR Invalid native outline\n\n");return;}
    ReplyEdit(Edit(*cell,!std::strncmp(line,"native_reset_entry ",19)?2:1,parser.GetData(),parser.GetCount(),nullptr,0));
}

constexpr const char *TrustKey="native_store/bases";
uint32_t verified_fingerprint=0;
uint32_t last_open_pool_end=0;
bool trust_loaded=false;
int ReadNativeTrust(const char *,size_t bytes,settings_read_cb read,void *argument,void *output) {
    uint32_t value=0;if(bytes==sizeof(value) && read(argument,&value,sizeof(value))==sizeof(value))*static_cast<uint32_t *>(output)=value;
    return 0;
}
void LoadNativeTrust() {
    if(trust_loaded)return;
    trust_loaded=true;
    if(settings_load_subtree_direct(TrustKey,ReadNativeTrust,&verified_fingerprint))verified_fingerprint=0;
}
bool ForgetBaseTrust() {
    verified_fingerprint=0;trust_loaded=true;flash_device.Invalidate();
    return settings_delete(TrustKey)==0;
}
uint32_t BaseFingerprint(const CatalogRoot &root) {
    uint8_t prefix[32];if(!flash_device.Read(root.address,prefix,32))return 0;
    const uint32_t dictionaries=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8,extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
    if(dictionaries>64 || extents>253 || root.bytes!=32+dictionaries*96+extents*24)return 0;
    const uint8_t domain[]="SQBCAT02/native-bases";
    uint32_t crc=crc32_ieee(domain,sizeof(domain));
    if (flash_device.PoolEnd() == kLargePoolEnd) {
        const uint8_t geometry[] = {64};
        crc = crc32_ieee_update(crc, geometry, sizeof(geometry));
    }
    for(unsigned i=0;i<extents;++i) {
        uint8_t row[24];if(!flash_device.Read(root.address+32+dictionaries*96+i*24,row,24))return 0;
        if(Word(row+12)==1)crc=crc32_ieee_update(crc,row,24);
    }
    return crc?crc:0x4e415431u;
}
} // namespace

extern "C" void steno_squid_native_power_changed(void) {
    QueueIdleMaintenance();
}

extern "C" bool steno_squid_native_flash_open(void) {
    // Only a same-boot, previously healthy mapper can retain its validated
    // layout. Metadata must be read from DMA before trusting mapped pointers.
    flash_device.mapped=false;
    if(healthy && loaded && last_open_pool_end == flash_device.PoolEnd() && NativeWarmResumeEligible(store)) {
        flash_device.mapped=true;
        for(size_t i=0;i<cell_count;++i)if(!cells[i].Probe()){base_provider.Fault();return false;}
        NoteIdleMaintenance();
        ++warm_resumes;return true;
    }
    last_open_pool_end=flash_device.PoolEnd();
    healthy=false;snapshot_format_rejected=false;++full_opens;
    LoadNativeTrust();
    // QSPI is in mapped quad mode here. Compare both root headers against
    // DMA before using the mapping for the many small index validation reads.
    bool mapping_ok = true;
    for (uint32_t at = 0; at < 2 * kSegmentBytes; at += kSegmentBytes) {
        uint8_t raw[32], mapped[32];
        if (!flash_device.Read(at, raw, sizeof(raw))) {
            mapping_ok = false;
            break;
        }
        copy_xip_bytes(mapped, reinterpret_cast<const void *>(Xip + at), sizeof(mapped));
        if (std::memcmp(raw, mapped, sizeof(raw))) {
            mapping_ok = false;
            break;
        }
    }
    flash_device.mapped = mapping_ok;
    flash_device.cold_read_yield_at = k_uptime_get() + 5;
    const auto result=store.Open([](const CatalogRoot &root){
        const uint32_t fingerprint=BaseFingerprint(root);
        flash_device.trust_bases=fingerprint && fingerprint==verified_fingerprint;
        const auto status=validator.Validate(root, false);flash_device.trust_bases=false;return status;
    });
    flash_device.cold_read_yield_at = 0;
    flash_device.mapped = false;
    if(result!=CatalogResult::Ok && result!=CatalogResult::Quarantined){base_provider.Fault();return false;}
    uint8_t prefix[8];if(!flash_device.Read(store.selected.root.address,prefix,8))return false;
    const size_t count=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
    if(count>MaxDictionaries || (loaded && count!=cell_count))return false;
    for(size_t i=0;i<count;++i) {
        uint8_t row[96];if(!store.Row(i,row))return false;
        auto &cell=cells[i];const uint32_t id=Word(row);
        if(loaded && id!=cell.identity)return false;
        cell.identity=id;cell.user_snapshot=row[11]==kUserSnapshot;cell.factory_absent=row[11]==kFactoryAbsent;cell.enabled=Word(row+4);std::memcpy(cell.name,row+16,row[12]);cell.name[row[12]]=0;
        const uint16_t base=uint16_t(row[8]) | uint16_t(row[9])<<8;
        if(base!=kNoExtent) {
            uint8_t extent[24];
            if(!flash_device.Read(store.selected.root.address+32+count*96+base*24,extent,24))return false;
            if (!cell.LoadBase((uint16_t(extent[0]) | uint16_t(extent[1])<<8)*kSegmentBytes,
                               Word(extent+4),Word(extent+8),row[10])) {
                snapshot_format_rejected=cell.user_snapshot;
                store.opened=false;
                return false;
            }
        } else if(cell.base)return false;
        cell.Bind();
    }
    cell_count=count;loaded=true;flash_device.mapped=true;
    for(size_t i=0;i<count;++i)if(!cells[i].Probe()){base_provider.Fault();return false;}
    healthy=true;
    if(parent && !StenoSquidSyncNativeCollection(*parent,count,[](unsigned i)->const Cell &{return cells[i];})) {
        base_provider.Fault();return false;
    }
    const uint32_t fingerprint=BaseFingerprint(store.selected.root);
    NoteIdleMaintenance();
    if(!scrub_due)scrub_due=k_uptime_get()+(fingerprint && fingerprint==verified_fingerprint?60000:24*60*60*1000LL);
    if(fingerprint && fingerprint!=verified_fingerprint) {
        verified_fingerprint=fingerprint;
        if(settings_save_one(TrustKey,&fingerprint,sizeof(fingerprint)))printk("Native base trust could not be saved\n");
    }
    return true;
}
StenoDictionary *steno_squid_native_dictionary(size_t i){return healthy && i<cell_count?&cells[i]:nullptr;}
bool steno_squid_native_dictionary_ready(){return healthy && store.opened;}
size_t steno_squid_native_dictionary_count(){return healthy?cell_count:0;}
bool steno_squid_native_dictionary_enabled(size_t i){return i<cell_count && cells[i].enabled;}
bool steno_squid_native_select(const char *name,int operation,bool *enabled) {
    auto *cell=ByName(name);if(!healthy || !cell || cell->factory_absent || exporting || steno_squid_native_input_pending())return false;
    const NativeInputPause input_pause;
    const bool value=operation==0?true:operation==1?false:!cell->enabled;
    flash_device.mapped=false;
    const auto result=store.Enable(cell->identity,value,export_view,[](uint32_t i,const uint8_t *row){return validator.Extent(i,row);});
    flash_device.mapped=true;
    if(result!=CatalogResult::Ok){if(!store.opened)base_provider.Fault();return false;}
    cell->enabled=value;*enabled=value;return true;
}
void steno_squid_native_register(Console &console,StenoDictionary &root) {
    parent=&root;
#define UPSTREAM_COMMAND(name)
#define ENGINE_COMMAND(name, help, handler)
#define USER_COMMAND(name, help, handler, usb_only) console.RegisterCommand(name,help,handler,nullptr);
#include "console_commands.def"
#undef UPSTREAM_COMMAND
#undef ENGINE_COMMAND
#undef USER_COMMAND
}
StenoDictionary *lapwing_dictionary(){return healthy?ById(6):nullptr;}
StenoDictionary *plover_dictionary(){return healthy?ById(7):nullptr;}
static const char *const extra_names[]={"Lapwing Commands","Lapwing Numbers","Lapwing UK Additions","Lapwing Proper Nouns"};
const char *extras_dictionary_name(size_t index){return index<4 && ById(index+2)?ById(index+2)->name:index<4?extra_names[index]:nullptr;}
StenoDictionary *extras_dictionary(size_t index){return healthy && index<4?ById(index+2):nullptr;}
StenoDictionary *steno_squid_user_dictionary_open(){return healthy?ByName("user_dictionary"):nullptr;}
void steno_squid_user_dictionary_register(Console &console,StenoDictionary &root){steno_squid_native_register(console,root);}
StenoDictionaryLookupResult lapwing_copy_lookup_text(const uint8_t *text) {
    const auto address=reinterpret_cast<uintptr_t>(text);
    for(size_t i=0;i<cell_count;++i) {
        const auto &cell=cells[i];const uintptr_t begin=Xip+cell.base_offset,end=begin+cell.base_bytes;
        if(!cell.base || address<begin || address>=end)continue;
        size_t bytes=0;
        while(bytes<2048 && address+bytes<end && xip_load_byte(text+bytes))++bytes;
        if(bytes==2048 || address+bytes==end)return StenoDictionaryLookupResult::CreateInvalid();
        char *copy=static_cast<char *>(std::malloc(bytes+1));if(!copy)return StenoDictionaryLookupResult::CreateInvalid();
        copy_xip_bytes(copy,text,bytes);copy[bytes]=0;return StenoDictionaryLookupResult::CreateDynamicString(copy);
    }
    return StenoDictionaryLookupResult::CreateInvalid();
}

namespace {
NativeProvisioner<FlashDevice> transfer(flash_device);
NativeStage<FlashDevice> staged(store, validator);
enum steno_squid_mode stage_mode = STENO_SQUID_MODE_IMPORT;
alignas(4) uint8_t incoming[512];
void reboot_worker(k_work *){sys_reboot(SYS_REBOOT_COLD);}
K_WORK_DELAYABLE_DEFINE(reboot_work,reboot_worker);
int64_t install_deadline=0;
void install_timeout(k_work *);
K_WORK_DELAYABLE_DEFINE(install_timeout_work,install_timeout);
void TouchInstall(){install_deadline=k_uptime_get()+60000;k_work_reschedule_for_queue(&steno_squid_engine_queue,&install_timeout_work,K_SECONDS(60));}
void install_timeout(k_work *) {
    k_mutex_lock(&flash_lock,K_FOREVER);
    if((transfer.Active() || staged.Active()) && k_uptime_get()>=install_deadline) {
        transfer.Abort();staged.Abort();dictionary_flash_read_session(false);steno_squid_upload_status(STENO_SQUID_MODE_UNKNOWN,0);
    } else if(transfer.Active() || staged.Active())TouchInstall();
    k_mutex_unlock(&flash_lock);
}

bool Number(const char *&p,uint32_t &out) {
    if(*p<'0' || *p>'9')return false;
    out=0;
    while(*p>='0' && *p<='9'){uint32_t digit=*p++-'0';if(out>(UINT32_MAX-digit)/10)return false;out=out*10+digit;}
    return true;
}
bool End(const char *p){return !*p || (*p=='\n' && !p[1]);}
bool ProbeStagedBase(const uint8_t *extent, const uint8_t *row) {
    Cell probe;
    probe.user_snapshot = row[11] == kUserSnapshot;
    std::memcpy(probe.name, row + 16, row[12]);
    const uint32_t start = uint16_t(extent[0]) | uint16_t(extent[1]) << 8;
    if (!probe.LoadBase(start * kSegmentBytes, Word(extent + 4), Word(extent + 8), row[10]))
        return false;
    for (unsigned i = 0; i < probe.definition.maximumOutlineLength; ++i) {
        const auto &group = probe.strokes[i];
        if (!group.hashMapMask) continue;
        const auto data = reinterpret_cast<uintptr_t>(group.data);
        const auto hash = reinterpret_cast<uintptr_t>(group.offsets);
        if (hash < data || probe.GroupEntries(group) > (hash - data) / (3 * (i + 2))) return false;
    }
    return true;
}
const char *LayoutMap(char *reply, size_t bytes, unsigned first) {
    if (lf_suspended || !healthy || !store.opened || exporting || transfer.Active() || staged.Active() ||
        store.work != NativeStore<FlashDevice>::Work::None)
        return "ERR Layout scan deferred until flash is available";
    const auto deadline = k_uptime_get() + 25;
    const auto stop = [&] { return k_uptime_get() >= deadline; };
    const bool writable = store.catalog.CanWrite(store.selected.root);
    const char access = !writable ? 'r' : store.catalog.DestinationPinned(store.selected) ? 'p' : 'w';
    const unsigned end = flash_device.PoolEnd();
    if (first % 256 || first > end) return "ERR Invalid layout page";
    const int prefix = end == kSmallPoolEnd
        ? snprintk(reply, bytes, "OK MAP1 %u %c -- ", unsigned(store.selected.slot), access)
        : snprintk(reply, bytes, "OK MAP2 %u %u %llu %llu %u %c -- ", end + 1, first,
                   (unsigned long long)store.catalog.TrustedRoot(0).generation,
                   (unsigned long long)store.catalog.TrustedRoot(1).generation,
                   unsigned(store.selected.slot), access);
    if (prefix < 3 || size_t(prefix) + 514 > bytes) return "ERR Layout reply too small";
    for (unsigned slot = 0; slot < 2; ++slot) {
        char *map = reply + prefix + slot * 257;
        const auto root = store.catalog.TrustedRoot(slot);
        char state = 'e';
        if (root.generation) {
            state = ReadLayout(flash_device, root, map, stop, first) ? 'v' : '?';
            if (stop()) return "ERR Layout scan timed out";
        } else if (!writable) {
            state = '?';
        }
        if (state != 'v') {
            std::memset(map, state == 'e' ? '.' : '?', 256);
            if (!first) map[0] = map[1] = 'c';
            if (end - first < 256) map[end - first] = 'x';
        }
        reply[prefix - 3 + slot] = state;
        map[256] = slot == 0 ? ' ' : 0;
    }
    return nullptr;
}
const char *Update(const char *command, char *reply, size_t bytes) {
    if (!std::strcmp(command, "capabilities\n")) return "OK BULK1";
    if (!std::strcmp(command, "named-capabilities\n")) {
        snprintk(reply, bytes, "OK NAMED1 %u", unsigned(MaxDictionaries));
        return nullptr;
    }
    if (!std::strcmp(command, "manage-capabilities\n")) return "OK MANAGE1";
    if (!std::strcmp(command, "snapshot-capabilities\n"))
        return "OK SNAPSHOT2";
    if (!std::strcmp(command, "snapshot-merge-capabilities\n")) {
        snprintk(reply, bytes, "OK MERGE1 %u", unsigned(MaxDictionaries));
        return nullptr;
    }
    if (!std::strcmp(command, "personal-snapshot-capabilities\n"))
        return "OK PERSONAL1";
    if (!std::strcmp(command, "journal-snapshot-capabilities\n"))
        return "OK JOURNAL1";
    if (!std::strcmp(command, "snapshot-reset-capabilities\n"))
        return "OK RESET1";
    if (!std::strcmp(command, "snapshot-clear-capabilities\n"))
        return "OK CLEAR1";
    if (!std::strcmp(command, "recovery-capabilities\n"))
        return "OK RECOVERY1";
#ifdef CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS
    if (!std::strcmp(command, "repack-capabilities\n"))
        return "OK REPACK1";
#endif
    if (!std::strcmp(command, "layout-capabilities\n")) return "OK MAP1";
    if (!std::strcmp(command, "journal-usage-capabilities\n")) return "OK USAGE2";
    if (!std::strcmp(command, "layout-map\n")) return LayoutMap(reply, bytes, 0);
    if (!std::strncmp(command, "layout-map ", 11)) {
        const char *p = command + 11;
        uint32_t first;
        if (!Number(p, first) || !End(p)) return "ERR Invalid layout page";
        return LayoutMap(reply, bytes, first);
    }
    if (!std::strcmp(command, "base-capabilities\n")) return "OK BASE1";
    if (!std::strcmp(command, "factory-member-capabilities\n")) return "OK FACTORY2";
    NativeUpdateKind kind = NativeUpdateKind::Compact;
    uint32_t snapshot_id = 0, lower_id = 0;
    const bool metadata_read = !std::strcmp(command, "begin metadata\n");
    if (!std::strcmp(command, "begin import\n")) kind = NativeUpdateKind::Personal;
    else if (!std::strcmp(command, "begin named\n")) kind = NativeUpdateKind::Named;
    else if (!std::strcmp(command, "begin snapshot-named\n"))
        kind = NativeUpdateKind::SnapshotNamed;
    else if (!std::strncmp(command, "begin snapshot-", 15)) {
        const char *p = command + 15;
        if (!std::strncmp(p, "consolidate ", 12)) {
            p += 12;
            kind = NativeUpdateKind::SnapshotConsolidate;
        } else if (!std::strncmp(p, "merge ", 6)) {
            p += 6;
            kind = NativeUpdateKind::SnapshotMerge;
        } else if (!std::strncmp(p, "delete ", 7)) {
            p += 7;
            kind = NativeUpdateKind::SnapshotDelete;
        } else if (!std::strncmp(p, "personal ", 9)) {
            p += 9;
            kind = NativeUpdateKind::SnapshotPersonal;
        } else if (!std::strncmp(p, "convert ", 8)) {
            p += 8;
            kind = NativeUpdateKind::SnapshotPersonal;
        } else if (!std::strncmp(p, "clear ", 6)) {
            p += 6;
            kind = NativeUpdateKind::SnapshotClear;
        } else return "ERR Invalid snapshot action";
        if (!Number(p, snapshot_id) || !snapshot_id)
            return "ERR Invalid snapshot dictionary ID";
        if (kind == NativeUpdateKind::SnapshotMerge && (*p++ != ' ' || !Number(p, lower_id) || !lower_id))
            return "ERR Invalid second snapshot dictionary ID";
        if (!End(p)) return "ERR Invalid snapshot dictionary ID";
    }
    else if (!std::strcmp(command, "begin reorder\n")) kind = NativeUpdateKind::Reorder;
    else if (!std::strcmp(command, "begin rename\n")) kind = NativeUpdateKind::Rename;
    else if (!std::strcmp(command, "begin delete\n")) kind = NativeUpdateKind::Delete;
    else if (metadata_read || !std::strcmp(command, "begin backup\n")) kind = NativeUpdateKind::Backup;
    else if (!std::strcmp(command, "begin base\n")) kind = NativeUpdateKind::Base;
    else if (!std::strncmp(command, "begin factory-member ", 21)) {
        const char *p = command + 21;
        if (!Number(p, snapshot_id) || !End(p) || !FactoryName(snapshot_id))
            return "ERR Invalid factory dictionary";
        kind = NativeUpdateKind::FactoryMember;
    }
    if (kind != NativeUpdateKind::Compact || !std::strcmp(command, "begin compact\n")) {
        if (!healthy || exporting || transfer.Active() || !staged.Begin(kind, MaxDictionaries, snapshot_id, lower_id))
            return "ERR Native store cannot stage an update";
        dictionary_flash_read_session(true);
        TouchInstall();
        stage_mode = kind == NativeUpdateKind::Backup ? STENO_SQUID_MODE_UNKNOWN : (kind == NativeUpdateKind::Personal || kind == NativeUpdateKind::Named || kind == NativeUpdateKind::Base || kind == NativeUpdateKind::SnapshotNamed)
                         ? STENO_SQUID_MODE_IMPORT : STENO_SQUID_MODE_SAVING;
        if (!metadata_read) stage_mode = STENO_SQUID_MODE_BUSY;
        steno_squid_upload_status(stage_mode, 60000);
        const auto &root = staged.Root();
        int length = snprintk(reply, bytes, "OK %u %u %u ", root.address, root.bytes, root.crc);
        for (unsigned i = 0; i <= flash_device.PoolEnd()/32; ++i)
            length += snprintk(reply + length, bytes - length, "%08x", staged.Protected().words[i]);
        return nullptr;
    }
    if (!staged.Active()) return "ERR No native update active";
    TouchInstall();
    steno_squid_upload_status(stage_mode, 60000);
    if (!std::strcmp(command, "abort\n")) {
        staged.Abort();
        dictionary_flash_read_session(false);
        k_work_cancel_delayable(&install_timeout_work);
        steno_squid_upload_status(stage_mode, 0);
        return "OK";
    }
    if (!std::strncmp(command, "journal-usage ", 14)) {
        const char *p = command + 14;
        uint32_t position;
        if (staged.Kind() != NativeUpdateKind::Backup || !Number(p, position) || !End(p))
            return "ERR Invalid journal measurement";
        const auto &view = store.live.CurrentView();
        struct BlockUsage {
            unsigned slot, segment;
            OverlayUsage usage;
        };
        constexpr unsigned max_blocks = 16;
        BlockUsage blocks[max_blocks];
        unsigned block_count = 0;
        if (view.dictionary_count > MaxDictionaries) return "ERR Invalid journal view";
        const int64_t deadline = k_uptime_get() + 2;
        const auto collect = [&](unsigned slot, unsigned segment, unsigned operation, unsigned span) {
            unsigned i = 0;
            for (; i < block_count; ++i)
                if (blocks[i].slot == slot && blocks[i].segment == segment) break;
            if (i == block_count) {
                if (block_count == max_blocks) return false;
                blocks[block_count++] = {slot, segment, {}};
            }
            auto &usage = blocks[i].usage;
            ++usage.count;
            usage.entries += operation == 0;
            usage.bytes += span;
            return true;
        };
        if (!MeasureOverlay(flash_device, view, position, collect, [&] { return k_uptime_get() >= deadline; }))
            return "ERR Journal measurement failed";
        int length = snprintk(reply, bytes, "OK USAGE2 %u %u", unsigned(position),
                              unsigned(view.forward_count + view.tail.count));
        for (unsigned i = 0; i < block_count && length >= 0 && size_t(length) < bytes; ++i) {
            const auto &block = blocks[i];
            const auto &row = block.usage;
            length += snprintk(reply + length, bytes - length, " %u,%u,%u,%u,%u", block.segment,
                               unsigned(view.identities[block.slot]), unsigned(row.count),
                               unsigned(row.entries), unsigned(row.bytes));
        }
        if (length < 0 || size_t(length) >= bytes) return "ERR Journal measurement reply too small";
        return nullptr;
    }
    if (!std::strncmp(command, "read ", 5)) {
        const char *p = command + 5;
        uint32_t address, size;
        size_t encoded = 0;
        if (!Number(p, address) || *p++ != ' ' || !Number(p, size) || !End(p) ||
            !staged.Read(address, incoming, size) ||
            base64_encode(reinterpret_cast<uint8_t *>(reply + 3), bytes - 3, &encoded, incoming, size))
            return "ERR Native update read refused";
        std::memcpy(reply, "OK ", 3);
        reply[3 + encoded] = 0;
        return nullptr;
    }
    const NativeInputPause input_pause(true);
    flash_device.mapped = false;
    const char *result = "ERR Invalid native update command";
    bool reboot = false;
    if (!std::strncmp(command, "erase ", 6)) {
        const char *p = command + 6;
        uint32_t segment;
        if (Number(p, segment) && End(p)) {
            if (stage_mode == STENO_SQUID_MODE_BUSY && staged.Kind() != NativeUpdateKind::Backup) {
                const auto operation = staged.Kind();
                stage_mode = operation == NativeUpdateKind::Personal || operation == NativeUpdateKind::Named ||
                             operation == NativeUpdateKind::Base || operation == NativeUpdateKind::SnapshotNamed
                    ? STENO_SQUID_MODE_IMPORT : STENO_SQUID_MODE_SAVING;
                steno_squid_upload_status(stage_mode, 60000);
            }
            result = staged.Erase(segment) ? "OK" : "ERR Native staging erase refused";
        }
    } else if (!std::strncmp(command, "write ", 6)) {
        const char *p = command + 6;
        uint32_t address;
        if (Number(p, address) && *p++ == ' ') {
            const size_t encoded = std::strcspn(p, "\n");
            size_t size = 0;
            if (End(p + encoded) && !base64_decode(incoming, sizeof(incoming), &size,
                                                  reinterpret_cast<const uint8_t *>(p), encoded))
                result = staged.Write(address, incoming, size) ? "OK" : "ERR Native staging write refused";
        }
    } else if (!std::strncmp(command, "commit ", 7)) {
        const char *p = command + 7;
        uint32_t segment, size, crc;
        if (Number(p, segment) && *p++ == ' ' && Number(p, size) && *p++ == ' ' &&
            Number(p, crc) && End(p)) {
            const auto status = staged.Commit(segment, size, crc, reboot, ProbeStagedBase);
            if (status == CatalogResult::Ok) result = "OK Rebooting";
            else if (status == CatalogResult::Quarantined) result = "ERR Update committed; catalog mirror requires recovery";
            else result = "ERR Native update commit refused";
        }
    }
    if (reboot) {
        staged.Abort();
        dictionary_flash_read_session(false);
        k_work_cancel_delayable(&install_timeout_work);
        healthy = store.opened = false;
        atomic_clear(&lf_mapped_ready);
        k_work_reschedule(&reboot_work, K_SECONDS(2));
    } else {
        flash_device.mapped = true;
    }
    return result;
}
const char *Install(const char *command,char *reply,size_t bytes) {
    if (!std::strncmp(command, "update ", 7)) return Update(command + 7, reply, bytes);
    if (!std::strcmp(command, "geometry\n")) {
        if (!flash_device.PoolEnd()) return "ERR Unsupported external flash";
        snprintk(reply, bytes, "OK POOL1 %u", unsigned(flash_device.PoolEnd()));
        return nullptr;
    }
    bool explicit_large = false;
    if (!std::strncmp(command, "large ", 6)) {
        const char *p = command + 6;
        uint32_t pool_end;
        if (!Number(p, pool_end) || *p++ != ' ' || pool_end != kLargePoolEnd ||
            pool_end != flash_device.PoolEnd() || std::strncmp(p, "begin ", 6))
            return "ERR Native pool geometry does not match";
        explicit_large = true;
        command = p;
    }
    if(!std::strcmp(command,"status\n")) {
        const bool installing=transfer.Active();
        const char *detail = snapshot_format_rejected && !installing ?
            " Incompatible compact snapshot. Reprovision a disposable pool with --install --confirm-erase." : "";
        snprintk(reply,bytes,"NATIVE2 %s %u%s",healthy?"READY":installing?"INSTALL":"UNPROVISIONED",
                 unsigned(store.selected.root.generation),detail);
        return nullptr;
    }
    if (!std::strncmp(command, "match RECOVERY ", 15)) {
        const char *p = command + 15;
        uint32_t expected, actual;
        if (!Number(p, expected) || !End(p)) return "ERR Invalid recovery CRC";
        if (!healthy || lf_suspended || transfer.Active() || staged.Active() || exporting ||
            store.work != NativeStore<FlashDevice>::Work::None)
            return "ERR Recovery verification unavailable";
        const NativeInputPause pause(true);
        const bool mapped = flash_device.mapped;
        flash_device.mapped = false;
        const bool match = flash_device.Checksum(0, flash_device.Limit(), actual) && actual == expected;
        flash_device.mapped = mapped;
        return match ? "OK MATCH" : "ERR Recovery contents differ";
    }
    const bool ordinary_recovery = !std::strncmp(command,"begin RECOVERY ",15);
    const bool guarded_recovery = !std::strncmp(command,"begin REPACK ",13);
    const bool recovering = ordinary_recovery || guarded_recovery;
    if (recovering || !std::strncmp(command,"begin CLEAR ",12)) {
        if (flash_device.PoolEnd() == kLargePoolEnd && !explicit_large)
            return "ERR 64 MiB pool requires a matching installer";
        const char *p = command + (ordinary_recovery ? 15 : guarded_recovery ? 13 : 12);
        uint32_t crc, final_crc = 0;
        if (!Number(p, crc) || (recovering && (*p++ != ' ' || !Number(p, final_crc))))
            return "ERR Invalid manifest CRC";
        if (transfer.Active() || staged.Active() || exporting ||
            store.work != NativeStore<FlashDevice>::Work::None) return "ERR Native store busy";
        if (guarded_recovery) {
#ifdef CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS
            uint32_t generation_low, generation_high, catalog_crc, sequence_low, sequence_high, layout;
            if (*p++ != ' ' || !Number(p, generation_low) ||
                *p++ != ' ' || !Number(p, generation_high) ||
                *p++ != ' ' || !Number(p, catalog_crc) ||
                *p++ != ' ' || !Number(p, sequence_low) ||
                *p++ != ' ' || !Number(p, sequence_high) ||
                *p++ != ' ' || !Number(p, layout) || !End(p) || layout > 1)
                return "ERR Invalid repack source identity";
            // Called under flash_lock, the same lock that protects edits,
            // catalog publication and host layout changes. BeginRecovery below
            // closes Native access before releasing it.
            const uint64_t generation = (uint64_t(generation_high) << 32) | generation_low;
            const uint64_t sequence = (uint64_t(sequence_high) << 32) | sequence_low;
            if (!healthy || !store.opened || store.selected.quarantine ||
                store.selected.root.generation != generation || store.selected.root.crc != catalog_crc ||
                store.live.Highwater() != sequence || steno_squid_host_layout_index() != layout)
                return "ERR Repack source changed or unavailable; no erase started";
#else
            return "ERR Guarded repack unavailable";
#endif
        } else if (!End(p)) {
            return "ERR Invalid manifest CRC";
        }
        if(lf_suspended || !lf_provisioning_ready || !squid_qspi_reactivate())return "ERR Flash unavailable";
        if(!ForgetBaseTrust())return "ERR Cannot invalidate native base trust; no flash changed";
        healthy=store.opened=false;flash_device.mapped=false;flash_device.Invalidate();store.catalog.Invalidate();atomic_clear(&lf_mapped_ready);
        const auto result = recovering ? transfer.BeginRecovery(crc, final_crc) : transfer.Begin(crc);
        if(result!=ProvisionResult::Ok)return "ERR Native installer busy";
        dictionary_flash_read_session(true);TouchInstall();
        steno_squid_upload_status(STENO_SQUID_MODE_INSTALL,30000);return "OK";
    }
    if(!transfer.Active())return !std::strcmp(command,"finish\n") && transfer.Finish()==ProvisionResult::Ok?"OK Rebooting":"ERR No native installation active";
    TouchInstall();
    if(!std::strcmp(command,"abort\n")) {
        transfer.Abort();dictionary_flash_read_session(false);steno_squid_upload_status(STENO_SQUID_MODE_UNKNOWN,0);
        return "OK";
    }
    if(!std::strncmp(command,"erase ",6)) {
        const char *p=command+6;uint32_t segment;
        if(!Number(p,segment) || segment>=flash_device.PoolEnd() || !End(p))return "ERR Invalid native segment";
        if(transfer.Erase(segment)!=ProvisionResult::Ok)return "ERR Native erase failed";
        steno_squid_upload_status(STENO_SQUID_MODE_INSTALL,30000);return "OK";
    }
    if(!std::strncmp(command,"write ",6)) {
        const char *p=command+6;uint32_t at;size_t decoded=0;
        if(!Number(p,at) || *p++!=' ' || at%4 || at>=flash_device.Limit())return "ERR Invalid native write";
        const size_t encoded=std::strcspn(p,"\n");
        if(!End(p+encoded) || base64_decode(incoming,sizeof(incoming),&decoded,reinterpret_cast<const uint8_t *>(p),encoded) ||
           !decoded || decoded%4 || decoded>flash_device.Limit()-at)return "ERR Invalid native data";
        if(transfer.Write(at,incoming,decoded)!=ProvisionResult::Ok)return "ERR Native write or readback refused";
        steno_squid_upload_status(STENO_SQUID_MODE_INSTALL,30000);return "OK";
    }
    if(!std::strcmp(command,"finish\n")) {
        if(transfer.Finish()!=ProvisionResult::Ok)return "ERR Native image verification failed";
        dictionary_flash_read_session(false);k_work_cancel_delayable(&install_timeout_work);
        k_work_reschedule(&reboot_work,K_SECONDS(2));return "OK Rebooting";
    }
    return "ERR Unknown native installation command";
}
}
extern "C" void steno_squid_native_install_command(const char *command,char *reply,size_t bytes,uint32_t bluetooth_session) {
    if (!std::strcmp(command, "connection\n")) {
        snprintk(reply, bytes, "%s", bluetooth_session ? "OK BLUETOOTH" : "OK USB");
        return;
    }
    if (!std::strcmp(command, "identity\n")) {
        // Format the same hardware ID as Zephyr's USB serial descriptor,
        // so reconnecting wirelessly never depends on the OS's HID path.
        constexpr size_t serial_bytes = sizeof(CONFIG_USB_DEVICE_SN) / 2;
        uint8_t hardware_id[MAX(16, serial_bytes)]{};
        const auto length = hwinfo_get_device_id(hardware_id, sizeof(hardware_id));
        if (length <= 0) {
            snprintk(reply, bytes, "ERR Keyboard identity unavailable");
            return;
        }
        const size_t start = size_t(length) > serial_bytes ? size_t(length) - serial_bytes : 0;
        constexpr char digits[] = "0123456789ABCDEF";
        char serial[2 * serial_bytes + 1]{};
        for (size_t i = 0; i < serial_bytes; ++i) {
            const auto byte = hardware_id[start + i];
            serial[2 * i] = digits[byte >> 4];
            serial[2 * i + 1] = digits[byte & 15];
        }
        snprintk(reply, bytes, "OK SQMANAGER1 %s", serial);
        return;
    }
    if (!dictionary_flash_try_service()) {
        snprintk(reply, bytes, "%s", !std::strncmp(command, "update layout-map", 17)
                ? "ERR Layout scan deferred until flash is available" : "ERR Native flash busy");
        return;
    }
    static uint32_t owner_session;
    const bool was_active = transfer.Active() || staged.Active();
    const char *result;
    if (was_active && owner_session != bluetooth_session && std::strcmp(command, "status\n")) {
        result = "ERR Native transfer belongs to another connection";
    } else {
        result = Install(command, reply, bytes);
        if (!was_active && (transfer.Active() || staged.Active())) owner_session = bluetooth_session;
    }
    if (result) snprintk(reply, bytes, "%s", result);
    k_mutex_unlock(&flash_lock);
}

namespace {
int64_t scrub_due=0;size_t scrub_cell=0;uint32_t scrub_offset=0,scrub_crc=0;
alignas(4) uint8_t scrub_chunk[256];
}
extern "C" int32_t steno_squid_native_scrub_wait(void) {
    if(!healthy || !dictionary_flash_mapped_address() || lf_suspended)return -1;
    if(!scrub_due)scrub_due=k_uptime_get()+24*60*60*1000LL;
    const int64_t remaining=scrub_due-k_uptime_get();return remaining>0?int32_t(remaining):0;
}
extern "C" void steno_squid_native_scrub_step(void) {
    if(steno_squid_native_scrub_wait()!=0)return;
    if (!scrub_offset) {
        while (scrub_cell < cell_count) {
            const auto &candidate = cells[scrub_cell];
            bool seen = false;
            for (size_t previous = 0; previous < scrub_cell; ++previous) {
                if (cells[previous].base && cells[previous].base_offset == candidate.base_offset)
                    seen = true;
            }
            if (candidate.base && !seen) break;
            ++scrub_cell;
        }
    }
    if(scrub_cell==cell_count){scrub_cell=0;scrub_due=k_uptime_get()+24*60*60*1000LL;return;}
    const auto &cell=cells[scrub_cell];uint32_t n=cell.base_bytes-scrub_offset;if(n>sizeof(scrub_chunk))n=sizeof(scrub_chunk);
    if(!flash_device.Read(cell.base_offset+scrub_offset,scrub_chunk,n)){base_provider.Fault();return;}
    scrub_crc=crc32_ieee_update(scrub_crc,scrub_chunk,n);scrub_offset+=n;
    if(scrub_offset==cell.base_bytes) {
        if(scrub_crc!=cell.base_crc){base_provider.Fault();return;}
        ++scrub_cell;scrub_offset=scrub_crc=0;
    }
    scrub_due=k_uptime_get()+8;
}
