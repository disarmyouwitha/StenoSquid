#define DT_DRV_COMPAT zmk_behavior_steno_squid_status

#include <drivers/behavior.h>
#include <lvgl.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zmk/battery.h>
#include <zmk/display.h>
#include <zmk/display/status_screen.h>
#include <zmk/events/activity_state_changed.h>
#include <zmk/events/battery_state_changed.h>
#include <zmk/events/position_state_changed.h>
#include <zmk/events/split_peripheral_status_changed.h>
#if IS_ENABLED(CONFIG_USB_DEVICE_STACK)
#include <zmk/events/usb_conn_state_changed.h>
#include <zmk/usb.h>
#endif

#include <string.h>

#include "display/screen.h"
#include "display/pet_widget.h"
#include "split_status.h"
#include "practice_types.h"
#include "stroke_feed.h"
#include "practice_view.h"

BUILD_ASSERT(DT_HAS_COMPAT_STATUS_OKAY(DT_DRV_COMPAT));
BUILD_ASSERT(__builtin_strcmp(DEVICE_DT_NAME(DT_DRV_INST(0)), STENO_SQUID_STATUS_BEHAVIOR) == 0);

/* Past the modes the left can send: show the layer name it sent instead. */
#define MODE_LAYER_NAME STENO_SQUID_MODE_COUNT

static atomic_t mode;
static atomic_t host_disconnected;
static atomic_t layer_name_low;
static atomic_t layer_name_high;
static atomic_t practice_screen;
static struct steno_squid_practice_view practice_view;
static struct steno_squid_practice_receiver practice_receiver;
K_MUTEX_DEFINE(status_lock);
static atomic_t pet_wake;
static atomic_t pet_typing;
static atomic_t pet_greeting;

#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
/* The last stroke the left half sent. Messages arrive on the Bluetooth thread
 * and the display work queue reads it.
 */
static struct steno_squid_tape tape;

static void clear_tape(void) {
    memset(&tape, 0, sizeof(tape));
}
#else
static void clear_tape(void) {}
#endif

struct status_snapshot {
    unsigned mode, practice;
    uint32_t name_low, name_high;
    bool disconnected;
    struct steno_squid_practice_view view;
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
    struct steno_squid_tape tape;
#endif
};
static void refresh_status(struct k_work *work) {
    struct status_snapshot snapshot = {0};
    k_mutex_lock(&status_lock, K_FOREVER);
    snapshot.mode = atomic_get(&mode);
    snapshot.name_low = atomic_get(&layer_name_low);
    snapshot.name_high = atomic_get(&layer_name_high);
    snapshot.practice = atomic_get(&practice_screen);
    snapshot.view = practice_view;
    snapshot.disconnected = atomic_get(&host_disconnected);
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
    snapshot.tape = tape;
#endif
    k_mutex_unlock(&status_lock);
    const unsigned shown = snapshot.mode;
    const uint32_t practice = snapshot.practice;
    char name[STENO_SQUID_LAYER_NAME_CHARS + 1] = {0};
    if (shown == MODE_LAYER_NAME) steno_squid_layer_name_decode(snapshot.name_low, snapshot.name_high, name);
    const bool wake = atomic_clear(&pet_wake);
    const bool typing = atomic_clear(&pet_typing);
    const bool practicing = (practice & STENO_SQUID_PRACTICE_ACTIVE) != 0;
#if IS_ENABLED(CONFIG_USB_DEVICE_STACK)
    steno_squid_pet_set_powered(zmk_usb_is_powered());
#endif
    if (wake && !practicing) {
        steno_squid_pet_set_state(PET_IDLE);
    }
    static struct status_snapshot previous;
    static unsigned previous_charge;
    static bool shown_once;
    const unsigned charge = zmk_battery_state_of_charge();
    const bool changed = !shown_once || charge != previous_charge || memcmp(&snapshot, &previous, sizeof(snapshot));
    if (changed) {
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
        steno_squid_screen_set_tape(&snapshot.tape);
#endif
        if (practicing) steno_squid_practice_widget_set_view(&snapshot.view);
        steno_squid_screen_update(shown, name, practice, charge, snapshot.disconnected);
        previous = snapshot;
        previous_charge = charge;
        shown_once = true;
    }
    steno_squid_practice_widget_activity(zmk_activity_get_state() == ZMK_ACTIVITY_ACTIVE);
    if (!practicing) {
        if (typing && shown == STENO_SQUID_MODE_UNKNOWN) {
            steno_squid_pet_set_state(PET_WAIT_TAP);
        } else if (zmk_activity_get_state() != ZMK_ACTIVITY_ACTIVE) {
            steno_squid_pet_set_state(PET_SLEEP);
        } else if (typing) {
            steno_squid_pet_set_state(PET_TYPING);
        }
        /* Wait for the first valid mode snapshot after connection so the
         * greeting gets the space previously used by Waiting / For Left.
         */
        if (shown != STENO_SQUID_MODE_UNKNOWN && atomic_cas(&pet_greeting, 1, 0)) {
            steno_squid_pet_set_state(PET_GREETING);
        }
    } else {
        atomic_clear(&pet_greeting);
    }
}

K_WORK_DEFINE(refresh_status_work, refresh_status);

static void queue_refresh(void) {
    if (zmk_display_is_initialized()) {
        k_work_submit_to_queue(zmk_display_work_q(), &refresh_status_work);
    }
}

#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
static void expire_wpm(struct k_work *work) {
    k_mutex_lock(&status_lock, K_FOREVER);
    tape.wpm = 0;
    k_mutex_unlock(&status_lock);
    queue_refresh();
}
K_WORK_DELAYABLE_DEFINE(wpm_expiry, expire_wpm);
#endif

static int status_received_locked(struct zmk_behavior_binding *binding,
                           struct zmk_behavior_binding_event event) {
    if (binding->param2 == STENO_SQUID_HOST_STATUS_PROTOCOL) {
        if (binding->param1 > 1) {
            return -EINVAL;
        }
        atomic_set(&host_disconnected, binding->param1);
    } else if ((binding->param2 & 0xffu) == STENO_SQUID_PRACTICE_VIEW_PROTOCOL &&
               !(binding->param2 & (STENO_SQUID_LAYER_NAME_PROTOCOL | STENO_SQUID_TAPE_TAG))) {
            const int result = steno_squid_practice_receive(&practice_receiver, binding->param1,
                                                       binding->param2, &practice_view);
        if (result == 1) atomic_set(&practice_screen, practice_view.state);
            if (result < 0) return -EINVAL;
        if (!result) return ZMK_BEHAVIOR_OPAQUE;
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
    } else if ((binding->param2 & STENO_SQUID_TAPE_TAG) &&
               !(binding->param2 & STENO_SQUID_LAYER_NAME_PROTOCOL)) {
            struct steno_squid_tape next = tape;
        const int kind = steno_squid_tape_decode(binding->param1, binding->param2, &next);
        if (kind >= 0) {
            tape = next;
            if (kind == STENO_SQUID_TAPE_KIND_STROKE) k_work_reschedule(&wpm_expiry, K_SECONDS(15));
        }
            if (kind == STENO_SQUID_TAPE_INVALID) {
            return -EINVAL;
        }
        if (kind == STENO_SQUID_TAPE_STALE) {
            return ZMK_BEHAVIOR_OPAQUE;
        }
#endif
    } else if (binding->param2 & STENO_SQUID_LAYER_NAME_PROTOCOL) {
        /* The left half repeats its status every minute. A repeat must not
         * wipe the stroke tape, only a change of mode, layer name or practice.
         */
        const uint32_t low = binding->param1;
        const uint32_t high = binding->param2 & ~STENO_SQUID_LAYER_NAME_PROTOCOL;
        const bool same = atomic_get(&mode) == MODE_LAYER_NAME &&
                          (uint32_t)atomic_get(&layer_name_low) == low &&
                          (uint32_t)atomic_get(&layer_name_high) == high;
        const bool was_practicing =
            ((uint32_t)atomic_clear(&practice_screen) & STENO_SQUID_PRACTICE_ACTIVE) != 0;
        atomic_set(&layer_name_low, low);
        atomic_set(&layer_name_high, high);
        atomic_set(&mode, MODE_LAYER_NAME);
        if (!same || was_practicing) {
            clear_tape();
        }
    } else if (binding->param2 == STENO_SQUID_STATUS_PROTOCOL &&
               binding->param1 < STENO_SQUID_MODE_COUNT) {
        const bool was_practicing =
            ((uint32_t)atomic_clear(&practice_screen) & STENO_SQUID_PRACTICE_ACTIVE) != 0;
        const atomic_val_t old_mode = atomic_set(&mode, binding->param1);
        if (was_practicing || old_mode != (atomic_val_t)binding->param1) {
            clear_tape();
        }
    } else {
        return -EINVAL;
    }
    queue_refresh();
    return ZMK_BEHAVIOR_OPAQUE;
}

static int status_received(struct zmk_behavior_binding *binding,
                           struct zmk_behavior_binding_event event) {
    k_mutex_lock(&status_lock, K_FOREVER);
    const int result = status_received_locked(binding, event);
    k_mutex_unlock(&status_lock);
    return result;
}

static const struct behavior_driver_api status_driver = {
    .binding_pressed = status_received,
};

BEHAVIOR_DT_INST_DEFINE(0, NULL, NULL, NULL, NULL, POST_KERNEL,
                        CONFIG_KERNEL_INIT_PRIORITY_DEFAULT, &status_driver);

static int display_event(const zmk_event_t *event) {
    const struct zmk_position_state_changed *position = as_zmk_position_state_changed(event);
    if (position) {
        if (position->source == ZMK_POSITION_STATE_CHANGE_SOURCE_LOCAL && position->state) {
            atomic_set(&pet_typing, 1);
            queue_refresh();
        }
        return ZMK_EV_EVENT_BUBBLE;
    }
    const struct zmk_activity_state_changed *activity = as_zmk_activity_state_changed(event);
    if (activity && activity->state == ZMK_ACTIVITY_ACTIVE) {
        atomic_set(&pet_wake, 1);
    }
    const struct zmk_split_peripheral_status_changed *split =
        as_zmk_split_peripheral_status_changed(event);
    if (split) {
        atomic_set(&pet_greeting, split->connected);
        if (!split->connected) {
            k_mutex_lock(&status_lock, K_FOREVER);
            atomic_clear(&mode);
            atomic_clear(&host_disconnected);
                    atomic_clear(&practice_screen);
            memset(&practice_receiver, 0, sizeof(practice_receiver));
            memset(&practice_view, 0, sizeof(practice_view));
                    clear_tape();
            k_mutex_unlock(&status_lock);
        }
    }
    queue_refresh();
    return ZMK_EV_EVENT_BUBBLE;
}

ZMK_LISTENER(steno_squid_display, display_event);
ZMK_SUBSCRIPTION(steno_squid_display, zmk_activity_state_changed);
ZMK_SUBSCRIPTION(steno_squid_display, zmk_split_peripheral_status_changed);
ZMK_SUBSCRIPTION(steno_squid_display, zmk_battery_state_changed);
ZMK_SUBSCRIPTION(steno_squid_display, zmk_position_state_changed);
#if IS_ENABLED(CONFIG_USB_DEVICE_STACK)
ZMK_SUBSCRIPTION(steno_squid_display, zmk_usb_conn_state_changed);
#endif

lv_obj_t *zmk_display_status_screen(void) {
    lv_obj_t *screen = steno_squid_screen_create();
    refresh_status(NULL);
    return screen;
}
