#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define JAVELIN_HID_REPORT_BYTES 63

/* Javelin's vendor console collection, separate from keyboard and Plover HID.
 * One input and one output report of JAVELIN_HID_REPORT_BYTES bytes, no report
 * ID. Both the USB and the Bluetooth HID services expose these bytes.
 */
#define JAVELIN_HID_REPORT_MAP \
    0x06, 0x31, 0xff, /* Usage Page 0xFF31 */ \
    0x09, 0x74,       /* Usage 0x74 */ \
    0xa1, 0x01,       /* Application collection */ \
    0x15, 0x00,       /* Logical minimum 0 */ \
    0x26, 0xff, 0x00, /* Logical maximum 255 */ \
    0x75, 0x08,       /* Eight bits per field */ \
    0x95, JAVELIN_HID_REPORT_BYTES, \
    0x09, 0x75, \
    0x81, 0x02,       /* Input, data, variable */ \
    0x95, JAVELIN_HID_REPORT_BYTES, \
    0x09, 0x76, \
    0x91, 0x02,       /* Output, data, variable */ \
    0xc0

#ifdef __cplusplus
extern "C" {
#endif

bool steno_squid_usb_hid_ready(void);

/* Queue a Bluetooth report without running the engine on the Bluetooth thread. */
int steno_squid_javelin_ble_receive(const void *data, size_t size, uint32_t session);
int steno_squid_javelin_ble_send(const void *data, size_t size, uint32_t session);
int steno_squid_javelin_ble_session_valid(uint32_t session);
#ifdef CONFIG_ZMK_STENO_SQUID_LAB_BLE_TUNING
void steno_squid_javelin_ble_lab(const char *args, char *reply, size_t bytes);
#endif

#ifdef __cplusplus
}
#endif

#ifdef __cplusplus
class IWriter;
bool steno_squid_javelin_console_line(const char *line, size_t size, IWriter &writer,
                                      bool (*cancelled)(void *), void *context, uint32_t bluetooth_session);
uint32_t steno_squid_javelin_bluetooth_session();
bool steno_squid_javelin_response_cancelled();
bool steno_squid_javelin_response_active();
#endif
