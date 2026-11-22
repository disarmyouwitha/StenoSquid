/* Integrated native catalog/checkpoint test on twelve reserved 4 KiB sectors.
 * No engine storage switch. All physical IO is bounded to FF4000..FFFFFF. */
#include "storage_probe.h"
#include "console.h"
#include "dictionary_flash.h"
#include "javelin_engine_lock.h"
#include "w25q_quad.h"
#include "xip_load.h"
#include "native_dictionary_adapter.h"
#include "../../tools/storage/native_catalog.h"
#include "../../tools/storage/native_live_journal.h"
#include "../../tools/storage/native_checkpoint_job.h"
#include "../../tools/storage/native_rollover_job.h"
#include "../../tools/storage/native_checkpoint.h"
#include "../../tools/storage/native_index_sink.h"
#include "../../tools/storage/binary_record.h"
#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/reboot.h>
#include <nrfx.h>
#include <cstring>

namespace {
using namespace candidate_b;
constexpr uint32_t Physical=0xff4000, Virtual=2*kSegmentBytes;
constexpr uint64_t Epoch=0x435050524f424532ULL;
constexpr uintptr_t Xip=DT_REG_ADDR_BY_NAME(DT_NODELABEL(qspi),qspi_mm);
struct ProbeDevice {
    bool mapped=false;
    alignas(4) uint8_t dma[256];
    bool Translate(uint32_t at,uint32_t n,uint32_t &physical) {
        uint32_t segment=at/kSegmentBytes,offset=at%kSegmentBytes;
        if(segment>=12 || offset>4096 || n>4096-offset)return false;
        physical=Physical+segment*4096+offset;return true;
    }
    bool Read(uint32_t at,uint8_t *out,uint32_t n) {
        uint32_t physical;
        if(!Translate(at,n,physical))return false;
        if(mapped){copy_xip_bytes(out,reinterpret_cast<const void *>(Xip+physical),n);return true;}
        while(n) {
            uint32_t skip=physical%4,size=n<256-skip?n:256-skip;
            if(nrfx_qspi_read(dma,(size+skip+3)&~3u,physical-skip)!=NRFX_SUCCESS)return false;
            std::memmove(out,dma+skip,size);physical+=size;out+=size;n-=size;
        }
        return true;
    }
    bool Program(uint32_t at,const uint8_t *data,uint32_t n) {
        uint32_t physical;
        if(mapped || !n || n>256 || at%4 || at/256!=(at+n-1)/256 ||
           !Translate(at,(n+3)&~3u,physical))return false;
        std::memset(dma,255,sizeof(dma));std::memcpy(dma,data,n);
        return nrfx_qspi_write(dma,(n+3)&~3u,physical)==NRFX_SUCCESS && squid_qspi_wait_ready(2000)==NRFX_SUCCESS;
    }
    bool Erase(uint32_t segment) {
        return segment>=2 && segment<12 && EraseRegion(segment*kSegmentBytes);
    }
    bool EraseIndex(uint32_t segment, uint32_t bytes) {
        return bytes == kEraseSectorBytes && Erase(segment);
    }
    bool ErasePoolSector(uint32_t address) {
        return address >= Virtual && EraseRegion(address);
    }
    bool EraseRegion(uint32_t address) {
        uint32_t physical;
        return !mapped && address%kSegmentBytes==0 && Translate(address,4096,physical) &&
            nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_4KB,physical)==NRFX_SUCCESS && squid_qspi_wait_ready(2000)==NRFX_SUCCESS;
    }
    bool EraseCatalog(uint32_t address,uint32_t bytes) {
        return (address==0 || address==kSegmentBytes) && bytes==4096 && EraseRegion(address);
    }
    bool Blank() {
        for(unsigned segment=0;segment<12;++segment)for(uint32_t offset=0;offset<4096;offset+=256) {
            if(!Read(segment*kSegmentBytes+offset,dma,256))return false;
            for(auto byte:dma)if(byte!=255)return false;
        }
        return true;
    }
} probe_device;
NativeJournal<ProbeDevice> probe_journal(probe_device);
NativeCatalog<ProbeDevice> probe_catalog(probe_device,0,kSegmentBytes,4096);
LiveJournal<ProbeDevice> probe_live(probe_device,probe_catalog);
NativeCheckpointJob<ProbeDevice> probe_job(probe_device,probe_catalog,probe_live);
NativeRolloverJob<ProbeDevice> probe_rollover(probe_device,probe_catalog,probe_live);
LiveView live_snapshot; // Fixed workspace; never put this on the HID stack.
IndexSink<ProbeDevice> forward_sink(probe_device),reverse_sink(probe_device);
alignas(4) uint8_t payload[32+2*96+3*24];
Tail recent;
uint32_t steps=0;
CatalogSelection selected;
uint32_t selected_forward=5*kSegmentBytes;

struct BuildOps {
    int Operation(uint32_t address) {
        uint8_t prefix[8];
        return probe_device.Read(address+32,prefix,8)?prefix[4]:-1;
    }
    int CompareKey(uint32_t a,uint32_t b) {
        JournalRecord record;
        JournalKey left, right;
        if(probe_journal.ReadKey(a,record,left)!=JournalResult::Ok ||
           probe_journal.ReadKey(b,record,right)!=JournalResult::Ok)return 2;
        return CompareOutlineKey(left,right.identity,right.strokes,right.count);
    }
    int CompareReverse(uint32_t a,uint32_t b) {
        uint8_t x[32],y[32];
        if(!probe_device.Read(a+32,x,8) || !probe_device.Read(b+32,y,8))return 2;
        uint32_t ax=a+40+4*x[5],by=b+40+4*y[5];
        uint32_t an=uint16_t(x[6]) | uint16_t(x[7])<<8,bn=uint16_t(y[6]) | uint16_t(y[7])<<8;
        if(x[4] || y[4] || an>2047 || bn>2047)return 2;
        for(uint32_t offset=0;offset<an && offset<bn;) {
            uint32_t n=an-offset;if(n>bn-offset)n=bn-offset;if(n>32)n=32;
            if(!probe_device.Read(ax+offset,x,n) || !probe_device.Read(by+offset,y,n))return 2;
            for(uint32_t i=0;i<n;++i)if(x[i]!=y[i])return x[i]<y[i]?-1:1;
            offset+=n;
        }
        if(an!=bn)return an<bn?-1:1;
        JournalRecord record;
        JournalKey left, right;
        if(probe_journal.ReadKey(a,record,left)!=JournalResult::Ok ||
           probe_journal.ReadKey(b,record,right)!=JournalResult::Ok)return 2;
        return CompareReverseKeyTie(left,right.identity,right.strokes,right.count);
    }
    bool ReadIndex(bool reverse,uint32_t position,uint32_t &address) {
        uint8_t bytes[4];
        if(!probe_device.Read((reverse?4u:3u)*kSegmentBytes+position*4,bytes,4))return false;
        address=Word(bytes);return true;
    }
    bool Emit(bool reverse,uint32_t address) {return reverse?reverse_sink.Append(address):forward_sink.Append(address);}
} build_ops;
CheckpointBuilder<BuildOps> builder(build_ops);

bool CheckExtent(uint32_t,const uint8_t *extent) {
    uint32_t start=uint16_t(extent[0]) | uint16_t(extent[1])<<8;
    uint32_t bytes=Word(extent+4),kind=Word(extent+12);
    if(start<2 || start>11 || !bytes || (kind!=2 && bytes>4096))return false;
    if(kind==2) {
        if((start!=2 && start!=3) || (bytes!=4096 && bytes!=kSegmentBytes))return false;
        const uint64_t epoch=Epoch+(start==3);
        if((uint64_t(Word(extent+16)) | uint64_t(Word(extent+20))<<32)!=epoch ||
           probe_journal.Open(start,epoch)!=JournalResult::Ok)return false;
        JournalScan scan;JournalRecord record;
        if(probe_journal.BeginScan(start,epoch,0,scan)!=JournalResult::Ok)return false;
        unsigned count=0;JournalResult status;
        while((status=probe_journal.Step(scan,record))==JournalResult::Ok) {
            if(start==3 && record.sequence!=11)return false;
            ++count;
        }
        return status==JournalResult::End && (start==3?count<=1:(count==6 || count==9 || count==10));
    }
    if(((kind==3 || kind==4) && start<3) || (kind!=3 && kind!=4) || bytes%4)return false;
    uint32_t crc=0,previous=0;
    uint8_t data[32];
    for(uint32_t offset=0;offset<bytes;) {
        uint32_t n=bytes-offset;if(n>sizeof(data))n=sizeof(data);
        if(!probe_device.Read(start*kSegmentBytes+offset,data,n))return false;
        crc=Crc(crc,data,n);
        for(uint32_t i=0;i<n;i+=4) {
            uint32_t address=Word(data+i);JournalRecord record;
            if(probe_journal.Read(address,record)!=JournalResult::Ok || address/kSegmentBytes!=2)return false;
            int op=build_ops.Operation(address);
            if(op<0 || op==2 || (kind==4 && op!=0))return false;
            if(previous && (kind==3?build_ops.CompareKey(previous,address):build_ops.CompareReverse(previous,address))>=0)return false;
            previous=address;
        }
        offset+=n;
    }
    return crc==Word(extent+8);
}

bool Source(uint32_t at,uint8_t *out,uint32_t n) {
    if(at>sizeof(payload) || n>sizeof(payload)-at)return false;
    std::memcpy(out,payload+at,n);return true;
}

Result FindProbeRecord(uint32_t dictionary,const uint32_t *strokes,uint8_t count,RecordView &view) {
    auto read=[](uint32_t offset,uint8_t *out,size_t n){return probe_device.Read(selected_forward+offset,out,n);};
    auto compare=[&](uint32_t address) {
        JournalRecord record;
        JournalKey key;
        return probe_journal.ReadKey(address,record,key)==JournalResult::Ok ?
            CompareOutlineKey(key,dictionary,strokes,count):2;
    };
    uint32_t address=0;
    const auto result=FindOrdered(2,read,compare,address);
    if(result!=Result::Found)return result;
    JournalRecord record;
    JournalKey key;
    if(probe_journal.Read(address,record,&key)!=JournalResult::Ok ||
       CompareOutlineKey(key,dictionary,strokes,count))return Result::Corrupt;
    view={static_cast<Operation>(key.operation),address+40u+4u*key.count,key.text_bytes};
    return Result::Found;
}

class ProbeBackend final : public DictionaryBackend {
public:
    bool Acquire(uint32_t dictionary,uint32_t &token)override {
        return dictionary>=1 && dictionary<=2 && probe_catalog.AcquirePin(selected.root,token);
    }
    void Release(uint32_t token)override{probe_catalog.ReleasePin(token);}
    EditState Find(uint32_t,uint32_t dictionary,const StenoDictionaryLookup &lookup,EditText &text)override {
        uint32_t strokes[16];for(size_t i=0;i<lookup.length;++i)strokes[i]=lookup.strokes[i].GetKeyState();
        RecordView view;
        const auto result=FindProbeRecord(dictionary,strokes,lookup.length,view);
        if(result==Result::Missing)return EditState::Missing;
        if(result!=Result::Found)return EditState::Error;
        text={view.text_address,view.text_bytes};
        return view.operation==Operation::Set?EditState::Set:view.operation==Operation::Remove?EditState::Remove:EditState::Reset;
    }
    bool Read(uint32_t,const EditText &text,char *out)override {
        return probe_device.Read(text.address,reinterpret_cast<uint8_t *>(out),text.bytes);
    }
    StenoDictionaryLookupResult Base(uint32_t,uint32_t,const StenoDictionaryLookup &)override {
        return StenoDictionaryLookupResult::CreateInvalid(); // Probe has no compact base.
    }
} probe_backend;
NativeOverlayDictionary probe_dictionary(probe_backend,1,"Checkpoint Test");

bool Verify() {
    if(probe_catalog.Open(selected,CheckExtent)!=CatalogResult::Ok || (selected.root.generation<2 || selected.root.generation>4))return false;
    CatalogShape shape;
    if(probe_catalog.Validate(selected.root,CheckExtent,&shape)!=CatalogResult::Ok || shape.dictionaries!=2 ||
       shape.extents!=(selected.root.generation==4?4u:3u) || shape.through!=(selected.root.generation>=3?10u:9u))return false;
    uint8_t first[96];
    if(!probe_device.Read(selected.root.address+32,first,96) || first[16]!='N')return false;
    selected_forward=0;
    for(unsigned i=0;i<shape.extents;++i) {
        uint8_t extent[24];
        if(!probe_device.Read(selected.root.address+32+shape.dictionaries*96+i*24,extent,24))return false;
        if(Word(extent+12)==3)selected_forward=(uint16_t(extent[0]) | uint16_t(extent[1])<<8)*kSegmentBytes;
    }
    if(!selected_forward)return false;
    const char *expected=selected.root.generation>=3?"live":"newer";
    // Generation 3 checkpoints the live tail using the native transaction job.
    probe_device.mapped=true;
    uint32_t query=8;RecordView view;
    if(FindProbeRecord(1,&query,1,view)!=Result::Found || view.text_bytes!=std::strlen(expected))return false;
    uint8_t text[5];if(!probe_device.Read(view.text_address,text,view.text_bytes) || std::memcmp(text,expected,view.text_bytes))return false;
    const StenoStroke stroke(8);
    auto translated=probe_dictionary.Lookup(StenoDictionaryLookup(&stroke,1));
    const bool bridge=translated.IsValid() && !std::strcmp(translated.GetText(),expected);
    translated.Destroy();if(!bridge)return false;
    probe_device.mapped=false;return squid_qspi_reactivate();
}

bool Prepare() {
    if(!probe_device.Blank())return false;
    if(probe_journal.Initialize(2,Epoch)!=JournalResult::Ok)return false;
    const uint32_t key[]={8,4,16,8,4,16},id[]={1,1,2,1,2,2};
    const uint8_t op[]={0,0,0,0,1,2};
    const char *text[]={"old","base","cat","new","",""};
    uint32_t address=Virtual+256;
    recent={};steps=0;
    for(unsigned i=0;i<6;++i) {
        if(probe_journal.Append(address,i+1,id[i],op[i],key+i,1,
            reinterpret_cast<const uint8_t *>(text[i]),std::strlen(text[i]))!=JournalResult::Ok)return false;
        if(!recent.Append(address))return false;
        JournalRecord record;if(probe_journal.Read(address,record)!=JournalResult::Ok)return false;
        address+=record.span;
    }
    // Expected forward: ID1 key4(base), ID1 key8(new), ID2 key4(remove).
    // ID2 key16 reset removes its old set. No compact base in this isolated test.
    if(!probe_device.EraseRegion(3*kSegmentBytes) || !probe_device.EraseRegion(4*kSegmentBytes) ||
       !forward_sink.Begin(3*kSegmentBytes,4096) || !reverse_sink.Begin(4*kSegmentBytes,4096))return false;
    if(builder.Begin(recent,0,0)!=BuildResult::More)return false;
    for(;;) {
        const auto result=builder.Step();++steps;
        if(result==BuildResult::Done)break;
        if(result!=BuildResult::More || steps>10000)return false;
    }
    if(!forward_sink.Finish() || !reverse_sink.Finish() || builder.ForwardCount()!=3 || builder.ReverseCount()!=2)return false;
    std::memset(payload,0,sizeof(payload));PutWord(payload,3);payload[4]=2;payload[6]=3;PutWord(payload+8,6);
    for(unsigned i=0;i<2;++i) {
        uint8_t *row=payload+32+i*96;PutWord(row,i+1);PutWord(row+4,1);row[8]=row[9]=255;
        row[12]=4;std::memcpy(row+16,"Test",4);
    }
    for(unsigned i=0;i<3;++i) {
        uint8_t *extent=payload+32+2*96+i*24;extent[0]=i+2;extent[2]=1;
        PutWord(extent+4,i==0?4096:i==1?forward_sink.Bytes():reverse_sink.Bytes());
        PutWord(extent+8,i==0?0:i==1?forward_sink.Checksum():reverse_sink.Checksum());
        PutWord(extent+12,i==0?2:i==1?3:4);
        if(!i){PutWord(extent+16,uint32_t(Epoch));PutWord(extent+20,uint32_t(Epoch>>32));}
        if(i==1)PutWord(extent+16,kOutlineFirst);
    }
    CatalogSelection empty,first;
    if(probe_catalog.Publish(empty,sizeof(payload),Source,CheckExtent,first)!=CatalogResult::Ok)return false;
    // A second checkpoint merges the immutable first indexes with three edits.
    // It stages replacement tables elsewhere, preserving the old root/indexes.
    recent={};
    const uint32_t changed_key[]={4,8,4},changed_id[]={1,1,2};
    const uint8_t changed_op[]={2,0,0};
    const char *changed_text[]={"","newer","again"};
    for(unsigned i=0;i<3;++i) {
        if(probe_journal.Append(address,7+i,changed_id[i],changed_op[i],changed_key+i,1,
            reinterpret_cast<const uint8_t *>(changed_text[i]),std::strlen(changed_text[i]))!=JournalResult::Ok)return false;
        if(!recent.Append(address))return false;
        JournalRecord record;if(probe_journal.Read(address,record)!=JournalResult::Ok)return false;
        address+=record.span;
    }
    if(!probe_device.EraseRegion(5*kSegmentBytes) || !probe_device.EraseRegion(6*kSegmentBytes) ||
       !forward_sink.Begin(5*kSegmentBytes,4096) || !reverse_sink.Begin(6*kSegmentBytes,4096))return false;
    if(builder.Begin(recent,3,2)!=BuildResult::More)return false;
    for(;;) {
        const auto result=builder.Step();++steps;
        if(result==BuildResult::Done)break;
        if(result!=BuildResult::More || steps>10000)return false;
    }
    if(!forward_sink.Finish() || !reverse_sink.Finish() || builder.ForwardCount()!=2 || builder.ReverseCount()!=2)return false;
    payload[32+16]='N';PutWord(payload+8,9);
    for(unsigned i=1;i<3;++i) {
        uint8_t *extent=payload+32+2*96+i*24;extent[0]=i+4;
        PutWord(extent+4,i==1?forward_sink.Bytes():reverse_sink.Bytes());
        PutWord(extent+8,i==1?forward_sink.Checksum():reverse_sink.Checksum());
    }
    if(probe_catalog.Publish(first,sizeof(payload),Source,CheckExtent,selected)!=CatalogResult::Ok)return false;
    return Verify();
}
bool RecoverLive() {
    if(probe_live.Begin(selected)!=LiveResult::More)return false;
    for(unsigned step=0;step<64;++step) {
        const auto status=probe_live.RecoverStep();
        if(status==LiveResult::Ready)return true;
        if(status!=LiveResult::More)return false;
    }
    return false;
}
bool LiveText(const LiveView &input,const char *expected) {
    uint32_t key=8;RecordView record;uint8_t text[8];
    return probe_live.FindSnapshot(input,1,&key,1,record)==LiveResult::Ok &&
        record.text_bytes==std::strlen(expected) &&
        probe_live.ReadText(input,record,text,record.text_bytes)==LiveResult::Ok &&
        !std::memcmp(text,expected,record.text_bytes);
}
bool CheckLive() {
    if(!Verify() || !RecoverLive() || probe_live.Highwater()!=(selected.root.generation==4?11u:10u) || probe_live.TailCount()!=(selected.root.generation==3?0u:1u))return false;
    if(probe_live.Snapshot(live_snapshot)!=LiveResult::Ok)return false;
    const bool valid=LiveText(live_snapshot,selected.root.generation==4?"rolled":"live");
    return probe_live.Release(live_snapshot) && valid;
}
bool AppendLive() {
    if(!Verify() || !RecoverLive() || probe_live.Highwater()!=9 || probe_live.TailCount())return false;
    if(probe_live.Snapshot(live_snapshot)!=LiveResult::Ok)return false;
    uint32_t key=8;const uint8_t text[]={'l','i','v','e'};
    const bool appended=probe_live.Append(1,0,&key,1,text,sizeof(text))==LiveResult::Ok;
    // The old snapshot must still see checkpoint data after live publication.
    const bool isolated=appended && LiveText(live_snapshot,"newer");
    const bool released=probe_live.Release(live_snapshot);
    return isolated && released && CheckLive();
}
bool CheckpointLive() {
    if(!CheckLive() || selected.root.generation!=2)return false;
    SegmentMap excluded;excluded.Add(12,244);
    if(probe_job.Begin(selected,excluded)!=JobResult::More)return false;
    for(unsigned i=0;i<10000;++i) {
        const auto status=probe_job.Step(CheckExtent);++steps;
        if(status==JobResult::Done){selected=probe_job.Selection();return CheckLive();}
        if(status!=JobResult::More)return false;
        if(!LiveText(probe_job.Input(),"live")){probe_job.Cancel();return false;}
    }
    probe_job.Cancel();return false;
}
bool RolloverLive() {
    if(!CheckLive() || selected.root.generation!=3)return false;
    SegmentMap excluded;excluded.Add(12,244);
    if(probe_rollover.Begin(selected,excluded)!=JobResult::More)return false;
    for(unsigned i=0;i<10000;++i) {
        const auto status=probe_rollover.Step(CheckExtent);++steps;
        if(status==JobResult::Done) {
            selected=probe_rollover.Selection();uint32_t key=8;
            const uint8_t text[]={'r','o','l','l','e','d'};
            return probe_live.Append(1,0,&key,1,text,sizeof(text))==LiveResult::Ok && CheckLive();
        }
        if(status!=JobResult::More)return false;
        if(!LiveText(probe_rollover.Input(),"live")){probe_rollover.Cancel();return false;}
    }
    probe_rollover.Cancel();return false;
}
bool Clean() {
    if(!Verify() || !RecoverLive())return false;
    if(probe_live.Highwater()!=9 && !CheckLive())return false;
    probe_device.mapped=false;
    if(!squid_qspi_reactivate())return false;
    // Record/index sectors first, catalog identities last. Failed cleanup cannot
    // be mistaken for valid data by the next Verify; no automatic force erase.
    probe_catalog.Invalidate();
    const unsigned cleanup_order[]={2,3,4,5,6,7,8,9,10,11,0,1};
    for(unsigned segment:cleanup_order)if(!probe_device.EraseRegion(segment*kSegmentBytes))return false;
    return probe_device.Blank();
}
void reboot_worker(struct k_work *) {sys_reboot(SYS_REBOOT_COLD);}
K_WORK_DELAYABLE_DEFINE(reboot_work,reboot_worker);
}

void steno_squid_checkpoint_probe(void *,const char *line) {
    const char *prefix="dictionary_checkpoint_probe ";
    if(std::strncmp(line,prefix,std::strlen(prefix))) {Console::Printf("ERR Invalid checkpoint probe\n\n");return;}
    const char *command=line+std::strlen(prefix);
    if(!std::strcmp(command,"reboot")) {
        Console::SendOk();k_work_reschedule(&reboot_work,K_SECONDS(2));return;
    }
    if(std::strcmp(command,"run") && std::strcmp(command,"hold") && std::strcmp(command,"check") && std::strcmp(command,"clean") && std::strcmp(command,"live") && std::strcmp(command,"live_check") && std::strcmp(command,"job") && std::strcmp(command,"rollover")) {
        Console::Printf("ERR Expected run, hold, check, live, job, rollover, live_check, clean or reboot\n\n");return;
    }
    const JavelinEngineLock lock;
    if(!lock || !squid_qspi_reactivate()){Console::Printf("ERR Flash unavailable\n\n");return;}
    probe_device.mapped=false;
    if((!std::strcmp(command,"run") || !std::strcmp(command,"hold")) && !probe_device.Blank()) {
        Console::Printf("ERR Checkpoint test region nonblank or unreadable; nothing changed\n\n");return;
    }
    const uint32_t start=k_uptime_get_32();
    bool passed=!std::strcmp(command,"rollover")?RolloverLive():!std::strcmp(command,"job")?CheckpointLive():!std::strcmp(command,"live")?AppendLive():!std::strcmp(command,"live_check")?CheckLive():
        !std::strcmp(command,"check")?Verify():!std::strcmp(command,"clean")?Clean():Prepare();
    bool cleaned=!std::strcmp(command,"clean") && passed;
    if(passed && !std::strcmp(command,"run")){cleaned=Clean();passed=cleaned;}
    probe_device.mapped=false;squid_qspi_reactivate();
    Console::Printf("{\"probe\":\"native-checkpoint\",\"command\":\"%s\",\"passed\":%s,\"cleaned\":%s,\"generation\":%u,\"steps\":%u,\"elapsed_ms\":%u}\n\n",
        command,passed?"true":"false",cleaned?"true":"false",unsigned(selected.root.generation),steps,k_uptime_get_32()-start);
}
