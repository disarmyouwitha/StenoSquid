#include "javelin_hid_transport.h"
#include <zmk/usb.h>

bool steno_squid_usb_hid_ready(void) {
    return zmk_usb_is_hid_ready();
}
