/* Javelin's existing WebHID console carried by an independent BLE HID service.
 * No report IDs: the unmodified website calls sendReport(0, ...).
 */
#include "javelin_hid_transport.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <zmk/ble.h>

static const uint8_t report_map[] = {JAVELIN_HID_REPORT_MAP};
static const uint8_t hid_info[] = {0x11, 0x01, 0x00, 0x02};
static const uint8_t input_ref[] = {0, 1};
static const uint8_t output_ref[] = {0, 2};
static const uint8_t empty_report[JAVELIN_HID_REPORT_BYTES];
static struct k_spinlock peer_lock;
static struct bt_conn *peer;
static uint32_t generation = 1;
#ifdef CONFIG_ZMK_STENO_SQUID_BLE_FAST_LINK
static struct bt_conn *session_peer_current(void) {
    k_spinlock_key_t key = k_spin_lock(&peer_lock);
    struct bt_conn *conn = peer ? bt_conn_ref(peer) : NULL;
    k_spin_unlock(&peer_lock, key);
    return conn;
}
#endif

static bool allowed_peer(struct bt_conn *conn) {
    struct bt_conn_info info;
    return bt_conn_get_info(conn, &info) == 0 && info.type == BT_CONN_TYPE_LE &&
           info.role == BT_CONN_ROLE_PERIPHERAL &&
           bt_conn_get_security(conn) >= BT_SECURITY_L2 &&
           bt_addr_le_cmp(zmk_ble_active_profile_addr(), bt_conn_get_dst(conn)) == 0;
}

static ssize_t read_map(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                        void *buf, uint16_t len, uint16_t offset) {
    return bt_gatt_attr_read(conn, attr, buf, len, offset, report_map, sizeof(report_map));
}

static ssize_t read_info(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                         void *buf, uint16_t len, uint16_t offset) {
    return bt_gatt_attr_read(conn, attr, buf, len, offset, hid_info, sizeof(hid_info));
}

static ssize_t read_ref(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                        void *buf, uint16_t len, uint16_t offset) {
    return bt_gatt_attr_read(conn, attr, buf, len, offset, attr->user_data, 2);
}

static ssize_t read_report(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                           void *buf, uint16_t len, uint16_t offset) {
    return bt_gatt_attr_read(conn, attr, buf, len, offset, empty_report, sizeof(empty_report));
}

#ifdef CONFIG_ZMK_STENO_SQUID_BLE_FAST_LINK
/* While the Manager is talking, ask the host for a short connection interval and no peripheral latency: with the usual
 * 15 ms / latency 30 a console round trip took about 120 ms and a 260 KB upload 417 s; at 7.5 ms / latency 0 it took 45 s.
 * Ten seconds after the last console write the keyboard's normal low-power parameters are requested again. Typing, which is
 * the other HID service, never triggers this. */
static atomic_t fast_link;
static int last_link_rc;
static int change_link(bool fast) {
    struct bt_conn *conn = session_peer_current();
    if (!conn) {
        return -ENOTCONN;
    }
    const struct bt_le_conn_param fast_param = BT_LE_CONN_PARAM_INIT(6, 9, 0, CONFIG_BT_PERIPHERAL_PREF_TIMEOUT);
    const struct bt_le_conn_param normal_param = BT_LE_CONN_PARAM_INIT(
        CONFIG_BT_PERIPHERAL_PREF_MIN_INT, CONFIG_BT_PERIPHERAL_PREF_MAX_INT,
        CONFIG_BT_PERIPHERAL_PREF_LATENCY, CONFIG_BT_PERIPHERAL_PREF_TIMEOUT);
    const int rc = bt_conn_le_param_update(conn, fast ? &fast_param : &normal_param);
    bt_conn_unref(conn);
    last_link_rc = rc;
    return rc;
}
static void fast_link_work(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(fast_work, fast_link_work);
static void fast_link_work(struct k_work *work) {
    ARG_UNUSED(work);
    if (change_link(true) != 0 && atomic_get(&fast_link)) {
        // The stack may be busy with its own parameter update right after connecting: ask again in a second.
        k_work_schedule(&fast_work, K_SECONDS(1));
    }
}
static void relax_link_work(struct k_work *work) {
    ARG_UNUSED(work);
    atomic_set(&fast_link, 0);
    change_link(false);
}
static K_WORK_DELAYABLE_DEFINE(relax_work, relax_link_work);
static void console_activity(void) {
    if (!atomic_set(&fast_link, 1)) {
        k_work_schedule(&fast_work, K_NO_WAIT);
    }
    k_work_reschedule(&relax_work, K_SECONDS(10));
}
#endif

static ssize_t write_report(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                            const void *buf, uint16_t len, uint16_t offset, uint8_t flags) {
    if (offset || (flags & BT_GATT_WRITE_FLAG_PREPARE)) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
    }
    if (!len || len > JAVELIN_HID_REPORT_BYTES) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }
    if (!allowed_peer(conn)) {
        return BT_GATT_ERR(BT_ATT_ERR_AUTHORIZATION);
    }

    struct bt_conn *old = NULL;
    k_spinlock_key_t key = k_spin_lock(&peer_lock);
    if (peer != conn) {
        old = peer;
        peer = bt_conn_ref(conn);
        generation++;
    }
    const uint32_t session = generation;
    k_spin_unlock(&peer_lock, key);
    if (old) {
        bt_conn_unref(old);
    }
#ifdef CONFIG_ZMK_STENO_SQUID_BLE_FAST_LINK
    console_activity();
#endif
    if (steno_squid_javelin_ble_receive(buf, len, session) != 0) {
        return BT_GATT_ERR(BT_ATT_ERR_INSUFFICIENT_RESOURCES);
    }
    return len;
}



static ssize_t control_point(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                             const void *buf, uint16_t len, uint16_t offset, uint8_t flags) {
    if (offset || len != 1) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }
    return len;
}

static const struct bt_gatt_attr *input_value;
BT_GATT_SERVICE_DEFINE(javelin_hog,
    BT_GATT_PRIMARY_SERVICE(BT_UUID_HIDS),
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_INFO, BT_GATT_CHRC_READ,
        BT_GATT_PERM_READ, read_info, NULL, NULL),
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_REPORT_MAP, BT_GATT_CHRC_READ,
        BT_GATT_PERM_READ_ENCRYPT, read_map, NULL, NULL),
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_REPORT, BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
        BT_GATT_PERM_READ_ENCRYPT, read_report, NULL, NULL),
    BT_GATT_CCC(NULL, BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT),
    BT_GATT_DESCRIPTOR(BT_UUID_HIDS_REPORT_REF, BT_GATT_PERM_READ_ENCRYPT,
        read_ref, NULL, input_ref),
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_REPORT,
        BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
        BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT, read_report, write_report, NULL),
    BT_GATT_DESCRIPTOR(BT_UUID_HIDS_REPORT_REF, BT_GATT_PERM_READ_ENCRYPT,
        read_ref, NULL, output_ref),
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_CTRL_POINT, BT_GATT_CHRC_WRITE_WITHOUT_RESP,
        BT_GATT_PERM_WRITE_ENCRYPT, NULL, control_point, NULL));

/* Resolve by its descriptor rather than relying on an attribute index. The
 * service declaration/order stays unchanged, preserving existing bonds. */
static int find_input_value(void) {
    for (size_t i = 0; i < javelin_hog.attr_count; ++i) {
        const struct bt_gatt_attr *attr = &javelin_hog.attrs[i];
        if (bt_uuid_cmp(attr->uuid, BT_UUID_HIDS_REPORT) == 0 &&
            attr->read == read_report && attr->write == NULL) {
            input_value = attr;
            return 0;
        }
    }
    return -ENOENT;
}
SYS_INIT(find_input_value, APPLICATION, 90);

static struct bt_conn *session_peer(uint32_t session) {
    k_spinlock_key_t key = k_spin_lock(&peer_lock);
    struct bt_conn *conn = peer && generation == session ? bt_conn_ref(peer) : NULL;
    k_spin_unlock(&peer_lock, key);
    return conn;
}

#ifdef CONFIG_ZMK_STENO_SQUID_LAB_BLE_TUNING
#ifdef CONFIG_ZMK_STENO_SQUID_BLE_FAST_LINK
static int lab_rc(void) { return last_link_rc; }
#else
static int lab_rc(void) { return 0; }
#endif
/* Lab only: read and change the parameters of the console's Bluetooth link. Commands (from "native_install lab-ble "):
 *   info                      interval (1.25 ms units), latency, timeout (10 ms units), PHYs, data length, MTU
 *   params MIN MAX LAT TO     ask the central for new connection parameters (units as in the Bluetooth spec)
 *   phy2m                     ask for the 2M PHY
 *   datalen                   ask for the largest link-layer data length
 */
void steno_squid_javelin_ble_lab(const char *args, char *reply, size_t bytes) {
    struct bt_conn *conn = session_peer(generation);
    if (!conn) {
        snprintf(reply, bytes, "ERR No Bluetooth console session yet");
        return;
    }
    struct bt_conn_info info;
    if (bt_conn_get_info(conn, &info) != 0 || info.type != BT_CONN_TYPE_LE) {
        snprintf(reply, bytes, "ERR No link information");
    } else if (!strncmp(args, "info", 4)) {
        snprintf(reply, bytes, "OK interval %u latency %u timeout %u phy %u/%u datalen %u/%u mtu %u fastrc %d",
                 info.le.interval, info.le.latency, info.le.timeout, info.le.phy->tx_phy, info.le.phy->rx_phy,
                 info.le.data_len->tx_max_len, info.le.data_len->rx_max_len, bt_gatt_get_mtu(conn), lab_rc());
    } else if (!strncmp(args, "params ", 7)) {
        unsigned min = 0, max = 0, lat = 0, to = 0;
        if (sscanf(args + 7, "%u %u %u %u", &min, &max, &lat, &to) != 4) {
            snprintf(reply, bytes, "ERR params MIN MAX LATENCY TIMEOUT");
        } else {
            const struct bt_le_conn_param param = BT_LE_CONN_PARAM_INIT(min, max, lat, to);
            snprintf(reply, bytes, "OK requested rc %d", bt_conn_le_param_update(conn, &param));
        }
    } else if (!strncmp(args, "phy2m", 5)) {
        snprintf(reply, bytes, "OK requested rc %d", bt_conn_le_phy_update(conn, BT_CONN_LE_PHY_PARAM_2M));
    } else if (!strncmp(args, "datalen", 7)) {
        snprintf(reply, bytes, "OK requested rc %d", bt_conn_le_data_len_update(conn, BT_LE_DATA_LEN_PARAM_MAX));
    } else {
        snprintf(reply, bytes, "ERR info | params MIN MAX LAT TO | phy2m | datalen");
    }
    bt_conn_unref(conn);
}
#endif

int steno_squid_javelin_ble_session_valid(uint32_t session) {
    struct bt_conn *conn = session_peer(session);
    if (!conn) {
        return 0;
    }
    const bool valid = input_value && allowed_peer(conn) &&
        bt_gatt_is_subscribed(conn, input_value, BT_GATT_CCC_NOTIFY);
    bt_conn_unref(conn);
    return valid;
}

int steno_squid_javelin_ble_notify(const void *data, size_t size, uint32_t session) {
    if (size != JAVELIN_HID_REPORT_BYTES) {
        return -EINVAL;
    }
    struct bt_conn *conn = session_peer(session);
    if (!conn) {
        return -ENOTCONN;
    }
    int rc = -ENOTCONN;
    if (input_value && allowed_peer(conn) &&
        bt_gatt_is_subscribed(conn, input_value, BT_GATT_CCC_NOTIFY)) {
        if (bt_gatt_get_mtu(conn) < size + 3) {
            rc = -EMSGSIZE;
        } else {
            /* GATT copies the payload into its ATT buffer before returning.
             * Only the isolated TX worker can block for buffers; neither the engine
             * nor the Bluetooth RX thread waits here.
             */
            rc = bt_gatt_notify(conn, input_value, data, size);
        }
    }
    bt_conn_unref(conn);
    return rc;
}

static void disconnected(struct bt_conn *conn, uint8_t reason) {
    ARG_UNUSED(reason);
    struct bt_conn *old = NULL;
    k_spinlock_key_t key = k_spin_lock(&peer_lock);
    if (peer == conn) {
        old = peer;
        peer = NULL;
        generation++;
#ifdef CONFIG_ZMK_STENO_SQUID_BLE_FAST_LINK
        atomic_set(&fast_link, 0);
        k_work_cancel_delayable(&relax_work);
        k_work_cancel_delayable(&fast_work);
#endif
    }
    k_spin_unlock(&peer_lock, key);
    if (old) {
        bt_conn_unref(old);
    }
}

BT_CONN_CB_DEFINE(javelin_hog_callbacks) = {.disconnected = disconnected};
