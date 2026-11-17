#include "dictionary_flash_internal.h"

#include <zephyr/drivers/pinctrl.h>

PINCTRL_DT_DEFINE(QSPI_NODE);

const struct gpio_dt_spec lf_power = GPIO_DT_SPEC_GET(DT_PATH(zephyr_user), flash_power_gpios);

int lf_pins(uint8_t state) {
    return pinctrl_apply_state(PINCTRL_DT_DEV_CONFIG_GET(QSPI_NODE), state);
}

#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
atomic_t lf_powered;

bool dictionary_flash_is_powered(void) {
    return atomic_get(&lf_powered) != 0;
}
#endif

int lf_power_down_flash(bool initialized) {
    atomic_clear(&lf_mapped_ready);
    lf_suspended = true;
    if (initialized) {
        nrfx_qspi_uninit();
    }
    int rc = lf_pins(PINCTRL_STATE_SLEEP);
    if (rc == 0) {
        rc = gpio_pin_configure_dt(&lf_power, GPIO_OUTPUT_INACTIVE);
    }
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
    if (rc == 0) {
        atomic_clear(&lf_powered);
    }
#endif
    return rc;
}
