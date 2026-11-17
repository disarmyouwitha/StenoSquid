#pragma once

/* Shared by the lapwing_flash_*.c files. The public API is lapwing_flash.h.
 *
 * lapwing_flash.c        worker loop, public API, base image table
 * lapwing_flash_power.c  flash rail and QSPI pins
 * lapwing_flash_pm.c     sleep, idle power-down and the flash lock (ZMK_SLEEP)
 * lapwing_flash_qspi.c   QSPI start-up, retry, CRC checks and mapping
 * lapwing_flash_trust.c  records that allow a quick check
 * serial_console.c serial provisioning (SERIAL_CONSOLE)
 * lapwing_flash_diag.c   status and bench commands (diag builds)
 */

#include "lapwing_flash.h"
#include "w25q_quad.h"

#include <nrfx_qspi.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/util.h>

#define QSPI_NODE DT_NODELABEL(qspi)
#define XIP_BASE DT_REG_ADDR_BY_NAME(QSPI_NODE, qspi_mm)
#define SECTOR_BYTES 4096U
#define PAGE_BYTES 256U
#define CHUNK_BYTES (2U * PAGE_BYTES)

#define SERIAL_CONSOLE IS_ENABLED(CONFIG_ZMK_JAVELIN_SERIAL_CONSOLE)
#define IDLE_OFF IS_ENABLED(CONFIG_ZMK_JAVELIN_STENO_FLASH_IDLE_OFF)
#if IDLE_OFF
#define IDLE_OFF_MS (CONFIG_ZMK_JAVELIN_STENO_FLASH_IDLE_SECONDS * 1000U)
#endif

struct base_image {
    const char *name;
    uint32_t offset;
    uint32_t bytes;
    uint32_t crc;
    bool verified;
};

#if PLOVER_BYTES > 0 && EXTRAS_BYTES > 0
#define IMAGE_COUNT 3
#elif PLOVER_BYTES > 0 || EXTRAS_BYTES > 0
#define IMAGE_COUNT 2
#else
#define IMAGE_COUNT 1
#endif

#ifdef CONFIG_ZMK_JAVELIN_STENO_HID_PROVISIONING
/* All accesses are under flash_lock. */
extern bool lf_provisioning_ready;
extern bool lf_provisioning_recheck;
#endif

/* lapwing_flash.c */
extern struct base_image lf_images[IMAGE_COUNT];
extern atomic_t lf_mapped_ready;
/* Set once a bring-up attempt has finished, whatever its result. */
extern atomic_t lf_checked;
/* Both bases passed a full CRC since this controller last lost power. */
extern bool lf_trusted;
#if IDLE_OFF
/* Set while the idle timer, and not ZMK's sleep, has the flash powered down. */
extern atomic_t lf_idle_off;
/* Set while the steno layer is selected: the idle timer leaves the flash on. */
extern atomic_t lf_held;
extern atomic_t lf_last_use;
void lf_note_use(void);
#endif

/* lapwing_flash_power.c */
extern const struct gpio_dt_spec lf_power;
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
extern atomic_t lf_powered;
#endif
int lf_pins(uint8_t state);
/* Hold flash_lock. Stops QSPI, floats its pins and switches the rail off. */
int lf_power_down_flash(bool initialized);

/* lapwing_flash_pm.c */
extern struct k_mutex flash_lock;
extern atomic_t lf_read_sessions;
/* True until the first bring-up, and again whenever the flash is powered down. */
extern bool lf_suspended;
extern atomic_t lf_restart_requested;
extern struct k_sem lf_restart_wake;
extern struct k_condvar lf_restart_done;
extern int64_t lf_last_restart;
void lf_request_restart(void);
/* Hold flash_lock. False while a flash operation is still in progress. */
bool lf_flash_is_idle(bool initialized);

/* lapwing_flash_qspi.c */
extern uint8_t lf_readback[CHUNK_BYTES];
extern uint8_t lf_last_jedec[3];
#if SERIAL_CONSOLE
extern bool lf_last_check_quick;
extern uint32_t lf_last_check_ms;
#endif
bool lf_all_verified(void);
bool lf_image_crc(const struct base_image *image, bool mapped, uint32_t *crc_out,
                  uint32_t *failed_at);
bool lf_read_image_crc(const struct base_image *image);
bool lf_enable_verified_mapping(nrfx_qspi_config_t *config, bool quick);
bool lf_bring_up_flash(nrfx_qspi_config_t *config, bool *mapping_failed);

/* Opportunistic CRC scrub; all calls hold flash_lock. */
void lf_scrub_verified(void);
void lf_scrub_invalidated(void);
void lf_scrub_step(void);
int32_t lf_scrub_wait_ms(void);

/* lapwing_flash_trust.c */
uint8_t lf_trust_marker(void);
void lf_forget_wake_trust(void);
void lf_leave_wake_trust(void);
bool lf_stored_trust(void);
void lf_store_trust(void);
bool lf_clear_trust(void);
bool lf_take_wake_trust(void);

/* serial_console.c */
#if SERIAL_CONSOLE
extern const struct device *const lf_console;
/* Handles one pending serial command. True when the worker should poll again
 * at once instead of sleeping.
 */
void lf_serial_init(void);
bool lf_serial_poll(nrfx_qspi_config_t *config, bool *qspi_ok, bool *mapping_failed);
#endif

/* lapwing_flash_diag.c */
#if SERIAL_CONSOLE && defined(CONFIG_ZMK_JAVELIN_DIAG)
void lf_print_flash_status(void);
void lf_run_bench(uint8_t command, nrfx_qspi_config_t *config, bool *qspi_ok,
                  bool *mapping_failed);
#endif
