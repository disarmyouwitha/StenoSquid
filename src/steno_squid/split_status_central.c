#include <string.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/kernel.h>
#include <zephyr/spinlock.h>
#include <zephyr/sys/atomic.h>
#include <zmk/ble.h>
#include <zmk/events/ble_active_profile_changed.h>
#include <zmk/events/layer_state_changed.h>
#if IS_ENABLED(CONFIG_ZMK_USB)
#include <zmk/events/usb_conn_state_changed.h>
#include <zmk/usb.h>
#endif
#include <zmk/keymap.h>
#include <zmk/split/bluetooth/service.h>
#include <zmk/split/central.h>
#include <dt-bindings/zmk/javelin_steno_layers.h>

#include "layer_control.h"
#include "split_status.h"
#include "stroke_feed.h"
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
#include "practice_progress.h"
#include "practice_view.h"
#endif
#if IS_ENABLED(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_LAPWING)
#include "lapwing_flash.h"
#endif

BUILD_ASSERT(ZMK_SPLIT_CENTRAL_PERIPHERAL_COUNT == 1);

/* The names the keymap gives its layers, by layer ID. A layer that still has
 * its keymap name shows the firmware's own label for its mode.
 */
#define KEYMAP_LAYER_NAME(node) DT_PROP_OR(node, display_name, DT_PROP_OR(node, label, ""))
static const char *const keymap_layer_names[] = {
    ZMK_KEYMAP_LAYERS_FOREACH_SEP(KEYMAP_LAYER_NAME, (, ))};

BUILD_ASSERT(sizeof(STENO_SQUID_STATUS_BEHAVIOR) <= ZMK_SPLIT_RUN_BEHAVIOR_DEV_LEN);

/* Four snapshots a second apart, then six more spread over half a minute.
 * After that one snapshot a minute, so a message the right half missed is
 * repaired within a minute. It is a single small write on a link that is
 * already open.
 */
#define SYNC_FAST_ATTEMPTS 4
#define SYNC_SLOW_ATTEMPTS 6
#define SYNC_HEARTBEAT_SECONDS 60

static atomic_t connected;
static atomic_t sync_attempts;

/* A layer that needs the dictionaries says so while they are not usable. */
static enum steno_squid_mode dictionary_mode(enum steno_squid_mode ready) {
#if IS_ENABLED(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_LAPWING)
    switch (lapwing_flash_status()) {
    case LAPWING_FLASH_LOADING:
        return STENO_SQUID_MODE_LOADING;
    case LAPWING_FLASH_UNAVAILABLE:
        return STENO_SQUID_MODE_NO_DICTIONARY;
    default:
        break;
    }
#endif
    return ready;
}

static enum steno_squid_mode current_mode(void) {
    switch (steno_squid_active_layer()) {
    case JAVELIN_LAYER_SYM_NUM:
        return STENO_SQUID_MODE_SYMBOLS;
    case JAVELIN_LAYER_STENO:
        return dictionary_mode(STENO_SQUID_MODE_JAVELIN);
    case JAVELIN_LAYER_PLOVER_HID:
        return STENO_SQUID_MODE_PLOVER;
    default:
        /* The main layer, and any layer a user adds in ZMK Studio. */
        return STENO_SQUID_MODE_TYPING;
    }
}

/* The active layer's name when the display should show it: the layer was
 * renamed in ZMK Studio, or it has no mode of its own. Otherwise NULL.
 */
static const char *custom_layer_name(void) {
    const zmk_keymap_layer_id_t layer = steno_squid_active_layer();
    const char *name = zmk_keymap_layer_name(layer);
    if (!name || !name[0]) {
        return NULL;
    }
    if (layer <= JAVELIN_LAYER_PLOVER_HID && layer < ARRAY_SIZE(keymap_layer_names) &&
        strcmp(name, keymap_layer_names[layer]) == 0) {
        return NULL;
    }
    return name;
}

static void pack_layer_name(const char *name, struct zmk_behavior_binding *binding) {
    uint64_t packed = 0;
    for (int i = 0; i < STENO_SQUID_LAYER_NAME_CHARS && name[i]; i++) {
        const uint8_t c = name[i];
        /* The display font has ASCII only. */
        packed |= (uint64_t)(c >= ' ' && c < 0x7f ? c : '?') << (7 * i);
    }
    binding->param1 = (uint32_t)packed;
    binding->param2 = (uint32_t)(packed >> 32) | STENO_SQUID_LAYER_NAME_PROTOCOL;
}

#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
static void retry_practice(struct k_work *work);
K_WORK_DELAYABLE_DEFINE(practice_retry_work, retry_practice);
#endif

static void send_status(struct k_work *work) {
    if (!atomic_get(&connected)) {
        return;
    }

    struct zmk_behavior_binding binding = {
        .behavior_dev = STENO_SQUID_STATUS_BEHAVIOR,
        .param1 = current_mode(),
        .param2 = STENO_SQUID_STATUS_PROTOCOL,
    };
    /* Loading and No dictionary take the place of a layer's name. */
    const char *name = custom_layer_name();
    if (name && binding.param1 != STENO_SQUID_MODE_LOADING &&
        binding.param1 != STENO_SQUID_MODE_NO_DICTIONARY) {
        pack_layer_name(name, &binding);
    }
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
    struct steno_squid_practice_view view;
    steno_squid_practice_view_snapshot(&view);
    if (view.state & STENO_SQUID_PRACTICE_ACTIVE) {
        static uint8_t sequence;
        uint8_t bytes[STENO_SQUID_PRACTICE_FRAME_MAX];
        const unsigned length = steno_squid_practice_encode(&view, bytes);
        ++sequence;
        struct zmk_behavior_binding_event event = {.timestamp = k_uptime_get()};
        for (unsigned part=0; part<(length+4)/5; part++) {
            steno_squid_practice_fragment(bytes, length, sequence, part, &binding.param1, &binding.param2);
            zmk_split_central_invoke_behavior(0, &binding, event, true);
        }
        /* One prompt/results repeat repairs a dropped fragment promptly.
         * Correct feedback already advances in 250 ms and needs no repeat. */
        if (work != &practice_retry_work.work &&
            steno_squid_practice_feedback_value(view.state) != STENO_SQUID_PRACTICE_CORRECT) {
            k_work_reschedule(&practice_retry_work, K_MSEC(150));
        }
        return;
    }
    k_work_cancel_delayable(&practice_retry_work);
#endif
    struct zmk_behavior_binding_event event = {.timestamp = k_uptime_get()};
    zmk_split_central_invoke_behavior(0, &binding, event, true);
}

#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
static void retry_practice(struct k_work *work) { send_status(work); }
#endif

K_WORK_DEFINE(send_status_work, send_status);

void steno_squid_split_status_refresh(void) {
    k_work_submit(&send_status_work);
}

#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
/* The latest stroke waits here for the work queue. A stroke typed before the
 * last one was sent replaces it, so the display always gets the newest.
 */
static struct steno_squid_tape pending_tape;
static struct k_spinlock pending_tape_lock;

static void invoke_tape(uint32_t param1, uint32_t param2) {
    struct zmk_behavior_binding binding = {
        .behavior_dev = STENO_SQUID_STATUS_BEHAVIOR,
        .param1 = param1,
        .param2 = param2,
    };
    struct zmk_behavior_binding_event event = {.timestamp = k_uptime_get()};
    zmk_split_central_invoke_behavior(0, &binding, event, true);
}

static void send_tape(struct k_work *work) {
    if (!atomic_get(&connected)) {
        return;
    }
    struct steno_squid_tape tape;
    k_spinlock_key_t key = k_spin_lock(&pending_tape_lock);
    tape = pending_tape;
    k_spin_unlock(&pending_tape_lock, key);
    if (!tape.valid) {
        return;
    }
    uint32_t param1, param2;
    steno_squid_tape_encode_stroke(&tape, &param1, &param2);
    invoke_tape(param1, param2);
    if (steno_squid_tape_needs_more(&tape)) {
        steno_squid_tape_encode_more(&tape, &param1, &param2);
        invoke_tape(param1, param2);
    }
    if (tape.hint_count) {
        steno_squid_tape_encode_hint(&tape, &param1, &param2);
        invoke_tape(param1, param2);
    }
}

K_WORK_DEFINE(send_tape_work, send_tape);

void steno_squid_tape_publish(const struct steno_squid_tape *tape) {
    k_spinlock_key_t key = k_spin_lock(&pending_tape_lock);
    pending_tape = *tape;
    k_spin_unlock(&pending_tape_lock, key);
    k_work_submit(&send_tape_work);
}
#endif

/* Report the active host profile, independent of the split Bluetooth link. */
static void send_host_status(struct k_work *work) {
    if (!atomic_get(&connected)) {
        return;
    }

    bool host_connected = zmk_ble_active_profile_is_connected();
#if IS_ENABLED(CONFIG_ZMK_USB)
    host_connected = host_connected || zmk_usb_is_hid_ready();
#endif
    struct zmk_behavior_binding binding = {
        .behavior_dev = STENO_SQUID_STATUS_BEHAVIOR,
        .param1 = !host_connected,
        .param2 = STENO_SQUID_HOST_STATUS_PROTOCOL,
    };
    struct zmk_behavior_binding_event event = {.timestamp = k_uptime_get()};
    zmk_split_central_invoke_behavior(0, &binding, event, true);
}

K_WORK_DEFINE(send_host_status_work, send_host_status);

static int host_status_changed(const zmk_event_t *event) {
    k_work_submit(&send_host_status_work);
    return ZMK_EV_EVENT_BUBBLE;
}

ZMK_LISTENER(steno_squid_host_status, host_status_changed);
ZMK_SUBSCRIPTION(steno_squid_host_status, zmk_ble_active_profile_changed);
#if IS_ENABLED(CONFIG_ZMK_USB)
ZMK_SUBSCRIPTION(steno_squid_host_status, zmk_usb_conn_state_changed);
#endif

static void sync_after_connect(struct k_work *work) {
    send_status(work);
    send_host_status(work);
    // ZMK discovers the remote behavior characteristic after the link comes up.
    // Send snapshots through discovery, which can outlast the first few seconds
    // on a fresh pairing. After that, only send on layer changes.
    if (!atomic_get(&connected)) {
        return;
    }
    k_timeout_t next = K_SECONDS(SYNC_HEARTBEAT_SECONDS);
    if (atomic_get(&sync_attempts) > 0) {
        const atomic_val_t remaining = atomic_dec(&sync_attempts) - 1;
        if (remaining > 0) {
            next = remaining > SYNC_SLOW_ATTEMPTS ? K_SECONDS(1) : K_SECONDS(5);
        }
    }
    k_work_reschedule(k_work_delayable_from_work(work), next);
}

K_WORK_DELAYABLE_DEFINE(sync_status_work, sync_after_connect);

static bool is_split_link(struct bt_conn *conn) {
    struct bt_conn_info info;
    return bt_conn_get_info(conn, &info) == 0 && info.role == BT_CONN_ROLE_CENTRAL;
}

static void status_connected(struct bt_conn *conn, uint8_t err) {
    if (err == 0 && is_split_link(conn)) {
        atomic_set(&connected, 1);
        atomic_set(&sync_attempts, SYNC_FAST_ATTEMPTS + SYNC_SLOW_ATTEMPTS);
        k_work_reschedule(&sync_status_work, K_MSEC(500));
    }
}

static void status_disconnected(struct bt_conn *conn, uint8_t reason) {
    if (is_split_link(conn)) {
        atomic_clear(&connected);
        k_work_cancel_delayable(&sync_status_work);
    }
}

BT_CONN_CB_DEFINE(steno_squid_split_status_conn) = {
    .connected = status_connected,
    .disconnected = status_disconnected,
};
