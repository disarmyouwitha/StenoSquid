#!/usr/bin/env python3
"""Exercise real diagnostic commands and trace boundaries with timer/device doubles."""
from pathlib import Path
import subprocess
import tempfile
from hostlib import compiler

ROOT=Path(__file__).resolve().parents[2]
STUBS=r'''
#pragma once
#include <cassert>
#include <cstdint>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#define CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK 1
#define CONFIG_ZMK_JAVELIN_STENO_QSPI_SCK_DIVIDER 16
#define JAVELIN_BUILD_ID "fixture"
struct Counter { uint32_t value=0; operator uint32_t() { value+=64;return value; } };
struct Dwt { uint32_t CTRL=0;Counter CYCCNT; } dwt;
struct Debug { uint32_t DEMCR=0; } debug;
#define DWT (&dwt)
#define CoreDebug (&debug)
#define CoreDebug_DEMCR_TRCENA_Msk 1u
#define DWT_CTRL_CYCCNTENA_Msk 1u
#define __DSB()
#define __ISB()
uint32_t SystemCoreClock=64000000;
struct k_spinlock {};
unsigned k_spin_lock(k_spinlock *) { return 0; }
void k_spin_unlock(k_spinlock *,unsigned) {}
using atomic_t=uint32_t;
uint32_t atomic_get(atomic_t *p){return *p;}
uint32_t atomic_set(atomic_t *p,uint32_t n){auto old=*p;*p=n;return old;}
uint32_t atomic_clear(atomic_t *p){return atomic_set(p,0);}
void *k_current_get(){return nullptr;}
int k_thread_stack_space_get(void *,size_t *n){*n=2048;return 0;}
uint32_t k_uptime_get_32(){return 42;}
std::string output;
struct Console {
 static void Printf(const char *fmt,...) { char text[4096];va_list a;va_start(a,fmt);vsnprintf(text,sizeof(text),fmt,a);va_end(a);output+=text; }
 static void SendOk(){output+="OK\n\n";}
};
struct StenoStroke { uint32_t value=0;uint32_t GetKeyState()const{return value;} };
struct StenoKeyState { uint64_t value;explicit StenoKeyState(uint64_t v):value(v){} StenoStroke ToStroke(){return {uint32_t(value)};} };
struct StenoDictionaryLookup { const StenoStroke *strokes;size_t length; };
struct StenoDictionaryLookupResult { bool hit;bool IsValid()const{return hit;}const char *GetText()const{return "cat";}void Destroy(){} };
struct StenoReverseDictionaryLookup { explicit StenoReverseDictionaryLookup(const char *){} struct {size_t GetCount()const{return 2;}}results; };
struct StenoDictionary {
 size_t GetMaximumOutlineLength()const{return 1;}
 StenoDictionaryLookupResult Lookup(const StenoDictionaryLookup &q)const;
 void ReverseLookup(StenoReverseDictionaryLookup &)const;
};
StenoDictionary dict;
struct StenoEngine { struct Allocation;static Allocation container;StenoDictionary &GetDictionary(){return dict;} };
struct StenoEngine::Allocation { StenoEngine value; };
StenoEngine::Allocation StenoEngine::container;
struct StrokeListParser {
 std::vector<StenoStroke> list;const char *failureOrEnd="";
 bool Parse(const char *s){if(!strcmp(s,"KAT")){list.push_back({1});return true;}if(!strcmp(s,"KAT/KAT")){list.push_back({1});list.push_back({1});return true;}return false;}
 size_t GetCount(){return list.size();}const StenoStroke *GetData(){return list.data();}
};
'''
HARNESS=r'''
#include "stubs.h"
#include "dictionary_benchmark.cc"
StenoDictionary *lapwing_dictionary(){return &dict;}
StenoDictionary *plover_dictionary(){return &dict;}
StenoDictionary *extras_dictionary(size_t){return &dict;}
StenoDictionary *steno_squid_user_dictionary_open(){return &dict;}
uint32_t steno_squid_benchmark_jedec(){return 0xef4018;}
StenoDictionaryLookupResult StenoDictionary::Lookup(const StenoDictionaryLookup &q)const {
 if(squid_bench_count_reads){squid_bench_xip_words+=7;squid_bench_personal_reads++;squid_bench_personal_bytes+=8;}
 dwt.CYCCNT.value+=128;return {q.length!=0};
}
void StenoDictionary::ReverseLookup(StenoReverseDictionaryLookup &)const {dwt.CYCCNT.value+=128;}
void command(const char *s){output.clear();steno_squid_dictionary_benchmark(nullptr,s);}
void send_chord(bool fail=false){
 {StenoSquidKeyBenchmark press(3,true);if(fail)press.Dropped();else press.Processed();}
 {StenoSquidKeyBenchmark release(3,false);release.Processed();}
}
int main(){
 command("dictionary_benchmark meta");assert(output.find("64000000")!=std::string::npos && output.find("2000000")!=std::string::npos);
 dwt.CYCCNT.value=0xffffffc0;
 command("dictionary_benchmark lookup lapwing KAT");
 assert(output.find("\"hits\":32")!=std::string::npos);
 assert(output.find("\"xip_word_loads\":7")!=std::string::npos); // Count one untimed pass only.
 assert(output.find("\"fingerprint\":108289031")!=std::string::npos);
 assert(!(dwt.CTRL&1) && !(debug.DEMCR&1) && !squid_bench_count_reads);
 command("dictionary_benchmark lookup lapwing KAT/KAT");
 assert(output.find("\"hits\":0")!=std::string::npos && output.find("\"xip_word_loads\":0")!=std::string::npos);
 command("dictionary_benchmark lookup nope KAT");assert(output.find("ERR")!=std::string::npos);
 command("dictionary_benchmark lookup lapwing BAD");assert(output.find("ERR")!=std::string::npos);
 command("dictionary_benchmark reverse lapwing cat");assert(output.find("\"hits\":2")!=std::string::npos);
 command("dictionary_benchmark capture start");assert(output=="OK\n\n" && (dwt.CTRL&1));
 command("dictionary_benchmark capture start");assert(output.find("ERR")!=std::string::npos);
 command("dictionary_benchmark lookup lapwing KAT");assert(output.find("ERR")!=std::string::npos);
 command("dictionary_benchmark capture read");assert(output.find("ERR")!=std::string::npos);
 send_chord(true);send_chord();
 command("dictionary_benchmark capture stop");assert(!(dwt.CTRL&1));
 command("dictionary_benchmark capture read");assert(count==2 && samples[0].outcome==0 && samples[1].outcome==1);
 assert(samples[0].outline==8 && samples[0].cycles<1000);
 // Preserve an existing debugger's timer ownership.
 debug.DEMCR=1;dwt.CTRL=1;
 command("dictionary_benchmark capture start");
 for(unsigned i=0;i<CAPACITY+1;i++)send_chord();
 command("dictionary_benchmark capture stop");assert(debug.DEMCR==1 && dwt.CTRL==1);
 command("dictionary_benchmark capture read");assert(count==CAPACITY && overflow==1);
 send_chord();assert(count==CAPACITY && overflow==1);
 puts("Diagnostic benchmark: command validation, untimed read counts, timer wrap/ownership, dropped chords and trace overflow passed");
}
'''

def main():
    with tempfile.TemporaryDirectory() as tmp:
        out=Path(tmp)
        (out/'stubs.h').write_text(STUBS)
        for name in ['console.h','dictionary/dictionary.h','engine.h','stroke_list_parser.h','steno_key_state.h','flash_diagnostics_build_id.h','zephyr/kernel.h','zephyr/sys/atomic.h','nrfx.h']:
            p=out/name;p.parent.mkdir(exist_ok=True,parents=True);p.write_text('#include "stubs.h"\n')
        (out/'test.cc').write_text(HARNESS)
        subprocess.run([*compiler(),'-std=c++20','-I'+str(out),'-I'+str(ROOT/'src/steno_squid'),str(out/'test.cc'),'-o',str(out/'test')],check=True)
        subprocess.run([str(out/'test')],check=True)
if __name__=='__main__':main()
