/* Diagnostic-only Candidate-B native core on the reserved last 4 KiB sector.
 * The running engine keeps its existing storage. No catalog/format switch. */
#include "storage_probe.h"
#include "console.h"
#include "dictionary_flash.h"
#include "javelin_engine_lock.h"
#include "w25q_quad.h"
#include "xip_load.h"
#include "../../tools/storage/native_journal.h"
#include "../../tools/storage/packed_index.h"
#include "../../tools/storage/binary_record.h"
#include "../../tools/storage/reverse_cursor.h"
#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
#include <nrfx.h>
#include <array>
#include <cstring>

extern "C" uint32_t lf_flash_bytes;

namespace {
using namespace candidate_b;
constexpr uint32_t Virtual=2*kSegmentBytes, Bytes=4096;
uint32_t Physical() { return lf_flash_bytes - Bytes; }
constexpr uint64_t Epoch=0x4250524f42453031ULL;
constexpr uintptr_t Xip=DT_REG_ADDR_BY_NAME(DT_NODELABEL(qspi),qspi_mm);
struct Scratch {
    bool mapped=false;
    alignas(4) uint8_t dma[256];
    bool Bounds(uint32_t address,uint32_t n) {
        return address>=Virtual && address-Virtual<=Bytes && n<=Bytes-(address-Virtual);
    }
    bool Read(uint32_t address,uint8_t *out,uint32_t n) {
        if (!Bounds(address,n)) return false;
        if (mapped) {
            copy_xip_bytes(out,reinterpret_cast<const void *>(Xip+Physical()+address-Virtual),n);
            return true;
        }
        while(n) {
            uint32_t skip=address&3, size=n<256-skip?n:256-skip;
            if(nrfx_qspi_read(dma,(size+skip+3)&~3u,Physical()+address-Virtual-skip)!=NRFX_SUCCESS) return false;
            std::memmove(out,dma+skip,size);
            address+=size; out+=size; n-=size;
        }
        return true;
    }
    bool Program(uint32_t address,const uint8_t *data,uint32_t n) {
        if(mapped || !n || !Bounds(address,(n+3)&~3u) || address%4 || n>256 ||
           address/256!=(address+n-1)/256) return false;
        std::memset(dma,255,sizeof(dma)); std::memcpy(dma,data,n);
        return nrfx_qspi_write(dma,(n+3)&~3u,Physical()+address-Virtual)==NRFX_SUCCESS &&
               squid_qspi_wait_ready(2000)==NRFX_SUCCESS;
    }
    bool Erase(uint32_t segment) {
        return !mapped && segment==2 && nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_4KB,Physical())==NRFX_SUCCESS &&
               squid_qspi_wait_ready(2000)==NRFX_SUCCESS;
    }
    bool Blank() {
        for(uint32_t at=0;at<Bytes;at+=256) {
            if(!Read(Virtual+at,dma,256)) return false;
            for(auto byte:dma) if(byte!=255) return false;
        }
        return true;
    }
};
// Command runs on the serialized console under engine lock, never key thread.
// Keep the long fixture in internal flash to leave the HID stack and RAM free.
Scratch scratch_device;
NativeJournal<Scratch> journal(scratch_device);
constexpr auto LongText() {
    std::array<uint8_t, 2047> text{};
    for (auto &byte : text) byte = 'x';
    return text;
}
constexpr auto long_text = LongText();
uint32_t table[3];
uint32_t timings[32];

bool execute(uint32_t &max_us,uint32_t &min_us,uint32_t &records) {
    if(journal.Initialize(2,Epoch)!=JournalResult::Ok) return false;
    const uint32_t strokes[]={8,256};
    const uint8_t cat[]={'c','a','t'}, accent[]={0xc3,0xa7,'a',' ','v','a'};
    uint32_t at=Virtual+256;
    table[0]=at;
    if(journal.Append(at,1,1,0,strokes,1,cat,3)!=JournalResult::Ok) return false;
    JournalRecord record;
    if(journal.Read(at,record)!=JournalResult::Ok) return false;
    at+=record.span;
    table[1]=at;
    if(journal.Append(at,2,2,0,strokes,2,accent,6)!=JournalResult::Ok) return false;
    if(journal.Read(at,record)!=JournalResult::Ok) return false;
    at+=record.span;
    table[2]=at;
    if(journal.Append(at,3,3,0,strokes,1,long_text.data(),long_text.size())!=JournalResult::Ok) return false;
    if(journal.Read(at,record)!=JournalResult::Ok) return false;
    at+=record.span;
    if(journal.Append(at,4,4,1,strokes,1,nullptr,0)!=JournalResult::Ok) return false;
    if(journal.Read(at,record)!=JournalResult::Ok) return false;
    at+=record.span;
    if(journal.Append(at,5,4,2,strokes,1,nullptr,0)!=JournalResult::Ok) return false;
    if(journal.Read(at,record)!=JournalResult::Ok) return false;
    at+=record.span;
    // Reopen through fresh cursor: exact sequences and both UTF-8 boundaries.
    JournalScan scan;
    if(journal.BeginScan(2,Epoch,0,scan)!=JournalResult::Ok) return false;
    records=0;
    while(journal.Step(scan,record)==JournalResult::Ok) ++records;
    if(records!=5 || scan.next!=at || journal.Step(scan,record)!=JournalResult::End) return false;
    // An incomplete header must seal the tail and leave committed prefix usable.
    const uint8_t torn[4]={'S','Q','J','R'};
    if(!scratch_device.Program(at,torn,4) || journal.Read(at,record)!=JournalResult::Sealed) return false;
    // Persist a small ordered table, then exercise actual QSPI XIP lookups.
    const uint32_t index=Virtual+3584;
    if(!scratch_device.Program(index,reinterpret_cast<const uint8_t *>(table),sizeof(table))) return false;
    const uint32_t reverse_table[]={table[0],table[2],table[1]};
    const uint32_t reverse_index=index+16;
    if(!scratch_device.Program(reverse_index,reinterpret_cast<const uint8_t *>(reverse_table),sizeof(reverse_table))) return false;
    scratch_device.mapped=true;
    auto read=[&](uint32_t offset,uint8_t *out,size_t size) {
        return offset<=sizeof(table) && size<=sizeof(table)-offset && scratch_device.Read(index+offset,out,size);
    };
    RecordView view;
    auto compare=[&](uint32_t address) {
        uint32_t payload=address==table[0]?15:address==table[1]?22:2059;
        return CompareOutline(address+32,payload,2,strokes,2,
            [&](uint32_t p,uint8_t *out,size_t size){return scratch_device.Read(p,out,size);},view);
    };
    uint32_t found=0;
    min_us=UINT32_MAX; max_us=0;
    for(unsigned i=0;i<32;++i) {
        const uint32_t start=k_cycle_get_32();
        const auto result=FindOrdered(3,read,compare,found);
        timings[i]=k_cyc_to_us_ceil32(k_cycle_get_32()-start);
        if(result!=Result::Found || found!=table[1] || view.text_bytes!=6) return false;
        if(timings[i]<min_us) min_us=timings[i];
        if(timings[i]>max_us) max_us=timings[i];
    }
    auto data_read=[&](uint32_t p,uint8_t *out,size_t size){return scratch_device.Read(p,out,size);};
    if(CompareText(table[1]+32,22,accent,6,data_read)!=0 ||
       CompareText(table[2]+32,2059,long_text.data(),long_text.size(),data_read)!=0) return false;
    auto reverse_read=[&](uint32_t offset,uint8_t *out,size_t size) {
        return offset<=sizeof(reverse_table) && size<=sizeof(reverse_table)-offset && scratch_device.Read(reverse_index+offset,out,size);
    };
    auto text_compare=[&](uint32_t address) {
        return CompareText(address+32,address==table[0]?15:address==table[1]?22:2059,accent,6,data_read);
    };
    ReverseCursor cursor;
    if(SeekReverse(3,reverse_read,text_compare,cursor)!=Result::Found) return false;
    unsigned emitted=0;
    auto status=NextReverse(cursor,1,reverse_read,text_compare,
        [](uint32_t){return 1;},[&](uint32_t address){if(address==table[1]) ++emitted;});
    if(status!=PageResult::Done || emitted!=1) return false;
    // Return peripheral ownership after mapped reads before cleanup erase.
    scratch_device.mapped=false;
    return squid_qspi_reactivate();
}
}

void steno_squid_storage_probe(void *,const char *line) {
    if(std::strcmp(line,"dictionary_storage_probe run")) {
        Console::Printf("ERR Expected dictionary_storage_probe run (reserved scratch sector only)\n\n"); return;
    }
    const JavelinEngineLock lock;
    if(!lock || lf_flash_bytes<kSegmentBytes || !squid_qspi_reactivate()) { Console::Printf("ERR Flash unavailable\n\n"); return; }
    scratch_device.mapped=false;
    if(!scratch_device.Blank()) { Console::Printf("ERR Scratch sector is nonblank or unreadable; nothing changed\n\n"); return; }
    const uint32_t start=k_uptime_get_32();
    std::memset(timings,0,sizeof(timings));
    uint32_t maximum=0,minimum=0,records=0;
    const bool passed=execute(maximum,minimum,records);
    scratch_device.mapped=false;
    const bool cleanup=passed && squid_qspi_reactivate() && scratch_device.Erase(2) && scratch_device.Blank();
    Console::Printf("{\"probe\":\"candidate-b-native\",\"passed\":%s,\"scratch_offset\":%u,\"records\":%u,\"cleanup\":%s,\"elapsed_ms\":%u,\"lookup_min_us\":%u,\"lookup_max_us\":%u,\"lookup_samples_us\":[",
                    passed?"true":"false",Physical(),records,cleanup?"true":"false",k_uptime_get_32()-start,minimum,maximum);
    for(unsigned i=0;i<32;++i) Console::Printf("%s%u",i?",":"",timings[i]);
    Console::Printf("]}\n\n");
}
