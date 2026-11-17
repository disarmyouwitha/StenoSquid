#include "lapwing_flash_internal.h"
#include "diag.h"
#include "w25q_quad.h"

#if SERIAL_PROVISIONING && defined(CONFIG_ZMK_JAVELIN_DIAG)
#include <hal/nrf_gpio.h>

/* The S command: the flash lifecycle state, then the counters. */
void lf_print_flash_status(void) {
    int suspended = 0;
    int idle = 0;
    int holding = 0;
#ifdef CONFIG_ZMK_SLEEP
    suspended = lf_suspended;
#endif
#if IDLE_OFF
    idle = atomic_get(&lf_idle_off) != 0;
    holding = atomic_get(&lf_held) != 0;
#endif
    diag_printf(lf_console, "FLASH mapped=%d checked=%d suspended=%d idle_off=%d steno_hold=%d",
                (int)(atomic_get(&lf_mapped_ready) != 0), (int)(atomic_get(&lf_checked) != 0),
                suspended, idle, holding);
    diag_printf(lf_console, "FLASH jedec=%02x %02x %02x (expected ef 40 18)", lf_last_jedec[0],
                lf_last_jedec[1], lf_last_jedec[2]);
    diag_printf(lf_console, "FLASH last check %s %u ms, trusted since power loss=%d, saved record=%d",
                lf_last_check_quick ? "quick" : "full", lf_last_check_ms, (int)lf_trusted,
                (int)lf_stored_trust());
#if IDLE_OFF
    diag_printf(lf_console, "FLASH idle power-down on, after %u s",
                (uint32_t)CONFIG_ZMK_JAVELIN_STENO_FLASH_IDLE_SECONDS);
#else
    diag_printf(lf_console, "FLASH idle power-down off");
#endif
    for (size_t i = 0; i < ARRAY_SIZE(lf_images); i++) {
        diag_printf(lf_console, "IMAGE %s verified=%d bytes=%u crc=%08x", lf_images[i].name,
                    (int)lf_images[i].verified, lf_images[i].bytes, lf_images[i].crc);
    }
    diag_print_power(lf_console);
    diag_print_status(lf_console);
    diag_printf(lf_console, "END");
}

#ifdef CONFIG_ZMK_SLEEP
/* Bring-up bench: the R and T serial commands. Both hold flash_lock, take the
 * flash down, and leave it powered and checked again when they finish. They
 * can run for tens of seconds, and dictionary lookups wait meanwhile.
 */
#if CONFIG_ZMK_JAVELIN_DIAG_RAIL_SENSE_PIN >= 0
#define RAIL_SENSE_PIN CONFIG_ZMK_JAVELIN_DIAG_RAIL_SENSE_PIN
#else
#include <zephyr/dt-bindings/pinctrl/nrf-pinctrl.h>
#define RAIL_SENSE_PIN (DT_PROP_BY_IDX(DT_CHILD(DT_PHANDLE_BY_IDX(QSPI_NODE, pinctrl_0, 0), bus), psels, 0) & NRF_PIN_MSK)
#endif
#define BENCH_CYCLES 10

static void take_flash_down(void) {
    const bool initialized = nrfx_qspi_init_check();

    if (initialized) {
        (void)reactivate_after_xip();
    }
    (void)lf_power_down_flash(initialized);
}

static void bring_flash_back(nrfx_qspi_config_t *config, bool *qspi_ok, bool *mapping_failed) {
    *qspi_ok = lf_bring_up_flash(config, mapping_failed);
    lf_suspended = false;
#if IDLE_OFF
    lf_note_use();
    atomic_clear(&lf_idle_off);
#endif
    lapwing_flash_ready_changed();
}

/* Microseconds until the sense pin reads the wanted level, or -1 on timeout.
 * The counter ticks at 32768 Hz, so the resolution is about 30 us.
 */
static int32_t wait_for_sense(bool wanted, uint32_t timeout_ms) {
    const uint32_t start = k_cycle_get_32();

    for (;;) {
        const uint32_t elapsed = k_cyc_to_us_floor32(k_cycle_get_32() - start);

        if ((nrf_gpio_pin_read(RAIL_SENSE_PIN) != 0) == wanted) {
            return (int32_t)elapsed;
        }
        if (elapsed >= timeout_ms * 1000U) {
            return -1;
        }
        if (elapsed < 40000U) {
            k_busy_wait(20);
        } else {
            k_msleep(1);
        }
    }
}

/* R: drive P0.31 each way and watch the sense pin. With the Squid's 10 k
 * pull-up from chip select to the switched rail, the pin reads high while the
 * rail is up. It shows which level enables the rail, how fast it rises, and
 * how long it takes to fall until the pin reads low. That is a digital
 * threshold, roughly 1 V to 2 V on a 3.3 V rail, not a scope trace.
 */
static void rail_test(void) {
    diag_printf(lf_console, "RAIL test: P0.%d senses the rail, %s pin %u enables it", RAIL_SENSE_PIN, lf_power.port->name, lf_power.pin);
    take_flash_down();
    (void)gpio_pin_configure_dt(&lf_power, GPIO_OUTPUT_INACTIVE);
    nrf_gpio_cfg_input(RAIL_SENSE_PIN, NRF_GPIO_PIN_NOPULL);

    /* Which level raises the rail? Give a slow discharge three seconds. */
    gpio_pin_set_raw(lf_power.port, lf_power.pin, 0);
    k_msleep(3000);
    const bool low_reads = nrf_gpio_pin_read(RAIL_SENSE_PIN) != 0;
    gpio_pin_set_raw(lf_power.port, lf_power.pin, 1);
    k_msleep(1000);
    const bool high_reads = nrf_gpio_pin_read(RAIL_SENSE_PIN) != 0;
    diag_printf(lf_console, "RAIL sense reads %d with enable low, %d with enable high", (int)low_reads,
                (int)high_reads);

    if (high_reads && !low_reads) {
        /* The rail is up now. Switch it off and time the fall, then back on for the rise. */
        gpio_pin_set_raw(lf_power.port, lf_power.pin, 0);
        const int32_t fall = wait_for_sense(false, 3000);
        int32_t rise = -1;
        if (fall >= 0) {
            k_msleep(300);
            gpio_pin_set_raw(lf_power.port, lf_power.pin, 1);
            rise = wait_for_sense(true, 300);
        }
        diag_printf(lf_console, "RAIL high enables the rail, as the firmware assumes");
        if (rise >= 0) {
            diag_printf(lf_console, "RAIL rise: the pin read high after %d us", rise);
        }
        if (fall >= 0) {
            diag_printf(lf_console, "RAIL fall: the pin read low %d.%d ms after switch-off",
                        fall / 1000, (fall % 1000) / 100);
        } else {
            diag_printf(lf_console, "RAIL fall: the pin was still high 3 s after switch-off, so "
                                 "the rail barely discharges. Fit R2");
        }
    } else if (low_reads && !high_reads) {
        diag_printf(lf_console, "RAIL LOW enables the rail. The firmware drives it the wrong way: "
                             "change GPIO_ACTIVE_HIGH in src/steno_squid/qspi.overlay");
    } else if (!low_reads && !high_reads) {
        diag_printf(lf_console, "RAIL no rail seen at either level. Check that the 10 k from CS to "
                             "VCC is fitted, the chip is wired, and the configured GPIO is the rail enable");
    } else {
        diag_printf(lf_console, "RAIL reads high at both levels. The rail is always on, or it "
                             "needs more than 3 s to fall");
    }
    (void)gpio_pin_configure_dt(&lf_power, GPIO_OUTPUT_INACTIVE);
    diag_printf(lf_console, "RAIL END");
}

/* T: power the flash off and on again, BENCH_CYCLES times, through the same
 * start-up path a wake uses, with the same 500 ms off time as a retry.
 */
static void power_cycle_bench(nrfx_qspi_config_t *config, bool *qspi_ok, bool *mapping_failed) {
    uint32_t good = 0;
    uint32_t max_ms = 0;
    uint32_t total_ms = 0;
    const uint32_t retries = diag_session_count(DIAG_RETRY);
    const uint32_t fallbacks = diag_session_count(DIAG_QUICK_FALLBACK);

    for (uint32_t cycle = 1; cycle <= BENCH_CYCLES; cycle++) {
        take_flash_down();
        k_msleep(500);
        const int64_t started = k_uptime_get();
        bring_flash_back(config, qspi_ok, mapping_failed);
        const uint32_t ms = (uint32_t)(k_uptime_get() - started);
        const bool mapped = atomic_get(&lf_mapped_ready) != 0;

        diag_printf(lf_console, "BENCH %u of %u: %s, %s check, %u ms", cycle, BENCH_CYCLES,
                    mapped ? "mapped" : "FAILED", lf_last_check_quick ? "quick" : "full", ms);
        good += mapped;
        total_ms += ms;
        max_ms = MAX(max_ms, ms);
    }
    diag_printf(lf_console, "BENCH %u of %u mapped, mean %u ms, max %u ms", good, BENCH_CYCLES,
                total_ms / BENCH_CYCLES, max_ms);
    diag_printf(lf_console, "BENCH start-up retries %u, quick checks that fell back to full %u",
                diag_session_count(DIAG_RETRY) - retries,
                diag_session_count(DIAG_QUICK_FALLBACK) - fallbacks);
    diag_printf(lf_console, "BENCH END");
}

void lf_run_bench(uint8_t command, nrfx_qspi_config_t *config, bool *qspi_ok,
                  bool *mapping_failed) {
    if (command == 'R') {
        rail_test();
    }
    if (command == 'T') {
        power_cycle_bench(config, qspi_ok, mapping_failed);
    } else {
        /* The rail test leaves the flash off. Start it again. */
        bring_flash_back(config, qspi_ok, mapping_failed);
    }
}
#endif /* CONFIG_ZMK_SLEEP */

#endif /* SERIAL_PROVISIONING && CONFIG_ZMK_JAVELIN_DIAG */
