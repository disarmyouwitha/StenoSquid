#include "lapwing_flash_internal.h"
#include "diag.h"
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
#include "battery_log.h"
#endif
#include <zephyr/drivers/uart.h>

const struct device *const lf_console = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

bool lf_serial_poll(nrfx_qspi_config_t *config, bool *qspi_ok, bool *mapping_failed) {
    uint8_t command;
    if (uart_poll_in(lf_console, &command) != 0) {
        return false;
    }
#ifdef CONFIG_ZMK_JAVELIN_DIAG
    switch (command) {
    case 'S': lf_print_flash_status(); break;
    case 'D': diag_dump_log(lf_console); break;
    case 'Z': diag_clear(lf_console); break;
    case 'R':
    case 'T':
        k_mutex_lock(&flash_lock, K_FOREVER);
        lf_run_bench(command, config, qspi_ok, mapping_failed);
        k_mutex_unlock(&flash_lock);
        break;
    default: break;
    }
#endif
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
    if (command == 'B') {
        steno_squid_battery_log_export(lf_console);
    }
#endif
    return true;
}
