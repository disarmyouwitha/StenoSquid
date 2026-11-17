#include "dictionary_flash_internal.h"
#include "flash_diagnostics.h"
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
#include "battery_log.h"
#endif
#include <zephyr/drivers/uart.h>

const struct device *const lf_console = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

K_MSGQ_DEFINE(diagnostic_commands, sizeof(uint8_t), 16, 1);
static void serial_rx(const struct device *dev, void *context) {
    ARG_UNUSED(context);
    if (!uart_irq_update(dev) || !uart_irq_rx_ready(dev)) return;
    uint8_t byte;
    while (uart_fifo_read(dev, &byte, 1) == 1) {
        k_msgq_put(&diagnostic_commands, &byte, K_NO_WAIT);
    }
    k_sem_give(&lf_restart_wake);
}
void lf_serial_init(void) {
    if (device_is_ready(lf_console) && uart_irq_callback_user_data_set(lf_console, serial_rx, NULL) == 0)
        uart_irq_rx_enable(lf_console);
}

bool lf_serial_poll(nrfx_qspi_config_t *config, bool *qspi_ok, bool *mapping_failed) {
    uint8_t command;
    if (k_msgq_get(&diagnostic_commands, &command, K_NO_WAIT) != 0) {
        return false;
    }
#ifdef CONFIG_ZMK_JAVELIN_DIAG
    switch (command) {
    case 'S': lf_print_flash_status(); break;
    case 'D': squid_flash_diag_dump_log(lf_console); break;
    case 'Z': squid_flash_diag_clear(lf_console); break;
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
