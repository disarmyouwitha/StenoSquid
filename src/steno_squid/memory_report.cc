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
    Console::Printf("{\"version\":1,\"uptime_ms\":%lld,\"heap\":{\"ok\":%d,\"size\":%u,\"allocated\":%u,"
                    "\"free\":%u,\"max_allocated\":%u},\"stacks\":[",
                    static_cast<long long>(k_uptime_get()), heap_rc == 0 ? 1 : 0, unsigned(CONFIG_HEAP_MEM_POOL_SIZE),
                    unsigned(heap.allocated_bytes), unsigned(heap.free_bytes), unsigned(heap.max_allocated_bytes));
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