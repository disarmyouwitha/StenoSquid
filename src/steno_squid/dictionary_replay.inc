// Included by dictionary_benchmark.cc in the opt-in replay build.
#include "native_input.h"
#include "native_store_runtime.h"
#include "../../tools/storage/native_live_journal.h"
#include <stdlib.h>
#include <zephyr/fatal.h>
#include <zephyr/sys/reboot.h>

namespace {
uint64_t replay_keys[CAPACITY];
uint32_t replay_times[CAPACITY];
unsigned replay_count;
unsigned replay_total, replay_interval, replay_mismatches, replay_overruns, replay_saturations;
uint32_t replay_first_ms, replay_last_ms;
atomic_t replay_sent, replay_active, replay_aborted, replay_hints = ATOMIC_INIT(1);

struct ReplayFault {
    uint32_t magic, reason, pc, lr, address, unaligned_address, cfsr, hfsr, sent, completed, outline;
};
constexpr uint32_t FaultMagic = 0x53514655;
ReplayFault replay_fault __noinit;
atomic_t processing_outline, completed_chords;
struct {
    unsigned result, id, length, journal_line, journal_address, journal_status;
    uint32_t strokes[16];
} lookup_fault;

void StopReplay(k_timer *timer) {
    k_timer_stop(timer);
    if (atomic_clear(&replay_active)) dictionary_flash_read_session(false);
}

void ReplayTick(k_timer *timer) {
    if (!atomic_get(&replay_active)) return;
    const unsigned index = atomic_get(&replay_sent);
    const uint32_t now = k_uptime_get_32();
    replay_times[index % CAPACITY] = now;
    if (!index) replay_first_ms = now;
    replay_last_ms = now;
    constexpr bool transitions[] = {true, false};
    for (bool pressed : transitions) {
        for (uint64_t keys = replay_keys[index % replay_count]; keys; keys &= keys - 1) {
            if (!steno_squid_native_input_retry(__builtin_ctzll(keys), pressed, now)) {
                atomic_set(&replay_aborted, 1);
                StopReplay(timer);
                return;
            }
        }
    }
    atomic_inc(&replay_sent);
    if (index + 1 == replay_total) StopReplay(timer);
}
K_TIMER_DEFINE(replay_timer, ReplayTick, nullptr);

bool ReplayingLoop() {
    return replay_total > replay_count;
}

void RecordLoopChord(unsigned index, uint32_t outline, uint32_t elapsed, bool success) {
    const unsigned sent = atomic_get(&replay_sent);
    const bool timestamp_valid = index < sent && sent - index <= CAPACITY;
    const uint32_t delay = timestamp_valid ? k_uptime_get_32() - replay_times[index % CAPACITY] : UINT16_MAX;
    const uint32_t cycles_per_quantum = SystemCoreClock / 250000u;
    const uint32_t engine_4us = (elapsed + cycles_per_quantum - 1u) / cycles_per_quantum;
    if (!success || !timestamp_valid || index >= replay_total ||
        outline != StenoKeyState(replay_keys[index % replay_count]).ToStroke().GetKeyState())
        ++replay_mismatches;
    if (delay > UINT16_MAX || delay * 1000u >= replay_interval)
        ++replay_overruns;
    if (engine_4us > UINT16_MAX)
        ++replay_saturations;
    if (index < REPLAY_LOOP_CAPACITY) {
        trace.loop[index].engine_4us = uint16_t(MIN(engine_4us, UINT16_MAX));
        trace.loop[index].completion_ms = uint16_t(MIN(delay, UINT16_MAX));
    } else {
        ++overflow;
    }
}

void PrintLoopCapture() {
    const unsigned completed = atomic_get(&completed_chords);
    Console::Printf("{\"completed\":%u,\"overflow\":%u,\"mismatches\":%u,\"overruns\":%u,"
                    "\"engine_saturations\":%u,\"samples\":[",
                    completed, overflow, replay_mismatches, replay_overruns, replay_saturations);
    for (unsigned i = 0; i < MIN(completed, REPLAY_LOOP_CAPACITY); ++i) {
        const auto &sample = trace.loop[i];
        Console::Printf("%s[%u,%u]", i ? "," : "", 4u * sample.engine_4us, sample.completion_ms);
    }
    Console::Printf("]}\n\n");
}

bool ReplayCommand(const char *args) {
    if (strncmp(args, "replay ", 7)) return false;
    args += 7;
    if (!strcmp(args, "fault")) {
        if (replay_fault.magic != FaultMagic) {
            Console::Printf("{}\n\n");
        } else {
            Console::Printf("{\"reason\":%u,\"pc\":%u,\"lr\":%u,\"r0\":%u,\"r1\":%u,\"cfsr\":%u,\"hfsr\":%u,"
                            "\"sent\":%u,\"completed\":%u,\"outline\":%u}\n\n",
                            replay_fault.reason, replay_fault.pc, replay_fault.lr, replay_fault.address,
                            replay_fault.unaligned_address, replay_fault.cfsr, replay_fault.hfsr, replay_fault.sent,
                            replay_fault.completed, replay_fault.outline);
        }
    } else if (!strcmp(args, "status")) {
        const unsigned sent = atomic_get(&replay_sent);
        Console::Printf("{\"sent\":%u,\"aborted\":%s,\"suggestions\":%s,"
                        "\"first_ms\":%u,\"last_ms\":%u,\"times_ms\":[", sent,
                        atomic_get(&replay_aborted) ? "true" : "false",
                        atomic_get(&replay_hints) ? "true" : "false", replay_first_ms, replay_last_ms);
        if (!ReplayingLoop())
            for (unsigned i = 0; i < sent; ++i) Console::Printf("%s%u", i ? "," : "", replay_times[i]);
        Console::Printf("]}\n\n");
    } else if (!strcmp(args, "stop")) {
        StopReplay(&replay_timer);
        Console::SendOk();
    } else if (atomic_get(&replay_active) || steno_squid_native_input_pending()) {
        Console::Printf("ERR Replay is active or input is still draining\n\n");
    } else if (!strcmp(args, "clear")) {
        replay_count = 0;
        replay_total = 0;
        atomic_clear(&replay_sent);
        atomic_clear(&replay_aborted);
        Console::SendOk();
    } else if (!strcmp(args, "hints on") || !strcmp(args, "hints off")) {
        atomic_set(&replay_hints, !strcmp(args, "hints on"));
        Console::SendOk();
    } else if (!strncmp(args, "add ", 4)) {
        char *end;
        const unsigned long outline = strtoul(args + 4, &end, 16);
        uint64_t keys = 0;
        for (unsigned i = 0; i < unsigned(StenoKey::COUNT); ++i) {
            const int bit = StenoKeyState::STROKE_BIT_INDEX_LOOKUP[i];
            if (bit < 0 || !(outline & (1ul << bit))) continue;
            const uint64_t key = 1ull << i;
            if (!(StenoKeyState(keys).ToStroke().GetKeyState() & (1ul << bit))) keys |= key;
        }
        if (!keys || StenoKeyState(keys).ToStroke().GetKeyState() != outline ||
                *end || replay_count == CAPACITY) {
            Console::Printf("ERR Invalid replay chord or tape full\n\n");
        } else {
            replay_keys[replay_count++] = keys;
            Console::SendOk();
        }
    } else if (!strncmp(args, "start ", 6)) {
        char *end;
        const unsigned long interval = strtoul(args + 6, &end, 10);
        const unsigned long repeats = *end == ' ' ? strtoul(end + 1, &end, 10) : 1;
        if (*end || interval < 10000 || interval > 5000000 || !replay_count ||
                !repeats || repeats > REPLAY_LOOP_CAPACITY / replay_count ||
                atomic_get(&replay_sent) || !atomic_get(&capturing)) {
            Console::Printf("ERR Invalid replay schedule or capture not armed\n\n");
        } else {
            atomic_clear(&processing_outline);
            atomic_clear(&completed_chords);
            replay_total = replay_count * repeats;
            replay_interval = interval;
            // Loop samples reuse the individual-stroke buffer.
            if (ReplayingLoop()) count = 0;
            replay_mismatches = replay_overruns = replay_saturations = 0;
            replay_fault.magic = 0;
            dictionary_flash_read_session(true);
            atomic_set(&replay_active, 1);
            k_timer_start(&replay_timer, K_MSEC(500), K_USEC(interval));
            Console::SendOk();
        }
    } else {
        Console::Printf("ERR Unknown replay command\n\n");
    }
    return true;
}
} // namespace

bool steno_squid_replay_suggestions_enabled() { return atomic_get(&replay_hints); }

namespace candidate_b {
void ReplayLookupFault(LiveResult result, uint32_t id, const uint32_t *strokes, size_t length) {
    if (lookup_fault.length) return;
    lookup_fault.result = unsigned(result);
    lookup_fault.id = id;
    memcpy(lookup_fault.strokes, strokes, length * sizeof(*strokes));
    lookup_fault.length = length;
}
void ReplayJournalFault(unsigned line, uint32_t address, unsigned status) {
    if (lookup_fault.journal_line) return;
    lookup_fault.journal_line = line;
    lookup_fault.journal_address = address;
    lookup_fault.journal_status = status;
}
}

void steno_squid_replay_diagnostic_status(char *reply, size_t bytes, uint32_t store_fault) {
    const bool fatal = replay_fault.magic == FaultMagic;
    const auto used = snprintk(reply, bytes,
             "{\"store_fault_pc\":%u,\"fatal\":%s,\"reason\":%u,\"pc\":%u,\"lr\":%u,\"r0\":%u,\"r1\":%u,\"cfsr\":%u,\"hfsr\":%u,"
             "\"sent\":%u,\"completed\":%u,\"outline\":%u,"
             "\"lookup_result\":%u,\"lookup_id\":%u,\"journal_line\":%u,\"journal_address\":%u,"
             "\"journal_status\":%u,\"lookup_strokes\":[",
             store_fault, fatal ? "true" : "false", fatal ? replay_fault.reason : 0,
             fatal ? replay_fault.pc : 0, fatal ? replay_fault.lr : 0, fatal ? replay_fault.address : 0,
             fatal ? replay_fault.unaligned_address : 0, fatal ? replay_fault.cfsr : 0,
             fatal ? replay_fault.hfsr : 0,
             fatal ? replay_fault.sent : uint32_t(atomic_get(&replay_sent)),
             fatal ? replay_fault.completed : uint32_t(atomic_get(&completed_chords)),
             fatal ? replay_fault.outline : uint32_t(atomic_get(&processing_outline)),
             lookup_fault.result, lookup_fault.id, lookup_fault.journal_line,
             lookup_fault.journal_address, lookup_fault.journal_status);
    if (used < 0 || size_t(used) >= bytes) return;
    size_t at = used;
    for (unsigned i = 0; i < lookup_fault.length; ++i) {
        const auto extra = snprintk(reply + at, bytes - at, "%s%u", i ? "," : "", lookup_fault.strokes[i]);
        if (extra < 0 || size_t(extra) >= bytes - at) return;
        at += extra;
    }
    snprintk(reply + at, bytes - at, "]}");
}

extern "C" void k_sys_fatal_error_handler(unsigned reason, const struct arch_esf *esf) {
    replay_fault.reason = reason;
    replay_fault.pc = esf ? esf->basic.pc : 0;
    replay_fault.lr = esf ? esf->basic.lr : 0;
    replay_fault.address = esf ? esf->basic.r0 : 0;
    replay_fault.unaligned_address = esf ? esf->basic.r1 : 0;
    replay_fault.cfsr = SCB->CFSR;
    replay_fault.hfsr = SCB->HFSR;
    replay_fault.sent = atomic_get(&replay_sent);
    replay_fault.completed = atomic_get(&completed_chords);
    replay_fault.outline = atomic_get(&processing_outline);
    __DMB();
    replay_fault.magic = FaultMagic;
    sys_reboot(SYS_REBOOT_COLD);
}
