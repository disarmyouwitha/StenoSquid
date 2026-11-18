#include "native_input.h"
#include <zmk/event_manager.h>
#include <zmk/events/layer_state_changed.h>
static int cancel_layer_input(const zmk_event_t *event) {
    (void)event;
    steno_squid_native_input_cancel();
    return ZMK_EV_EVENT_BUBBLE;
}
ZMK_LISTENER(steno_squid_native_input_layers, cancel_layer_input);
ZMK_SUBSCRIPTION(steno_squid_native_input_layers, zmk_layer_state_changed);
