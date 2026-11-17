#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zmk/events/layer_state_changed.h>
#include <zmk/keymap.h>
#include <dt-bindings/zmk/javelin_steno_layers.h>

#include "layer_control.h"
#include "zmk_javelin_steno/zmk_platform_shim.h"
#include "split_status.h"
#include "lapwing_flash.h"

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

#include "layer_apply.h"

static int layer_changed(const zmk_event_t *event) {
    apply_layer();
    return ZMK_EV_EVENT_BUBBLE;
}

/* The dictionaries became usable, or a check gave up. Practice selected during
 * the check starts now, and the right display drops its loading message.
 */
static void flash_ready(struct k_work *work) {
    apply_layer();
}

K_WORK_DEFINE(flash_ready_work, flash_ready);

void lapwing_flash_ready_changed(void) {
    if (lapwing_mapped_address()) zmk_javelin_steno_prepare();
    k_work_submit(&flash_ready_work);
}

ZMK_LISTENER(steno_squid_layer_control, layer_changed);
ZMK_SUBSCRIPTION(steno_squid_layer_control, zmk_layer_state_changed);
