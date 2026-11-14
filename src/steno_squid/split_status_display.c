#define DT_DRV_COMPAT zmk_behavior_steno_squid_status

#include <drivers/behavior.h>
#include <lvgl.h>
#include <zephyr/kernel.h>
#include <zephyr/spinlock.h>
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
#include "stroke_feed.h"

BUILD_ASSERT(DT_HAS_COMPAT_STATUS_OKAY(DT_DRV_COMPAT));
BUILD_ASSERT(__builtin_strcmp(DEVICE_DT_NAME(DT_DRV_INST(0)), STENO_SQUID_STATUS_BEHAVIOR) == 0);

/* Past the modes the left can send: show the layer name it sent instead. */
#define MODE_LAYER_NAME STENO_SQUID_MODE_COUNT

static atomic_t mode;
static atomic_t host_disconnected;
static atomic_t layer_name_low;
static atomic_t layer_name_high;
static atomic_t practice_screen;
static uint64_t practice_progress;
static struct k_spinlock progress_lock;
static atomic_t pet_wake;
static atomic_t pet_typing;
static atomic_t pet_greeting;

#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
/* The last stroke the left half sent. Messages arrive on the Bluetooth thread
 * and the display work queue reads it.
 */
static struct steno_squid_tape tape;
static struct k_spinlock tape_lock;

static void clear_tape(void) {
    k_spinlock_key_t key = k_spin_lock(&tape_lock);
    memset(&tape, 0, sizeof(tape));
    k_spin_unlock(&tape_lock, key);
}
#else
static void clear_tape(void) {}
#endif

static void refresh_status(struct k_work *work) {
    const unsigned shown = atomic_get(&mode);
    char name[STENO_SQUID_LAYER_NAME_CHARS + 1] = {0};
    if (shown == MODE_LAYER_NAME) {
        const uint64_t packed = (uint64_t)(uint32_t)atomic_get(&layer_name_high) << 32 |
                                (uint32_t)atomic_get(&layer_name_low);
        for (int i = 0; i < STENO_SQUID_LAYER_NAME_CHARS; i++) {
            name[i] = (packed >> (7 * i)) & 0x7f;
        }
    }
    const uint32_t practice = atomic_get(&practice_screen);
    const bool wake = atomic_clear(&pet_wake);
    const bool typing = atomic_clear(&pet_typing);
    const bool practicing = (practice & STENO_SQUID_PRACTICE_ACTIVE) != 0;
#if IS_ENABLED(CONFIG_USB_DEVICE_STACK)
    steno_squid_pet_set_powered(zmk_usb_is_powered());
#endif
    if (wake && !practicing) {
        steno_squid_pet_set_state(PET_IDLE);
    }
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
    struct steno_squid_tape shown_tape;
    k_spinlock_key_t key = k_spin_lock(&tape_lock);
    shown_tape = tape;
    k_spin_unlock(&tape_lock, key);
    steno_squid_screen_set_tape(&shown_tape);
#endif
    k_spinlock_key_t progress_key = k_spin_lock(&progress_lock);
    const uint64_t shown_progress = practice_progress;
    k_spin_unlock(&progress_lock, progress_key);
    steno_squid_practice_widget_set_progress(shown_progress);
    steno_squid_screen_update(shown, name, practice, zmk_battery_state_of_charge(),
                         atomic_get(&host_disconnected));
    if (!practicing) {
        if (zmk_activity_get_state() != ZMK_ACTIVITY_ACTIVE) {
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

static int status_received(struct zmk_behavior_binding *binding,
                           struct zmk_behavior_binding_event event) {
    if (binding->param2 == STENO_SQUID_HOST_STATUS_PROTOCOL) {
        if (binding->param1 > 1) {
            return -EINVAL;
        }
        atomic_set(&host_disconnected, binding->param1);
    } else if ((binding->param2 & 0xffu) == STENO_SQUID_PRACTICE_PROGRESS_PROTOCOL &&
               !(binding->param2 & 0xe0000000u)) {
        const uint64_t value = ((uint64_t)(binding->param2 >> 8) << 32) | binding->param1;
        const unsigned review = steno_squid_practice_review(value);
        if (steno_squid_practice_first_try(value) > STENO_SQUID_WORD_ROUNDS ||
            steno_squid_practice_xp_earned(value) > 120 ||
            (review >= STENO_SQUID_WORD_COUNT && review != 127)) return -EINVAL;
        k_spinlock_key_t key = k_spin_lock(&progress_lock);
        practice_progress = value;
        k_spin_unlock(&progress_lock, key);
    } else if (binding->param2 == STENO_SQUID_PRACTICE_PROTOCOL) {
        const uint32_t value = binding->param1;
        const unsigned drill = (value >> STENO_SQUID_PRACTICE_DRILL_SHIFT) & 3u;
        if (!(value & STENO_SQUID_PRACTICE_ACTIVE) || drill >= STENO_SQUID_PRACTICE_DRILL_COUNT) {
            return -EINVAL;
        }
        const unsigned total = steno_squid_practice_rounds(drill);
        const unsigned round = steno_squid_practice_counter(value, STENO_SQUID_PRACTICE_ROUND_SHIFT);
        if (((value & STENO_SQUID_PRACTICE_MENU) &&
             (round || steno_squid_practice_feedback_value(value) != STENO_SQUID_PRACTICE_PROMPT)) ||
            round > total ||
            ((round == total) != (steno_squid_practice_feedback_value(value) == STENO_SQUID_PRACTICE_DONE)) ||
            steno_squid_practice_counter(value, STENO_SQUID_PRACTICE_STREAK_SHIFT) > total ||
            steno_squid_practice_counter(value, STENO_SQUID_PRACTICE_BEST_SHIFT) > total ||
            steno_squid_practice_prompt_value(value) >= steno_squid_practice_prompt_count(drill)) {
            return -EINVAL;
        }
        atomic_set(&practice_screen, value);
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
    } else if ((binding->param2 & STENO_SQUID_TAPE_TAG) &&
               !(binding->param2 & STENO_SQUID_LAYER_NAME_PROTOCOL)) {
        k_spinlock_key_t key = k_spin_lock(&tape_lock);
        struct steno_squid_tape next = tape;
        const int kind = steno_squid_tape_decode(binding->param1, binding->param2, &next);
        if (kind >= 0) {
            tape = next;
        }
        k_spin_unlock(&tape_lock, key);
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
            atomic_clear(&mode);
            atomic_clear(&host_disconnected);
            atomic_clear(&practice_screen);
            clear_tape();
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
