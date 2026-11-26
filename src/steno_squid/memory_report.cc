// Measured, not estimated: the high-water marks come from stack fill patterns (CONFIG_INIT_STACKS) and
// the system heap's own peak counter, so they cover everything that has run since boot. Read them
// after the workload under test (typing replay, a transfer, Bluetooth traffic), then reboot to reset.
#include "memory_report.h"
#ifdef CONFIG_ZMK_STENO_SQUID_MEMORY_DIAGNOSTICS

#include "console.h"

#include <stddef.h>
#include <stdint.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/sys_heap.h>

extern "C" {
extern struct k_heap _system_heap;
extern char _end[]; // First byte after the static RAM image; the libc heap starts just above it.
// Private Zephyr helper, also used by its own thread analyzer for the interrupt stack.
int z_stack_space_get(const uint8_t *stack_start, size_t size, size_t *unused_ptr);
}
K_KERNEL_STACK_ARRAY_DECLARE(z_interrupt_stacks, CONFIG_MP_MAX_NUM_CPUS, CONFIG_ISR_STACK_SIZE);

namespace {
struct Walk {
    bool first = true;
};

void Row(const char *name, size_t size, size_t unused, Walk &walk) {
    Console::Printf("%s{\"name\":\"%s\",\"size\":%u,\"used\":%u}", walk.first ? "" : ",", name,
                    unsigned(size), unsigned(size - unused));
    walk.first = false;
}

// The engine's malloc/calloc/realloc use the libc heap, not _system_heap. With
// CONFIG_COMMON_LIBC_MALLOC_ARENA_SIZE=-1 its arena is every byte of SRAM above _end, and Zephyr keeps the
// sys_heap that manages it static (lib/libc/common/source/stdlib/malloc.c). sys_heap_init() puts the heap header at
// the first 8-byte boundary of the arena, so a sys_heap pointing there reads the same live counters. Plausibility is
// checked (free + allocated must account for nearly the whole arena); if it fails we report ok:0 instead of numbers.
bool LibcHeapStats(struct sys_memory_stats &stats, size_t &arena_bytes) {
#if defined(CONFIG_COMMON_LIBC_MALLOC) && CONFIG_COMMON_LIBC_MALLOC_ARENA_SIZE < 0 && !defined(CONFIG_USERSPACE)
    const uintptr_t sram_end = uintptr_t(CONFIG_SRAM_BASE_ADDRESS) + size_t(CONFIG_SRAM_SIZE) * 1024u;
    const uintptr_t base = ROUND_UP(reinterpret_cast<uintptr_t>(_end), sizeof(double));
    if (base >= sram_end) return false;
    arena_bytes = sram_end - base;
    struct sys_heap view{};
    view.heap = reinterpret_cast<struct z_heap *>(base);
    if (sys_heap_runtime_stats_get(&view, &stats) != 0) return false;
    const size_t accounted = stats.free_bytes + stats.allocated_bytes;
    return accounted <= arena_bytes && accounted + 512 >= arena_bytes && stats.max_allocated_bytes >= stats.allocated_bytes &&
           stats.max_allocated_bytes <= arena_bytes;
#else
    (void)stats; (void)arena_bytes;
    return false;
#endif
}

void ThreadRow(const struct k_thread *thread, void *user) {
    size_t unused = 0;
    if (k_thread_stack_space_get(thread, &unused) != 0) return;
    const char *name = k_thread_name_get(const_cast<k_tid_t>(thread));
    Row(name && *name ? name : "(unnamed)", thread->stack_info.size, unused, *static_cast<Walk *>(user));
}
} // namespace

void steno_squid_memory_report(void *, const char *) {
    struct sys_memory_stats heap{};
    const int heap_rc = sys_heap_runtime_stats_get(&_system_heap.heap, &heap);
    struct sys_memory_stats libc{};
    size_t libc_arena = 0;
    const bool libc_ok = LibcHeapStats(libc, libc_arena);
    Console::Printf("{\"version\":2,\"uptime_ms\":%lld,\"heap\":{\"ok\":%d,\"size\":%u,\"allocated\":%u,"
                    "\"free\":%u,\"max_allocated\":%u},",
                    static_cast<long long>(k_uptime_get()), heap_rc == 0 ? 1 : 0, unsigned(CONFIG_HEAP_MEM_POOL_SIZE),
                    unsigned(heap.allocated_bytes), unsigned(heap.free_bytes), unsigned(heap.max_allocated_bytes));
    Console::Printf("\"libc\":{\"ok\":%d,\"size\":%u,\"allocated\":%u,\"free\":%u,\"max_allocated\":%u},\"stacks\":[",
                    libc_ok ? 1 : 0, unsigned(libc_arena), unsigned(libc.allocated_bytes), unsigned(libc.free_bytes),
                    unsigned(libc.max_allocated_bytes));
    Walk walk;
    k_thread_foreach(ThreadRow, &walk);
    for (int cpu = 0; cpu < CONFIG_MP_MAX_NUM_CPUS; ++cpu) {
        size_t unused = 0;
        if (z_stack_space_get(reinterpret_cast<const uint8_t *>(K_KERNEL_STACK_BUFFER(z_interrupt_stacks[cpu])),
                              K_KERNEL_STACK_SIZEOF(z_interrupt_stacks[cpu]), &unused) == 0) {
            Row("ISR", K_KERNEL_STACK_SIZEOF(z_interrupt_stacks[cpu]), unused, walk);
        }
    }
    Console::Printf("]}\n\n");
}

#endif