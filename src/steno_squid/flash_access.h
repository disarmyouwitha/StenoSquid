#pragma once

// Shared with the native contention/wake test. Included once by flash PM.
/* Ask the worker to power an idle flash back up, and wait for the result. */
static bool wake_idle_flash(void) {
#if IDLE_OFF
    if (!atomic_get(&lf_idle_off)) {
        return false;
    }
    lf_request_restart();
    if (k_current_get() == k_work_queue_thread_get(&k_sys_work_q)) return false;
    k_mutex_lock(&flash_lock, K_FOREVER);
    while (atomic_get(&lf_idle_off)) {
        if (k_condvar_wait(&lf_restart_done, &flash_lock, K_SECONDS(2)) != 0) break;
    }
    const bool ready = atomic_get(&lf_mapped_ready) != 0;
    k_mutex_unlock(&flash_lock);
    return ready;
#else
    return false;
#endif
}

bool lapwing_flash_acquire(void) {
    /* The second pass covers an idle power-down that won the lock first. */
    for (int attempt = 0; attempt < 2; attempt++) {
#if IDLE_OFF
        lf_note_use();
#endif
        if (!atomic_get(&lf_mapped_ready) && !wake_idle_flash()) {
            return false;
        }
        const bool key_thread = k_current_get() == k_work_queue_thread_get(&k_sys_work_q);
        if (k_mutex_lock(&flash_lock, key_thread ? K_MSEC(10) : K_FOREVER) != 0) return false;
        if (atomic_get(&lf_mapped_ready)) {
            return true;
        }
        k_mutex_unlock(&flash_lock);
    }
    return false;
}

void lapwing_flash_release(void) {
    k_mutex_unlock(&flash_lock);
}

