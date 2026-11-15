#pragma once

#include <lvgl.h>

enum pet_state {
    PET_IDLE,
    PET_TYPING,
    PET_LEVEL_UP,
    PET_SLEEP,
    PET_POWERED,
    PET_GREETING,
    PET_LAYER_CHANGE,
    PET_WAIT_TAP,
    PET_UPLOAD,
    PET_REPAIR,
    PET_INSTALL,
};

void steno_squid_pet_create(lv_obj_t *parent);
void steno_squid_pet_set_state(enum pet_state state);
void steno_squid_pet_set_visible(bool visible);
void steno_squid_pet_set_powered(bool powered);
void steno_squid_pet_set_headroom(bool available);
/* Hold an awake resting pose on the unconfirmed results screen. */
void steno_squid_pet_set_awake_hold(bool hold);

/* Select the transfer animation, or release it with PET_IDLE. */
void steno_squid_pet_set_transfer_animation(enum pet_state animation);
