/* Read-only Phase-0 instrumentation. No dictionary writes or selection changes. */
#include "dictionary_benchmark.h"
#include "console.h"
#include "dictionary_flash.h"
#include "engine.h"
#include "lapwing_dictionary.h"
#include "user_dictionary_store.h"
#include "stroke_list_parser.h"
#include "steno_key_state.h"
#ifndef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
#include "flash_diagnostics_build_id.h"
#endif
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <nrfx.h>
#include <string.h>

bool squid_bench_count_reads;
uint32_t squid_bench_xip_words, squid_bench_personal_reads, squid_bench_personal_bytes;
namespace {
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
constexpr unsigned CAPACITY = 320;
#else
constexpr unsigned CAPACITY = 128;
#endif
constexpr unsigned REPETITIONS = 32;
struct Sample { uint32_t outline, cycles, uptime_ms; uint8_t outcome; };
Sample samples[CAPACITY];
struct k_spinlock trace_lock;
atomic_t capturing;
unsigned count, overflow;
uint64_t held, chord;
bool chord_failed;
bool owned_dwt, owned_trace;
void timer_start() {
    owned_trace = !(CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk);
    owned_dwt = !(DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk);
    CoreDebug->DEMCR = CoreDebug->DEMCR | CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL = DWT->CTRL | DWT_CTRL_CYCCNTENA_Msk;
    __DSB(); __ISB();
}
void timer_stop() {
    if (owned_dwt) DWT->CTRL = DWT->CTRL & ~DWT_CTRL_CYCCNTENA_Msk;
    if (owned_trace) CoreDebug->DEMCR = CoreDebug->DEMCR & ~CoreDebug_DEMCR_TRCENA_Msk;
}
uint32_t cycles() {
    __asm__ volatile("" ::: "memory");
    const uint32_t value=DWT->CYCCNT;
    __asm__ volatile("" ::: "memory");
    return value;
}
StenoDictionary *target(const char *name) {
    if (!strcmp(name,"lapwing")) return lapwing_dictionary();
    if (!strcmp(name,"plover")) return plover_dictionary();
    if (!strcmp(name,"personal")) return steno_squid_user_dictionary_open();
    if (!strcmp(name,"stack")) return &StenoEngine::container.value.GetDictionary();
    for (unsigned i=0;i<EXTRAS_DICTIONARY_COUNT;i++) {
        const char *names[]={"commands","numbers","uk","proper"};
        if (!strcmp(name,names[i])) return extras_dictionary(i);
    }
    return nullptr;
}
void reset_reads() { squid_bench_xip_words=squid_bench_personal_reads=squid_bench_personal_bytes=0; }
}

#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
#include "dictionary_replay.inc"
#endif

StenoSquidKeyBenchmark::StenoSquidKeyBenchmark(int index, bool pressed) {
    if (!atomic_get(&capturing) || index<0 || index>=64) return;
    started=cycles();
    auto key=k_spin_lock(&trace_lock);
    tracking=atomic_get(&capturing);
    if (tracking) {
        const uint64_t bit=uint64_t(1)<<index;
        if (pressed) { held|=bit;chord|=bit; }
        else held&=~bit;
        complete=!pressed && !held && chord;
        if (complete) {
            outline=StenoKeyState(chord).ToStroke().GetKeyState();
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
            atomic_set(&processing_outline, outline);
#endif
            failed=chord_failed;
            chord=0;chord_failed=false;
        }
    }
    k_spin_unlock(&trace_lock,key);
}
void StenoSquidKeyBenchmark::Dropped() {
    failed=true;
    if (tracking && !complete) {
        auto key=k_spin_lock(&trace_lock);chord_failed=true;k_spin_unlock(&trace_lock,key);
    }
}
StenoSquidKeyBenchmark::~StenoSquidKeyBenchmark() {
    if (!tracking || !complete) return;
    const uint32_t elapsed=cycles()-started;
    auto key=k_spin_lock(&trace_lock);
    if (atomic_get(&capturing)) {
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
        if (ReplayingLoop()) {
            RecordLoopChord(atomic_get(&completed_chords), outline, elapsed, !failed && outcome == 1);
        } else
#endif
        {
            if (count<CAPACITY) samples[count++]={outline,elapsed,k_uptime_get_32(),uint8_t(failed?0:outcome)};
            else overflow++;
        }
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
        atomic_inc(&completed_chords);
        atomic_clear(&processing_outline);
#endif
    }
    k_spin_unlock(&trace_lock,key);
}

void steno_squid_dictionary_benchmark(void *, const char *line) {
    const char *args=strchr(line,' ');
    if (!args) { Console::Printf("ERR Expected meta, lookup, reverse or capture command\n\n");return; }
    ++args;
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
    if (ReplayCommand(args)) return;
#endif
    if (!strcmp(args,"meta")) {
        size_t free_stack=0;
        const int stack_rc=k_thread_stack_space_get(k_current_get(),&free_stack);
        Console::Printf("{\"version\":1,\"firmware\":\"%s\",\"compiler\":\"%s\",\"timer\":\"DWT CYCCNT\",\"timer_hz\":%u,\"qspi_hz\":%u,\"jedec\":%u,\"capture_capacity\":%u,\"replay_loop_capacity\":%u,\"hid_stack_free_bytes\":%d}\n\n",
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
                        "hardware-typing-replay",__VERSION__,
#else
                        JAVELIN_BUILD_ID,__VERSION__,
#endif
                        SystemCoreClock,32000000u/CONFIG_ZMK_JAVELIN_STENO_QSPI_SCK_DIVIDER,
                        steno_squid_benchmark_jedec(),CAPACITY,
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
                        REPLAY_LOOP_CAPACITY,
#else
                        0u,
#endif
                        stack_rc? -1:int(free_stack));
        return;
    }
    if (!strcmp(args,"capture start")) {
        if (atomic_get(&capturing)) { Console::Printf("ERR Capture already running\n\n");return; }
        timer_start();
        auto key=k_spin_lock(&trace_lock);
        count=overflow=0;held=chord=0;chord_failed=false;atomic_set(&capturing,1);
        k_spin_unlock(&trace_lock,key);
        Console::SendOk();return;
    }
    if (!strcmp(args,"capture stop")) {
        if (atomic_clear(&capturing)) timer_stop();
        Console::SendOk();return;
    }
    if (!strcmp(args,"capture read")) {
        if (atomic_get(&capturing)) { Console::Printf("ERR Stop capture before reading\n\n");return; }
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
        if (ReplayingLoop()) { PrintLoopCapture(); return; }
#endif
        Console::Printf("{\"timer_hz\":%u,\"overflow\":%u,\"samples\":[",SystemCoreClock,overflow);
        for (unsigned i=0;i<count;i++) {
            const auto &s=samples[i];
            Console::Printf("%s[%u,%u,%u,%u]",i?",":"",s.outline,s.cycles,s.uptime_ms,s.outcome);
        }
        Console::Printf("]}\n\n");return;
    }
    if (atomic_get(&capturing)) { Console::Printf("ERR Stop capture before lookup benchmarks\n\n");return; }
    const bool reverse=!strncmp(args,"reverse ",8);
    if (!reverse && strncmp(args,"lookup ",7)) { Console::Printf("ERR Unknown benchmark command\n\n");return; }
    args+=reverse?8:7;
    const char *space=strchr(args,' ');
    char name[16];
    if (!space || space==args || !space[1] || size_t(space-args)>=sizeof(name)) { Console::Printf("ERR Invalid benchmark target\n\n");return; }
    memcpy(name,args,space-args);name[space-args]=0;
    StenoDictionary *dictionary=target(name);
    if (!dictionary) { Console::Printf("ERR Dictionary unavailable\n\n");return; }
    StrokeListParser parser;
    if (!reverse && (!parser.Parse(space+1) || !parser.GetCount() || parser.GetCount()>16 || *parser.failureOrEnd)) {
        Console::Printf("ERR Invalid outline\n\n");return;
    }
    uint32_t elapsed[REPETITIONS], fingerprint=2166136261u;
    bool hash_result=false;
    const unsigned repetitions=reverse?1:REPETITIONS;
    unsigned hits=0;
    auto lookup=[&]() {
        if (reverse) {
            StenoReverseDictionaryLookup query(space+1);
            dictionary->ReverseLookup(query);
            return unsigned(query.results.GetCount());
        }
        // Compact dictionaries rely on their caller to bound outline length.
        // Match DictionaryList's dispatch contract before direct target lookup.
        if (parser.GetCount()>dictionary->GetMaximumOutlineLength()) return 0u;
        auto result=dictionary->Lookup(StenoDictionaryLookup(parser.GetData(),parser.GetCount()));
        unsigned found=result.IsValid();
        if (found && hash_result) for (const unsigned char *p=reinterpret_cast<const unsigned char *>(result.GetText());*p;p++) fingerprint=(fingerprint^*p)*16777619u;
        result.Destroy();return found;
    };
    timer_start();
    const uint32_t before=cycles(), bracket=cycles()-before;
    // Timings exclude JSON/HID writes and counter increments. The first sample
    // is first-in-batch, not a claim that flash/CPU caches were cold.
    for (unsigned i=0;i<repetitions;i++) { const uint32_t start=cycles();hits+=lookup();elapsed[i]=cycles()-start; }
    timer_stop();
    reset_reads();hash_result=true;squid_bench_count_reads=true;lookup();squid_bench_count_reads=false;
    Console::Printf("{\"samples_cycles\":[");
    for (unsigned i=0;i<repetitions;i++) Console::Printf("%s%u",i?",":"",elapsed[i]);
    Console::Printf("],\"hits\":%u,\"fingerprint\":%u,\"bracket_cycles\":%u,\"xip_word_loads\":%u,\"personal_read_calls\":%u,\"personal_read_bytes\":%u}\n\n",
                    hits,fingerprint,bracket,squid_bench_xip_words,squid_bench_personal_reads,squid_bench_personal_bytes);
}
