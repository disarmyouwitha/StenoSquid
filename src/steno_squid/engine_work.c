#include "engine_work.h"
#include <zephyr/init.h>

/* Engine construction and personal-store maintenance share one low-priority
 * queue. Neither uses ZMK's key-delivery queue or the small HID command stack. */
K_THREAD_STACK_DEFINE(engine_stack, 8192);
struct k_work_q steno_squid_engine_queue;
static int start_engine_queue(void) {
    k_work_queue_start(&steno_squid_engine_queue, engine_stack,
                      K_THREAD_STACK_SIZEOF(engine_stack),
                      K_LOWEST_APPLICATION_THREAD_PRIO, NULL);
    return 0;
}
SYS_INIT(start_engine_queue, APPLICATION, 90);
