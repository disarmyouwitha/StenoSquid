// Opt-in full-engine integration for Candidate B. Normal firmware is unchanged.
#include "native_store_runtime.h"
#include "native_dictionary_adapter.h"
#include "native_engine_backend.h"
#include "../../tools/storage/native_store.h"
#include "../../tools/storage/native_validator.h"
#include "../../tools/storage/native_provisioner.h"
#include "../../tools/storage/native_entry_stager.h"
#include "../../tools/storage/reverse_cursor.h"
#include "dictionary/compact_map_dictionary.h"
#include "dictionary/dictionary_definition.h"
#include "lapwing_dictionary.h"
#include "user_dictionary_store.h"
#include "dictionary_flash.h"
#include "javelin_engine_lock.h"
#include "engine_work.h"
#include "engine_stream.h"
#include "hint_index.h"
#include "stroke_list_parser.h"
#include "split_status.h"
#include "w25q_quad.h"
#include "xip_load.h"
#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/base64.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/settings/settings.h>
#include <new>
#include <cstdlib>
#include <cstring>

extern "C" {
extern struct k_mutex flash_lock;
extern atomic_t lf_mapped_ready,lf_checked;
extern bool lf_suspended,lf_provisioning_ready;
}
namespace {
using namespace candidate_b;
constexpr uintptr_t Xip=DT_REG_ADDR_BY_NAME(DT_NODELABEL(qspi),qspi_mm);
constexpr uint32_t Limit=255*kSegmentBytes;
bool ForgetBaseTrust();
extern int64_t scrub_due;
constexpr size_t MaxDictionaries=CONFIG_ZMK_STENO_SQUID_NATIVE_DICTIONARIES;
struct FlashDevice {
    bool mapped=false,trust_bases=false;
    alignas(4) uint8_t dma[256];
    struct Cached {uint32_t at=0,bytes=0,crc=0;} cached[32];
    unsigned cache_next=0;
    void Invalidate(){for(auto &c:cached)c.bytes=0;}
    void Dirty(uint32_t at,uint32_t n){for(auto &c:cached)if(c.bytes && at<c.at+c.bytes && c.at<at+n)c.bytes=0;}
    bool Read(uint32_t at,uint8_t *out,uint32_t n) {
        if(at>0x1000000 || n>0x1000000-at)return false;
        if(mapped){copy_xip_bytes(out,reinterpret_cast<const void *>(Xip+at),n);return true;}
        while(n) {
            uint32_t skip=at%4,count=n<256-skip?n:256-skip;
            if(nrfx_qspi_read(dma,(skip+count+3)&~3u,at-skip)!=NRFX_SUCCESS)return false;
            std::memmove(out,dma+skip,count);at+=count;out+=count;n-=count;
        }
        return true;
    }
    bool Program(uint32_t at,const uint8_t *data,uint32_t n) {
        if(mapped || !n || n>256 || at%4 || at>=Limit || n>Limit-at || at/256!=(at+n-1)/256)return false;
        Dirty(at,(n+3)&~3u);std::memset(dma,255,256);std::memcpy(dma,data,n);
        return nrfx_qspi_write(dma,(n+3)&~3u,at)==NRFX_SUCCESS && squid_qspi_wait_ready(2000)==NRFX_SUCCESS;
    }
    bool Erase(uint32_t segment) {
        if(mapped || segment<2 || segment>=255)return false;
        Dirty(segment*kSegmentBytes,kSegmentBytes);
        return nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_64KB,segment*kSegmentBytes)==NRFX_SUCCESS && squid_qspi_wait_ready(4000)==NRFX_SUCCESS;
    }
    bool EraseCatalog(uint32_t at,uint32_t bytes) {
        if(mapped || (at && at!=kSegmentBytes) || bytes!=kSegmentBytes)return false;
        Dirty(at,bytes);
        return nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_64KB,at)==NRFX_SUCCESS && squid_qspi_wait_ready(4000)==NRFX_SUCCESS;
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
    StenoDictionaryLookupResult Lookup(uint32_t,const StenoDictionaryLookup &)override;
} base_provider;
NativeEngineBackend<FlashDevice> backend(store,base_provider);
class Cell final : public StenoDictionary {
public:
    uint32_t identity=0,base_offset=0,base_bytes=0,base_crc=0;
    bool enabled=false;
    char name[45]{};
    StenoCompactMapDictionaryDefinition definition{};
    StenoCompactMapDictionaryStrokesDefinition strokes[16]{};
    StenoCompactMapDictionary *base=nullptr;
    alignas(NativeOverlayDictionary) uint8_t overlay_storage[sizeof(NativeOverlayDictionary)];
    alignas(StenoSquidHintIndex) uint8_t hint_storage[sizeof(StenoSquidHintIndex)];
    StenoSquidHintIndex *hint=nullptr;
    Cell():StenoDictionary(16){}
    bool Probe()const;
    void Bind(){new (overlay_storage) NativeOverlayDictionary(backend,identity,name);}
    const char *GetName()const override{return name;}
    StenoDictionaryLookupResult Lookup(const StenoDictionaryLookup &lookup)const override {
        return reinterpret_cast<const NativeOverlayDictionary *>(overlay_storage)->Lookup(lookup);
    }
    bool CanRemove()const override{return true;}
    bool Remove(const char *label,const StenoStroke *outline,size_t count)override;
    void ReverseLookup(StenoReverseDictionaryLookup &lookup)const override;
    void PrintDictionary(PrintDictionaryContext &context)const override;
    void PrintEntriesWithPrefix(PrintPrefixContext &context)const override;
    void PrintEntriesWithPartialOutline(PrintPartialOutlineContext &context)const override;
    bool LoadBase(uint32_t at,uint32_t bytes,uint32_t crc) {
        if(base)return base_offset==at && base_bytes==bytes && base_crc==crc;
        base_offset=at;base_bytes=bytes;base_crc=crc;
        const uintptr_t address=Xip+at;
        auto in=[&](uintptr_t p,size_t n){return p>=address && p-address<=bytes && n<=bytes-(p-address);};
        const auto *collection=reinterpret_cast<const uint8_t *>(address);
        if(bytes<20 || xip_load_word(collection)!=STENO_MAP_DICTIONARY_COLLECTION_MAGIC ||
           xip_load_word(collection+offsetof(StenoDictionaryCollection,dictionaryCount))!=1)return false;
        const uintptr_t map=xip_load_word(collection+offsetof(StenoDictionaryCollection,dictionaries));
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
        definition.strokes=strokes;definition.name=name;
        base=new (definition) StenoCompactMapDictionary(definition);
        if(!base)return false;
        const uint32_t text_bytes=xip_load_word(collection+offsetof(StenoDictionaryCollection,textBlock));
        hint=new (hint_storage) StenoSquidHintIndex(address,bytes,0,1,reinterpret_cast<uintptr_t>(definition.textBlock),text_bytes,xip_load_word,xip_load_byte);
        return true;
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
StenoDictionaryLookupResult BaseProvider::Lookup(uint32_t id,const StenoDictionaryLookup &lookup) {
    const auto *cell=ById(id);
    return cell && cell->base?cell->base->Lookup(lookup):StenoDictionaryLookupResult::CreateInvalid();
}
void StepWork(k_work *);
K_WORK_DELAYABLE_DEFINE(maintenance_work,StepWork);
void Kick() {
    if(store.work==NativeStore<FlashDevice>::Work::None)return;
    if(!maintenance_hold){dictionary_flash_read_session(true);maintenance_hold=true;}
    k_work_schedule_for_queue(&steno_squid_engine_queue,&maintenance_work,K_MSEC(8));
}
void StepWork(k_work *) {
    const JavelinEngineLock lock;
    if(!lock){
        if(!healthy){if(maintenance_hold){dictionary_flash_read_session(false);maintenance_hold=false;}return;}
        Kick();return;
    }
    flash_device.mapped=false;
    const auto result=store.Step([](uint32_t i,const uint8_t *row){return validator.Extent(i,row);});
    flash_device.mapped=true;
    if(result==JobResult::More){Kick();return;}
    if(!store.opened)base_provider.Fault();
    if(maintenance_hold){dictionary_flash_read_session(false);maintenance_hold=false;}
}
LiveResult Edit(Cell &cell,uint8_t operation,const StenoStroke *outline,size_t count,const char *text,size_t bytes) {
    if(!healthy || !count || count>16 || bytes>2047)return LiveResult::Invalid;
    uint32_t strokes[16];for(size_t i=0;i<count;++i)strokes[i]=outline[i].GetKeyState();
    flash_device.mapped=false;
    const auto result=store.Edit(cell.identity,operation,strokes,count,reinterpret_cast<const uint8_t *>(text),bytes);
    flash_device.mapped=true;
    if(result==LiveResult::Ok && parent)parent->OnLookupDataChanged();
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
bool Next(MergeRecord &record) {
    for(;;) {
        uint32_t address;
        if(export_cursor.indexed<export_view.forward_count) {
            uint8_t word[4];if(!flash_device.Read(export_view.forward_address+export_cursor.indexed++*4,word,4)){base_provider.Fault();return false;}
            address=Word(word);
        } else if(export_cursor.recent<export_view.tail.count)address=export_view.tail.addresses[export_cursor.recent++];
        else return false;
        if(!iter_ops.Record(address,record,true)){base_provider.Fault();return false;}
        if(!iter_ops.Active(record.identity))continue;
        RecordView newest;
        const auto result=store.live.FindSnapshot(export_view,record.identity,record.strokes,record.count,newest);
        if(result!=LiveResult::Ok && result!=LiveResult::Missing){base_provider.Fault();return false;}
        if(result==LiveResult::Ok && newest.operation==Operation::Set && newest.text_address==record.text_address)return true;
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
    while(!context.IsDone() && Next(record)) {
        if(record.identity!=identity || record.text_bytes<=context.prefixLength)continue;
        if(!flash_device.Read(record.text_address,reinterpret_cast<uint8_t *>(export_text),record.text_bytes)){base_provider.Fault();return;}
        export_text[record.text_bytes]=0;
        if(std::strncmp(export_text,context.prefix,context.prefixLength))continue;
        StenoStroke outline[16];for(unsigned i=0;i<record.count;++i)outline[i]=StenoStroke(record.strokes[i]);
        context.Add(outline,record.count,export_text,this);
    }
}
void Cell::PrintEntriesWithPartialOutline(PrintPartialOutlineContext &context)const {
    ExportGuard guard;if(!guard.ok)return;MergeRecord record;
    while(!context.IsDone() && Next(record)) {
        if(record.identity!=identity || record.count<=context.length)continue;
        StenoStroke outline[16];for(unsigned i=0;i<record.count;++i)outline[i]=StenoStroke(record.strokes[i]);
        if(!StenoStroke::HasPartialOutline(outline,context.strokes,context.length,record.count-context.length))continue;
        if(!flash_device.Read(record.text_address,reinterpret_cast<uint8_t *>(export_text),record.text_bytes)){base_provider.Fault();return;}
        export_text[record.text_bytes]=0;context.Add(outline,record.count,export_text,this);
    }
}
void Cell::ReverseLookup(StenoReverseDictionaryLookup &lookup)const {
    ExportGuard guard;if(!guard.ok || lookup.definitionLength>2047)return;
    auto matches=[&](uint32_t address) {
        MergeRecord record;if(!iter_ops.Record(address,record))return -1;
        if(record.identity!=identity || record.operation || record.count>=lookup.ignoreStrokeThreshold)return 0;
        RecordView current;
        const auto result=store.live.FindSnapshot(export_view,identity,record.strokes,record.count,current);
        if(result!=LiveResult::Ok && result!=LiveResult::Missing)return -1;
        if(result!=LiveResult::Ok || current.operation!=Operation::Set || current.text_address!=record.text_address)return 0;
        StenoStroke outline[16];for(unsigned i=0;i<record.count;++i)outline[i]=StenoStroke(record.strokes[i]);
        return !parent || parent->GetDictionaryForOutline(outline,record.count)==this?1:0;
    };
    auto compare=[&](uint32_t address) {
        MergeRecord record;if(!iter_ops.Record(address,record))return 2;
        return CompareText(address+32,record.record.payload_bytes,
            reinterpret_cast<const uint8_t *>(lookup.definition),lookup.definitionLength,
            [](uint32_t at,uint8_t *out,size_t n){return flash_device.Read(at,out,n);});
    };
    auto emit=[&](uint32_t address) {
        MergeRecord record;if(!iter_ops.Record(address,record)){base_provider.Fault();return;}
        StenoStroke outline[16];for(unsigned i=0;i<record.count;++i)outline[i]=StenoStroke(record.strokes[i]);
        lookup.AddResult(outline,record.count,this);
    };
    for(unsigned i=0;i<export_view.tail.count;++i) {
        const uint32_t address=export_view.tail.addresses[i];const int order=compare(address);
        // Only sets belong in a reverse search. Remove/reset records are valid
        // tail inputs but CompareText intentionally rejects their empty payload.
        const int op=iter_ops.Operation(address);if(op<0){base_provider.Fault();return;}if(op)continue;
        if(order==2){base_provider.Fault();return;}if(order)continue;
        const int accepted=matches(address);if(accepted<0){base_provider.Fault();return;}if(accepted)emit(address);
    }
    uint8_t prefix[8];if(!flash_device.Read(export_view.root.address,prefix,8)){base_provider.Fault();return;}
    uint32_t reverse=0,count=0;const uint32_t extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
    for(unsigned i=0;i<extents;++i) {
        uint8_t row[24];if(!flash_device.Read(export_view.root.address+32+export_view.dictionary_count*96+i*24,row,24)){base_provider.Fault();return;}
        if(Word(row+12)==4){reverse=(uint16_t(row[0]) | uint16_t(row[1])<<8)*kSegmentBytes;count=Word(row+4)/4;}
    }
    auto read=[&](uint32_t at,uint8_t *out,size_t n){return at<=count*4 && n<=count*4-at && flash_device.Read(reverse+at,out,n);};
    ReverseCursor cursor;
    if(SeekReverse(count,read,compare,cursor)!=Result::Found){base_provider.Fault();return;}
    while(cursor.next<cursor.end) {
        uint32_t page[16];unsigned used=0;
        const auto result=NextReverse(cursor,16,read,compare,matches,[&](uint32_t address){page[used++]=address;});
        if(result==PageResult::Corrupt || result==PageResult::IoError){base_provider.Fault();return;}
        for(unsigned i=0;i<used;++i)emit(page[i]);
        if(result==PageResult::Done)break;
    }
    uint32_t mask;
    if(healthy && hint && lookup.ignoreStrokeThreshold>1 && hint->Find(lookup.definition,mask)) {
        RecordView overlay;const auto state=store.live.FindSnapshot(export_view,identity,&mask,1,overlay);
        if(state!=LiveResult::Missing && state!=LiveResult::Ok){base_provider.Fault();return;}
        if(state==LiveResult::Missing || overlay.operation==Operation::Reset) {
            StenoStroke stroke(mask);
            if(!parent || parent->GetDictionaryForOutline(&stroke,1)==this)lookup.AddResult(&stroke,1,this);
        }
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
void lookup_outline(void *,const char *line) {
    StrokeListParser parser;const char *outline=std::strchr(line,' ');
    if(!parent || !outline || !parser.Parse(outline+1) || !parser.GetCount()){Console::Printf("ERR Invalid outline\n\n");return;}
    auto result=parent->Lookup(StenoDictionaryLookup(parser.GetData(),parser.GetCount()));
    if(result.IsValid()){Console::Printf("%s\n\n",result.GetText());result.Destroy();}
    else Console::Printf("ERR No translation\n\n");
}
void store_status(void *,const char *) {
    const auto *personal=ByName("user_dictionary");
    Console::Printf("{\"format\":\"SQBCAT02\",\"ready\":%s,\"generation\":%u,\"tail\":%u,\"maintenance\":%u,\"personal_id\":%u,\"dictionaries\":%u,\"ids\":[",healthy?"true":"false",unsigned(store.selected.root.generation),store.live.TailCount(),unsigned(store.work),personal?personal->identity:0,unsigned(cell_count));
    for(size_t i=0;i<cell_count;++i)Console::Printf("%s%u",i?",":"",cells[i].identity);
    Console::Printf("]}\n\n");
}
void store_verify(void *,const char *) {
    if(store.work!=NativeStore<FlashDevice>::Work::None){Console::Printf("ERR Native maintenance busy\n\n");return;}
    flash_device.Invalidate();flash_device.mapped=false;const bool good=validator.Validate(store.selected.root)==CatalogResult::Ok;flash_device.mapped=true;
    if(good){scrub_due=k_uptime_get()+24*60*60*1000LL;Console::SendOk();}else {base_provider.Fault();Console::Printf("ERR Native integrity failure\n\n");}
}
void store_rebuild(void *,const char *) {
    const auto result=store.Start(NativeStore<FlashDevice>::Work::Checkpoint);Kick();
    if(result==JobResult::More)Console::SendOk();else Console::Printf("ERR Native maintenance unavailable\n\n");
}
void reset_entries(void *,const char *) {
    auto *cell=ByName("user_dictionary");uint32_t replacement=0;
    if(!cell || !healthy || exporting){Console::Printf("ERR Native reset unavailable\n\n");return;}
    flash_device.mapped=false;
    const auto result=store.Clear(cell->identity,replacement,[](uint32_t i,const uint8_t *row){return validator.Extent(i,row);});
    flash_device.mapped=true;
    if(result!=CatalogResult::Ok){if(!store.opened)base_provider.Fault();Console::Printf("ERR Native reset refused\n\n");return;}
    cell->identity=replacement;cell->Bind();if(parent)parent->OnLookupDataChanged();Console::SendOk();
}
void bulk_import(void *,const char *) {Console::Printf("ERR Native store requires native import tooling\n\n");}
bool Number(const char *&p,uint32_t &out);
NativeEntryStager entry_transfer;
alignas(4) uint8_t entry_chunk[512];
void native_entry(void *,const char *line) {
    constexpr char prefix[]="native_entry ";
    if(std::strncmp(line,prefix,sizeof(prefix)-1)){Console::Printf("ERR Missing entry operation\n\n");return;}
    const char *p=line+sizeof(prefix)-1;
    if(!std::strcmp(p,"abort")){entry_transfer.Abort();Console::SendOk();return;}
    if(!std::strncmp(p,"begin ",6)) {
        p+=6;uint32_t id,bytes;
        if(!Number(p,id) || *p++!=' ' || !Number(p,bytes) || bytes>2047 || *p++!=' ' || !ById(id)){Console::Printf("ERR Invalid native entry target\n\n");return;}
        StrokeListParser parser;
        if(!parser.Parse(p) || !parser.GetCount() || parser.GetCount()>16 || *parser.failureOrEnd){Console::Printf("ERR Invalid native outline\n\n");return;}
        uint32_t masks[16];for(unsigned i=0;i<parser.GetCount();++i)masks[i]=parser.GetData()[i].GetKeyState();
        ReplyEdit(entry_transfer.Begin(id,masks,parser.GetCount(),bytes));return;
    }
    if(!std::strncmp(p,"data ",5)) {
        p+=5;uint32_t offset;size_t decoded=0;
        if(!Number(p,offset) || *p++!=' ' || base64_decode(entry_chunk,sizeof(entry_chunk),&decoded,reinterpret_cast<const uint8_t *>(p),std::strlen(p))){Console::Printf("ERR Invalid entry chunk\n\n");return;}
        ReplyEdit(entry_transfer.Write(offset,entry_chunk,decoded));return;
    }
    if(!std::strcmp(p,"commit")) {
        ReplyEdit(entry_transfer.Commit([](uint32_t id,const uint32_t *masks,uint8_t count,const uint8_t *text,uint16_t bytes) {
            auto *cell=ById(id);if(!cell)return LiveResult::Invalid;
            StenoStroke strokes[16];for(unsigned i=0;i<count;++i)strokes[i]=StenoStroke(masks[i]);
            return Edit(*cell,0,strokes,count,reinterpret_cast<const char *>(text),bytes);
        }));return;
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
    for(unsigned i=0;i<extents;++i) {
        uint8_t row[24];if(!flash_device.Read(root.address+32+dictionaries*96+i*24,row,24))return 0;
        if(Word(row+12)==1)crc=crc32_ieee_update(crc,row,24);
    }
    return crc?crc:0x4e415431u;
}
} // namespace

extern "C" bool steno_squid_native_flash_open(void) {
    healthy=false;flash_device.mapped=false;
    LoadNativeTrust();
    const auto result=store.Open([](const CatalogRoot &root){
        const uint32_t fingerprint=BaseFingerprint(root);
        flash_device.trust_bases=fingerprint && fingerprint==verified_fingerprint;
        const auto status=validator.Validate(root);flash_device.trust_bases=false;return status;
    });
    if(result!=CatalogResult::Ok){base_provider.Fault();return false;}
    uint8_t prefix[8];if(!flash_device.Read(store.selected.root.address,prefix,8))return false;
    const size_t count=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
    if(count>MaxDictionaries || (loaded && count!=cell_count))return false;
    for(size_t i=0;i<count;++i) {
        uint8_t row[96];if(!store.Row(i,row))return false;
        auto &cell=cells[i];const uint32_t id=Word(row);
        if(loaded && id!=cell.identity)return false;
        cell.identity=id;cell.enabled=Word(row+4);std::memcpy(cell.name,row+16,row[12]);cell.name[row[12]]=0;
        const uint16_t base=uint16_t(row[8]) | uint16_t(row[9])<<8;
        if(base!=kNoExtent) {
            uint8_t extent[24];
            if(!flash_device.Read(store.selected.root.address+32+count*96+base*24,extent,24) ||
               !cell.LoadBase((uint16_t(extent[0]) | uint16_t(extent[1])<<8)*kSegmentBytes,Word(extent+4),Word(extent+8)))return false;
        } else if(cell.base)return false;
        cell.Bind();
    }
    cell_count=count;loaded=true;flash_device.mapped=true;
    for(size_t i=0;i<count;++i)if(!cells[i].Probe()){base_provider.Fault();return false;}
    healthy=true;
    const uint32_t fingerprint=BaseFingerprint(store.selected.root);
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
    auto *cell=ByName(name);if(!healthy || !cell || exporting)return false;
    const bool value=operation==0?true:operation==1?false:!cell->enabled;
    flash_device.mapped=false;
    const auto result=store.Enable(cell->identity,value,[](uint32_t i,const uint8_t *row){return validator.Extent(i,row);});
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
alignas(4) uint8_t incoming[512];
void reboot_worker(k_work *){sys_reboot(SYS_REBOOT_COLD);}
K_WORK_DELAYABLE_DEFINE(reboot_work,reboot_worker);
int64_t install_deadline=0;
void install_timeout(k_work *);
K_WORK_DELAYABLE_DEFINE(install_timeout_work,install_timeout);
void TouchInstall(){install_deadline=k_uptime_get()+60000;k_work_reschedule_for_queue(&steno_squid_engine_queue,&install_timeout_work,K_SECONDS(60));}
void install_timeout(k_work *) {
    k_mutex_lock(&flash_lock,K_FOREVER);
    if(transfer.Active() && k_uptime_get()>=install_deadline) {
        transfer.Abort();dictionary_flash_read_session(false);steno_squid_upload_status(STENO_SQUID_MODE_UNKNOWN,0);
    } else if(transfer.Active())TouchInstall();
    k_mutex_unlock(&flash_lock);
}

bool Number(const char *&p,uint32_t &out) {
    if(*p<'0' || *p>'9')return false;
    out=0;
    while(*p>='0' && *p<='9'){uint32_t digit=*p++-'0';if(out>(UINT32_MAX-digit)/10)return false;out=out*10+digit;}
    return true;
}
bool End(const char *p){return !*p || (*p=='\n' && !p[1]);}
const char *Install(const char *command,char *reply,size_t bytes) {
    if(!std::strcmp(command,"status\n")) {
        snprintk(reply,bytes,"NATIVE1 %s %u",healthy?"READY":transfer.Active()?"INSTALL":"UNPROVISIONED",unsigned(store.selected.root.generation));return nullptr;
    }
    if(!std::strncmp(command,"begin CLEAR ",12)) {
        const char *p=command+12;uint32_t crc;
        if(!Number(p,crc) || !End(p))return "ERR Invalid manifest CRC";
        if(transfer.Active() || exporting || store.work!=NativeStore<FlashDevice>::Work::None)return "ERR Native store busy";
        if(lf_suspended || !lf_provisioning_ready || !squid_qspi_reactivate())return "ERR Flash unavailable";
        if(!ForgetBaseTrust())return "ERR Cannot invalidate native base trust; no flash changed";
        healthy=store.opened=false;flash_device.mapped=false;flash_device.Invalidate();store.catalog.Invalidate();atomic_clear(&lf_mapped_ready);
        if(transfer.Begin(crc)!=ProvisionResult::Ok)return "ERR Native installer busy";
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
        if(!Number(p,segment) || segment>=255 || !End(p))return "ERR Invalid native segment";
        if(transfer.Erase(segment)!=ProvisionResult::Ok)return "ERR Native erase failed";
        steno_squid_upload_status(STENO_SQUID_MODE_INSTALL,30000);return "OK";
    }
    if(!std::strncmp(command,"write ",6)) {
        const char *p=command+6;uint32_t at;size_t decoded=0;
        if(!Number(p,at) || *p++!=' ' || at%4 || at>=Limit)return "ERR Invalid native write";
        const size_t encoded=std::strcspn(p,"\n");
        if(!End(p+encoded) || base64_decode(incoming,sizeof(incoming),&decoded,reinterpret_cast<const uint8_t *>(p),encoded) ||
           !decoded || decoded%4 || decoded>Limit-at)return "ERR Invalid native data";
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
extern "C" void steno_squid_native_install_command(const char *command,char *reply,size_t bytes) {
    if(!dictionary_flash_acquire())k_mutex_lock(&flash_lock,K_FOREVER);
    const char *result=Install(command,reply,bytes);if(result)snprintk(reply,bytes,"%s",result);
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
    while(scrub_cell<cell_count && !cells[scrub_cell].base)++scrub_cell;
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
