#pragma once
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
extern bool squid_bench_count_reads;
extern uint32_t squid_bench_xip_words, squid_bench_personal_reads, squid_bench_personal_bytes;
uint32_t steno_squid_benchmark_jedec(void);
#endif
#ifdef __cplusplus
}
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
void steno_squid_dictionary_benchmark(void *, const char *);
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
void steno_squid_replay_diagnostic_status(char *reply, size_t bytes, uint32_t store_fault);
#endif
class StenoSquidKeyBenchmark {
public:
    StenoSquidKeyBenchmark(int index, bool pressed);
    ~StenoSquidKeyBenchmark();
    void Dropped();
    void Processed() { outcome = 1; }
private:
    uint32_t started = 0, outline = 0;
    bool complete = false, failed = false, tracking = false;
    uint8_t outcome = 2;
};
#endif
#endif
