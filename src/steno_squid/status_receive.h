#pragma once

// Private validated status receiver. Included after the state and refresh hooks.
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

