#include "dictionary_flash_internal.h"
#include "dictionary_benchmark.h"
#include "flash_diagnostics.h"
#include "w25q_quad.h"
#include "native_store_runtime.h"

#include <string.h>
#include <zephyr/drivers/pinctrl.h>
#ifdef CONFIG_ZMK_STENO_SQUID_BOOT_HFXO
#include <hal/nrf_clock.h>
#include <zephyr/drivers/clock_control/nrf_clock_control.h>
#include <zephyr/sys/onoff.h>

static int request_hfxo(struct onoff_manager *manager, struct onoff_client *client) {
    sys_notify_init_spinwait(&client->notify);
    int result = onoff_request(manager, client);
    if (result < 0) return result;

    const int64_t deadline = k_uptime_get() + 50;
    do {
        const bool completed = sys_notify_fetch_result(&client->notify, &result) == 0;
        if (completed && result < 0) return result;
        nrf_clock_hfclk_t source;
        if (nrf_clock_is_running(NRF_CLOCK, NRF_CLOCK_DOMAIN_HFCLK, &source) &&
            source == NRF_CLOCK_HFCLK_HIGH_ACCURACY) return 0;
        k_usleep(100);
    } while (k_uptime_get() < deadline);
    (void)onoff_cancel_or_release(manager, client);
    return -ETIMEDOUT;
}
#endif

/* The last chip ID read, for the status command. */
uint8_t lf_last_jedec[3];
uint32_t lf_flash_bytes;
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
uint32_t steno_squid_benchmark_jedec(void) {
    return (uint32_t)lf_last_jedec[0] << 16 | (uint32_t)lf_last_jedec[1] << 8 | lf_last_jedec[2];
}
#endif
#if SERIAL_CONSOLE
uint32_t lf_last_check_ms;
#endif

static bool start_qspi(nrfx_qspi_config_t *config) {
    if (!gpio_is_ready_dt(&lf_power) || gpio_pin_configure_dt(&lf_power, GPIO_OUTPUT_ACTIVE) != 0) {
        squid_flash_diag_log("QSPI start failed: flash power pin\n");
        return false;
    }
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
    atomic_set(&lf_powered, 1);
#endif
    k_msleep(CONFIG_ZMK_JAVELIN_STENO_FLASH_SETTLE_MS);
    if (lf_pins(PINCTRL_STATE_DEFAULT) != 0 ||
        nrfx_qspi_init(config, NULL, NULL) != NRFX_SUCCESS ||
        nrfx_qspi_activate(true) != NRFX_SUCCESS) {
        squid_flash_diag_log("QSPI start failed: pins, init or activate\n");
        return false;
    }

    const nrf_qspi_cinstr_conf_t read_id = {
        .opcode = 0x9f, .length = NRF_QSPI_CINSTR_LEN_4B, .io2_level = true, .io3_level = true,
    };
    uint8_t id[3];
    if (nrfx_qspi_cinstr_xfer(&read_id, NULL, id) != NRFX_SUCCESS) {
        squid_flash_diag_log("QSPI start failed: JEDEC read\n");
        return false;
    }
    memcpy(lf_last_jedec, id, sizeof(lf_last_jedec));
    squid_flash_diag_log("Lapwing JEDEC ID: %02x %02x %02x\n", id[0], id[1], id[2]);
    const bool supported = id[0] == 0xef && id[1] == 0x40 && (id[2] == 0x18 || id[2] == 0x20);
#ifdef CONFIG_ZMK_STENO_SQUID_BOOT_PROFILE
    if (!supported) {
        steno_squid_boot_stage("qspi_unsupported_jedec", (uint32_t)id[0] << 16 | (uint32_t)id[1] << 8 | id[2]);
    }
#endif
    return supported;
}

/* Four data lines for every read that follows, raw or mapped. */
static bool enable_quad_reads(nrfx_qspi_config_t *config) {
    if (w25q128_enable_volatile_quad() != NRFX_SUCCESS) {
        return false;
    }
    config->prot_if.readoc = NRF_QSPI_READOC_READ4O;
    return nrfx_qspi_reconfigure(config) == NRFX_SUCCESS &&
           nrfx_qspi_activate(true) == NRFX_SUCCESS;
}

/* Set when the last prepare_flash failed before it could read the images:
 * the chip did not answer, or four-line reads did not switch on. Those can
 * pass on a second try. Blank or mismatched images cannot, so they are not
 * retried.
 */
static bool transient_fault;

static bool prepare_flash(nrfx_qspi_config_t *config, bool *mapping_failed) {
    *mapping_failed = false;
    transient_fault = false;
    lf_flash_bytes = 0;
    config->prot_if.readoc = NRF_QSPI_READOC_FASTREAD;
    config->prot_if.addrmode = NRF_QSPI_ADDRMODE_24BIT;
#ifdef CONFIG_ZMK_STENO_SQUID_BOOT_PROFILE
    steno_squid_boot_stage("qspi_start", 0);
#endif
    if (!start_qspi(config)) {
        transient_fault = true;
        return false;
    }
    /* The raw CRC pass reads every byte of both bases. On one data line it
     * was most of the cold-boot wait, so switch to four lines first.
     */
#ifdef CONFIG_ZMK_STENO_SQUID_BOOT_PROFILE
    steno_squid_boot_stage("quad_setup", lf_last_jedec[2]);
#endif
    const bool large = lf_last_jedec[2] == 0x20;
    if (large) {
        if (w25q_enable_4byte_addressing() != NRFX_SUCCESS) {
            transient_fault = true;
            return false;
        }
        config->prot_if.addrmode = NRF_QSPI_ADDRMODE_32BIT;
    }
    if (!enable_quad_reads(config)) {
        *mapping_failed = true;
        transient_fault = true;
        return false;
    }
    lf_flash_bytes = (large ? 64u : 16u) * 1024u * 1024u;
    *mapping_failed = !steno_squid_native_flash_open();
    atomic_set(&lf_mapped_ready, !*mapping_failed);
    return true;
}

#define BRING_UP_ATTEMPTS 3

/* Takes the rail down for half a second, so the next attempt starts from a
 * chip that was off. With the optional 1 uF and 100 k bleed resistor on the
 * Steno Squid, the rail takes about 0.3 s to fall below 0.3 V. Holds
 * flash_lock through its caller.
 */
static void power_cycle_flash(void) {
    const bool initialized = nrfx_qspi_init_check();

    if (initialized) {
        (void)squid_qspi_reactivate();
    }
    (void)lf_power_down_flash(initialized);
    squid_flash_diag_note(DIAG_POWER_CYCLE);
    k_msleep(500);
}

/* A start-up that fails before it reads the images is tried again, a few
 * times, with a power cycle between. Otherwise one glitch, such as a rail
 * still settling after a wake, would leave Javelin without dictionaries until
 * the next reboot.
 */
static bool prepare_flash_with_retry(nrfx_qspi_config_t *config, bool *mapping_failed) {
    bool qspi_ok = false;

    for (int attempt = 1; attempt <= BRING_UP_ATTEMPTS; attempt++) {
        qspi_ok = prepare_flash(config, mapping_failed);
        if (atomic_get(&lf_mapped_ready) || !transient_fault || attempt == BRING_UP_ATTEMPTS) {
            break;
        }
        squid_flash_diag_note(DIAG_RETRY);
        squid_flash_diag_log("External flash did not start (attempt %d of %d); trying again\n", attempt,
                 BRING_UP_ATTEMPTS);
        power_cycle_flash();
    }
    return qspi_ok;
}

/* The Native store validates the catalog and owns the warm-resume and base CRC checks. */
bool lf_bring_up_flash(nrfx_qspi_config_t *config, bool *mapping_failed) {
    const int64_t started = k_uptime_get();
    atomic_clear(&lf_checked);
#ifdef CONFIG_ZMK_STENO_SQUID_BOOT_HFXO
    // Nordic anomaly 244: switching HFCLK sources during QSPI can corrupt data.
    struct onoff_manager *clock = z_nrf_clock_control_get_onoff(CLOCK_CONTROL_NRF_SUBSYS_HF);
    struct onoff_client hfxo;
    const int clock_result = clock ? request_hfxo(clock, &hfxo) : -ENODEV;
    steno_squid_boot_stage(clock_result == 0 ? "hfxo_held" : "hfxo_error", (uint32_t)clock_result);
    const bool qspi_ok = prepare_flash_with_retry(config, mapping_failed);
    if (clock_result == 0) (void)onoff_cancel_or_release(clock, &hfxo);
#else
    const bool qspi_ok = prepare_flash_with_retry(config, mapping_failed);
#endif
    lf_last_open_ok = atomic_get(&lf_mapped_ready) != 0;
    squid_flash_diag_note(DIAG_FULL_CHECK);
    if (!atomic_get(&lf_mapped_ready)) {
        squid_flash_diag_note(DIAG_UNAVAILABLE);
    }
    squid_flash_diag_log("External flash %s after Native open in %d ms\n",
             atomic_get(&lf_mapped_ready) ? "mapped" : "unavailable",
             (int)(k_uptime_get() - started));
    lf_suspended = false;
#if IDLE_OFF
    lf_note_use();
    atomic_clear(&lf_idle_off);
#endif
    lf_provisioning_ready = qspi_ok;
    atomic_set(&lf_checked, 1);
#ifdef CONFIG_ZMK_STENO_SQUID_BOOT_PROFILE
    steno_squid_boot_stage(atomic_get(&lf_mapped_ready) ? "flash_ready" : "flash_unavailable",
                           qspi_ok);
#endif
    k_condvar_broadcast(&lf_restart_done);
    dictionary_flash_ready_changed();
#if SERIAL_CONSOLE
    lf_last_check_ms = (uint32_t)(k_uptime_get() - started);
#endif
    return qspi_ok;
}
