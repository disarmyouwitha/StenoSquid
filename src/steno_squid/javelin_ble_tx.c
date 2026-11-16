/* Isolate potentially blocking Bluetooth TX from the engine/flash lock.
 * One report in flight, no polling and no unbounded transmit queue. A timed
 * out report keeps its own storage until the Bluetooth stack returns.
 */
#include "javelin_hid_transport.h"
#include <errno.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/spinlock.h>

int steno_squid_javelin_ble_notify(const void *data, size_t size,
                                   uint32_t session);

struct tx_report {
    uint8_t bytes[JAVELIN_HID_REPORT_BYTES];
    uint32_t session;
};
K_MSGQ_DEFINE(tx_reports, sizeof(struct tx_report), 1, 4);
K_SEM_DEFINE(tx_done, 0, 1);
static struct k_spinlock tx_lock;
static bool tx_busy, tx_complete, tx_abandoned;
static int tx_result;

static void tx_worker(void *a, void *b, void *c) {
    struct tx_report report;
    for (;;) {
        k_msgq_get(&tx_reports, &report, K_FOREVER);
        int result = steno_squid_javelin_ble_notify(
            report.bytes, sizeof(report.bytes), report.session);
        k_spinlock_key_t key = k_spin_lock(&tx_lock);
        if (tx_abandoned) {
            tx_busy = false;
        } else {
            tx_result = result;
            tx_complete = true;
            k_sem_give(&tx_done);
        }
        k_spin_unlock(&tx_lock, key);
    }
}

int steno_squid_javelin_ble_send(const void *data, size_t size,
                                 uint32_t session) {
    if (size != JAVELIN_HID_REPORT_BYTES)
        return -EINVAL;
    k_spinlock_key_t key = k_spin_lock(&tx_lock);
    if (tx_busy) {
        k_spin_unlock(&tx_lock, key);
        return -EBUSY;
    }
    k_sem_reset(&tx_done);
    tx_busy = true;
    tx_complete = tx_abandoned = false;
    k_spin_unlock(&tx_lock, key);
    struct tx_report report = {.bytes = {0}, .session = session};
    memcpy(report.bytes, data, size);
    int result = k_msgq_put(&tx_reports, &report, K_NO_WAIT);
    if (!result)
        (void)k_sem_take(&tx_done, K_SECONDS(2));
    key = k_spin_lock(&tx_lock);
    if (result)
        tx_busy = false;
    else if (tx_complete) {
        result = tx_result;
        tx_busy = false;
    } else {
        // The sender still owns its report. Do not reuse it on a new command.
        tx_abandoned = true;
        result = -ETIMEDOUT;
    }
    k_spin_unlock(&tx_lock, key);
    return result;
}

K_THREAD_DEFINE(javelin_ble_tx, 2048, tx_worker, NULL, NULL, NULL, 6, 0, 0);
