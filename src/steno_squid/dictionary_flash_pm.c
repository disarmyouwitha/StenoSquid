#include "dictionary_flash_internal.h"
#include "flash_diagnostics.h"
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
#include "battery_log.h"
#endif

#include <zephyr/pm/device.h>

BUILD_ASSERT(IS_ENABLED(CONFIG_ZMK_SLEEP), "External flash requires sleep support");
BUILD_ASSERT(!IS_ENABLED(CONFIG_ZMK_EXT_POWER), "The QSPI driver owns the flash power pin");
K_MUTEX_DEFINE(flash_lock);
atomic_t lf_read_sessions;
bool lf_suspended = true;
atomic_t lf_restart_requested = ATOMIC_INIT(1);
K_SEM_DEFINE(lf_restart_wake, 0, 1);
K_CONDVAR_DEFINE(lf_restart_done);
int64_t lf_last_restart = -5000;

void lf_request_restart(void) {
    if (lf_suspended && atomic_get(&lf_checked) && !lf_last_open_ok && k_uptime_get() - lf_last_restart < 5000) {
        return;
    }
    if (lf_suspended) {
#if IDLE_OFF
        squid_flash_diag_wake_requested(atomic_get(&lf_idle_off) != 0);
#else
        squid_flash_diag_wake_requested(false);
#endif
    }
    atomic_clear(&lf_checked);
    atomic_set(&lf_restart_requested, 1);
    dictionary_flash_ready_changed();
    k_sem_give(&lf_restart_wake);
}

#include "flash_access.h"

bool dictionary_flash_resume_read(void) {
    k_mutex_lock(&flash_lock, K_FOREVER);
#if IDLE_OFF
    lf_note_use();
#endif
    return atomic_get(&lf_mapped_ready) != 0;
}
void dictionary_flash_read_session(bool active) {
    if (active) atomic_inc(&lf_read_sessions);
    else {
        atomic_dec(&lf_read_sessions);
        k_sem_give(&lf_restart_wake);
    }
}

/* Hold flash_lock. False while a flash operation is still in progress. */
bool lf_flash_is_idle(bool initialized) {
    return !initialized ||
           (squid_qspi_reactivate() && nrfx_qspi_mem_busy_check() == NRFX_SUCCESS);
}

static int flash_pm_action(const struct device *dev, enum pm_device_action action) {
    ARG_UNUSED(dev);
    if (action == PM_DEVICE_ACTION_RESUME) {
        // ZMK aborted power-off and rolled back device suspension. Let the
        // worker restore the same mapping before allowing new flash access.
        lf_request_restart();
        return 0;
    }
    if (action != PM_DEVICE_ACTION_SUSPEND) {
        return -ENOTSUP;
    }
    if (k_mutex_lock(&flash_lock, K_NO_WAIT) != 0) {
        // ZMK retries on its next idle check. Let any read or save finish.
        return -EBUSY;
    }
    if (atomic_get(&lf_read_sessions)) {
        k_mutex_unlock(&flash_lock);
        return -EBUSY;
    }
    int rc = 0;
    if (lf_suspended) {
#if IDLE_OFF
        /* The idle timer already powered the flash down. Sleep takes over. */
        if (!atomic_cas(&lf_idle_off, 1, 0)) {
            k_mutex_unlock(&flash_lock);
            return -EBUSY;
        }
#else
        k_mutex_unlock(&flash_lock);
        return -EBUSY;
#endif
    } else {
        const bool initialized = nrfx_qspi_init_check();
        if (!lf_flash_is_idle(initialized)) {
            k_mutex_unlock(&flash_lock);
            return -EBUSY;
        }
        rc = lf_power_down_flash(initialized);
    }
    atomic_clear(&lf_restart_requested);
    if (rc != 0) {
        // A failing suspend is not on ZMK's resume list.
        lf_request_restart();
    }
    k_mutex_unlock(&flash_lock);
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
    if (rc == 0) {
        steno_squid_battery_log_sleep();
    }
#endif
    return rc;
}

PM_DEVICE_DEFINE(steno_squid_flash, flash_pm_action);
DEVICE_DEFINE(steno_squid_flash, "STENO_SQUID_QSPI", NULL, PM_DEVICE_GET(steno_squid_flash), NULL, NULL,
              POST_KERNEL, CONFIG_KERNEL_INIT_PRIORITY_DEVICE, NULL);
