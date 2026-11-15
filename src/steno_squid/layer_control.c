#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zmk/events/layer_state_changed.h>
#include <zmk/keymap.h>
#include <dt-bindings/zmk/javelin_steno_layers.h>

#include "layer_control.h"
#include "split_status.h"
#if IS_ENABLED(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_LAPWING)
#include "lapwing_flash.h"
#endif

/* What the left half does when the layer changes, whether or not the right
 * half has a display: keep the dictionaries ready while the steno layer is up,
 * end Practice when it is left, and start Practice once the dictionaries are
 * usable. Telling the display is the last step and exists only when the
 * display feature is built.
 */

/* Modes are recognised by layer ID, a layer's position in the devicetree
 * keymap. ZMK Studio can rename or reorder layers without changing it. These
 * checks fail the build if the keymap order drifts from javelin_steno_layers.h.
 */
#define CHECK_LAYER_ID(node, id)                                                                   \
    COND_CODE_1(DT_NODE_EXISTS(node), (BUILD_ASSERT(DT_NODE_CHILD_IDX(node) == (id));), ())
CHECK_LAYER_ID(DT_PATH(keymap, main_layer), JAVELIN_LAYER_MAIN)
CHECK_LAYER_ID(DT_PATH(keymap, sym_num_layer), JAVELIN_LAYER_SYM_NUM)
CHECK_LAYER_ID(DT_PATH(keymap, steno_layer), JAVELIN_LAYER_STENO)
CHECK_LAYER_ID(DT_PATH(keymap, plover_hid_layer), JAVELIN_LAYER_PLOVER_HID)

#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
/* Practice runs on the steno layer while this is set. */
static atomic_t practice_wanted;
#endif

static void refresh_display(void) {
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_SPLIT_STATUS)
    steno_squid_split_status_refresh();
#endif
}

static void apply_layer(void) {
    const bool steno = steno_squid_active_layer() == JAVELIN_LAYER_STENO;
    ARG_UNUSED(steno);
#if IS_ENABLED(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_LAPWING)
    /* Keep the dictionaries powered and ready while this layer is selected. */
    lapwing_flash_hold(steno);
#endif
#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
    /* Leaving the steno layer ends practice. */
    if (!steno) {
        atomic_clear(&practice_wanted);
    }
    const bool want_practice = steno && atomic_get(&practice_wanted);
#if IS_ENABLED(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_LAPWING)
    /* Starting practice takes the engine lock, which waits for an idle flash
     * to power up. This runs on the thread that handles keys, so do not wait
     * here: lapwing_flash_hold() above has started the wake, and the ready
     * hook calls apply_layer() again once the dictionaries are mapped.
     */
    if (want_practice && !lapwing_mapped_address()) {
        refresh_display();
        return;
    }
#endif
    steno_squid_practice_set_active(want_practice);
#endif
    refresh_display();
}

#if IS_ENABLED(CONFIG_ZMK_STENO_SQUID_PRACTICE)
void steno_squid_practice_toggle(void) {
    if (steno_squid_active_layer() == JAVELIN_LAYER_STENO) {
        atomic_set(&practice_wanted, !atomic_get(&practice_wanted));
    } else {
        /* The layer change clears the flag on its way, so set it afterwards. */
        /* Match &to: clear locked layers as well. A locked Plover layer
         * otherwise stays above Javelin and prevents Practice from starting.
         */
        zmk_keymap_layer_to(JAVELIN_LAYER_STENO, true);
        atomic_set(&practice_wanted, 1);
    }
    apply_layer();
}
#endif

static int layer_changed(const zmk_event_t *event) {
    apply_layer();
    return ZMK_EV_EVENT_BUBBLE;
}

#if IS_ENABLED(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_LAPWING)
/* The dictionaries became usable, or a check gave up. Practice selected during
 * the check starts now, and the right display drops its loading message.
 */
static void flash_ready(struct k_work *work) {
    apply_layer();
}

K_WORK_DEFINE(flash_ready_work, flash_ready);

void lapwing_flash_ready_changed(void) {
    k_work_submit(&flash_ready_work);
}
#endif

ZMK_LISTENER(steno_squid_layer_control, layer_changed);
ZMK_SUBSCRIPTION(steno_squid_layer_control, zmk_layer_state_changed);
