#include "console.h"
#include "javelin_hid_transport.h"
#include "console_commands.h"
#ifdef CONFIG_ZMK_JAVELIN_STENO_HID_PROVISIONING
#include "native_store_runtime.h"
#ifdef CONFIG_ZMK_STENO_SQUID_TRANSFER_SESSION
#include "transfer_frame.h"
#endif
#endif
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
#include "battery_log.h"
#endif

#include <string.h>
#include <zephyr/device.h>
#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/usb/class/usb_hid.h>



namespace {
BUILD_ASSERT(CONFIG_USB_HID_DEVICE_COUNT >= 3, "Javelin needs the third USB HID interface");
constexpr size_t REPORT_BYTES = JAVELIN_HID_REPORT_BYTES;
const device *hid;
K_SEM_DEFINE(transmit_ready, 1, 1);

struct Report {
    uint8_t bytes[REPORT_BYTES];
    bool bluetooth = false;
    uint32_t session = 0;
};
K_MSGQ_DEFINE(received_reports, sizeof(Report), 40, 4);
alignas(4) uint8_t transmit_report[REPORT_BYTES];

constexpr uint8_t descriptor[] = {JAVELIN_HID_REPORT_MAP};

void input_complete(const device *) {
    k_sem_give(&transmit_ready);
}

int output_report(const device *, usb_setup_packet *setup, int32_t *size, uint8_t **data) {
    if (sys_le16_to_cpu(setup->wValue) != 0x0200 || *size <= 0 ||
        *size > static_cast<int32_t>(REPORT_BYTES)) {
        return -EINVAL;
    }
    Report report{};
    memcpy(report.bytes, *data, *size);
    return k_msgq_put(&received_reports, &report, K_NO_WAIT);
}

const hid_ops operations = {
    .set_report = output_report,
    .int_in_ready = input_complete,
};

int initialize_hid() {
    hid = device_get_binding("HID_2");
    if (!hid) {
        return -ENODEV;
    }
    usb_hid_register_device(hid, descriptor, sizeof(descriptor), &operations);
    return usb_hid_init(hid);
}

class HidWriter final : public IWriter {
public:
    explicit HidWriter(const Report &report)
        : bluetooth(report.bluetooth), session(report.session) {}

#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
    void CountCommand() const { steno_squid_battery_log_editor_command(bluetooth); }
#endif

    bool IsUsb() const { return !bluetooth; }
    uint32_t BluetoothSession() const { return session; }

    bool Failed() const { return failed; }

    ~HidWriter() { Flush(); }

    void Write(const char *text, size_t size) override {
        while (size && !failed) {
            const size_t count = MIN(size, REPORT_BYTES - used);
            memcpy(pending + used, text, count);
            used += count;
            text += count;
            size -= count;
            if (used == REPORT_BYTES) Flush();
        }
    }

    void Flush() {
        if (!used || failed) return;
        memset(pending + used, 0, REPORT_BYTES - used);
        if (bluetooth) {
#ifdef CONFIG_ZMK_JAVELIN_STENO_BLE_HID
            failed = steno_squid_javelin_ble_send(pending, REPORT_BYTES, session) != 0;
#else
            failed = true;
#endif
        } else if (!hid || !steno_squid_usb_hid_ready() ||
                   k_sem_take(&transmit_ready, K_SECONDS(2)) != 0) {
            failed = true;
        } else {
            // The endpoint owns this buffer until completion, even after a timeout.
            memcpy(transmit_report, pending, REPORT_BYTES);
            if (hid_int_ep_write(hid, transmit_report, REPORT_BYTES, nullptr) != 0) {
                k_sem_give(&transmit_ready);
                failed = true;
            }
        }
        used = 0;
    }
private:
    bool bluetooth;
    uint32_t session;
    bool failed = false;
    uint8_t pending[REPORT_BYTES];
    size_t used = 0;
};

static int parse_channel(const uint8_t *line, size_t length) {
    return length >= 4 && line[0] == 'c' && line[1] >= '0' && line[1] <= '9' &&
           line[2] >= '0' && line[2] <= '9' && line[3] == ' '
        ? 10 * (line[1] - '0') + line[2] - '0' : -1;
}
static void reply_error(HidWriter &writer, const char *line, bool channel, const char *message) {
    if (channel) writer.Write(line, 4);
    writer.Write(message, strlen(message));
}

void dispatch(char *line, size_t size, HidWriter &writer) {
    struct FlushResponse { HidWriter &writer; ~FlushResponse() { writer.Flush(); } } flush{writer};
    const bool channel = parse_channel(reinterpret_cast<const uint8_t *>(line), size) >= 0;
    const char *command = line + (channel ? 4 : 0);
    if (strcmp(command, "start_javelin_console\n") == 0 || strcmp(command, "\n") == 0) {
        return;
    }
#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
    writer.CountCommand();
#endif

#ifdef CONFIG_ZMK_JAVELIN_STENO_HID_PROVISIONING
    constexpr char install_prefix[] = "native_install ";
    if (strncmp(command, install_prefix, sizeof(install_prefix) - 1) == 0) {
        const char *operation = command + sizeof(install_prefix) - 1;
        if (!writer.IsUsb() && (strcmp(operation, "update repack-capabilities\n") == 0 ||
                               strcmp(operation, "update recovery-capabilities\n") == 0 ||
                               strncmp(operation, "large ", 6) == 0 ||
                               strncmp(operation, "begin CLEAR ", 12) == 0 ||
                               strncmp(operation, "begin REPACK ", 13) == 0 ||
                               strncmp(operation, "begin RECOVERY ", 15) == 0 ||
                               strncmp(operation, "match RECOVERY ", 15) == 0)) {
            reply_error(writer, line, channel, "ERR Connect over USB for setup, repack or recovery\n\n");
            return;
        }
        static char reply[704]; // Only the HID worker calls dispatch.
        steno_squid_native_install_command(operation, reply, sizeof(reply), writer.BluetoothSession());
        if (channel) {
            writer.Write(line, 4);
        }
        writer.Write(reply, strlen(reply));
        writer.Write("\n\n", 2);
        return;
    }
#endif

    const bool forward = steno_squid_console_command_allowed(command, writer.IsUsb());
    if (forward) {
        const bool handled = steno_squid_javelin_console_line(
            line, size, writer,
            [](void *context) { return static_cast<HidWriter *>(context)->Failed(); }, &writer,
            writer.BluetoothSession());
        if (handled) return;
    }

    // Always answer, so the browser never waits on a dropped command.
    reply_error(writer, line, channel, forward ? "ERR Dictionary flash is not ready\n\n"
                                              : "ERR Command unavailable on this keyboard\n\n");
}

struct CommandBuffer {
    int channel = -2;
    size_t used = 0;
    bool too_long = false;
    int64_t last_packet = 0;
    bool bluetooth = false;
    uint32_t session = 0;
    char line[Console::BUFFER_SIZE];
};
// One slot per command split across reports. A command that fits in one report
// frees its slot at once, so a third command arriving while two others are half
// received (over USB, Bluetooth or separate channels) is answered with
// "ERR Too many console channels".
CommandBuffer buffers[2];

void reset_buffer(CommandBuffer &buffer) {
    buffer.channel = -2;
    buffer.used = 0;
    buffer.too_long = false;
    buffer.last_packet = 0;
}

void hid_worker(void *, void *, void *) {
    Report report;

    for (;;) {
        if (!steno_squid_usb_hid_ready()) {
            k_sem_give(&transmit_ready);
        }
        bool partial = false;
        for (auto &buffer : buffers) {
            if (buffer.channel == -2) {
                continue;
            }
            if ((!buffer.bluetooth && !steno_squid_usb_hid_ready()) ||
                k_uptime_get() - buffer.last_packet > 5000) {
                reset_buffer(buffer);
            } else {
                partial = true;
            }
        }
        // Sleep until the editor sends something. While a command is half
        // received, wake ten times a second so a disconnect or a stalled
        // sender clears it promptly.
        if (k_msgq_get(&received_reports, &report, partial ? K_MSEC(100) : K_FOREVER) != 0) {
            continue;
        }
        if (report.bluetooth) {
#ifdef CONFIG_ZMK_JAVELIN_STENO_BLE_HID
            if (!steno_squid_javelin_ble_session_valid(report.session)) {
                continue;
            }
#else
            continue;
#endif
        } else if (!steno_squid_usb_hid_ready()) {
            k_sem_give(&transmit_ready);
            continue;
        }
        HidWriter writer(report);

#ifdef CONFIG_ZMK_STENO_SQUID_TRANSFER_SESSION
        // Binary transfer frames start with 0xB5, which console text and Base64 never
        // produce, so routing needs no mode switch and the console is untouched.
        if (report.bytes[0] == steno_transfer::kMagic) {
#ifndef CONFIG_ZMK_STENO_SQUID_TRANSFER_OVER_BLUETOOTH
            if (report.bluetooth) continue; // binary transfer is USB-only until Bluetooth reaches 50 KiB/s
#endif
            uint8_t reply[REPORT_BYTES];
            const size_t length = steno_squid_native_transfer_report(
                report.bytes, sizeof(report.bytes), report.bluetooth ? report.session : 0, reply, sizeof(reply));
            if (length) writer.Write(reinterpret_cast<const char *>(reply), length);
            continue;
        }
#endif

        const auto *bytes = report.bytes;
        size_t length = sizeof(report.bytes);
        const int channel = parse_channel(bytes, length);
        const bool has_channel = channel >= 0;
        if (has_channel) {
            bytes += 4;
            length -= 4;
        }

        CommandBuffer *target = nullptr;
        for (auto &buffer : buffers) {
            if (buffer.channel == channel && buffer.bluetooth == report.bluetooth &&
                buffer.session == report.session) {
                target = &buffer;
                break;
            }
            if (buffer.channel == -2 || k_uptime_get() - buffer.last_packet > 5000) {
                target = &buffer;
            }
        }
        if (!target) {
            if (has_channel) {
                writer.Write(reinterpret_cast<const char *>(report.bytes), 4);
            }
            constexpr char busy[] = "ERR Too many console channels\n\n";
            writer.Write(busy, sizeof(busy) - 1);
            continue;
        }
        if (target->channel != channel || target->bluetooth != report.bluetooth ||
            target->session != report.session || k_uptime_get() - target->last_packet > 5000) {
            reset_buffer(*target);
            target->channel = channel;
            target->bluetooth = report.bluetooth;
            target->session = report.session;
        }
        target->last_packet = k_uptime_get();

        for (size_t i = 0; i < length; i++) {
            const uint8_t c = bytes[i];
            if (c == 0 || c == '\r') {
                continue;
            }
            if (target->used == 0 && has_channel) {
                memcpy(target->line, report.bytes, 4);
                target->used = 4;
            }
            if (c == '\n') {
                if (target->too_long) {
                    if (has_channel) {
                        writer.Write(reinterpret_cast<const char *>(report.bytes), 4);
                    }
                    constexpr char message[] = "ERR Command too long\n\n";
                    writer.Write(message, sizeof(message) - 1);
                } else {
                    target->line[target->used++] = '\n';
                    target->line[target->used] = '\0';
                    dispatch(target->line, target->used, writer);
                    if (writer.Failed()) {
                        reset_buffer(*target);
                        break;
                    }
                }
                target->used = 0;
                target->too_long = false;
            } else if (target->used + 2 < sizeof(target->line)) {
                target->line[target->used++] = c;
            } else {
                target->too_long = true;
            }
        }
        if (target->used == 0) {
            target->channel = -2;
        }
    }
}
} // namespace

#ifdef CONFIG_ZMK_JAVELIN_STENO_BLE_HID
extern "C" int steno_squid_javelin_ble_receive(const void *data, size_t size, uint32_t session) {
    if (!size || size > REPORT_BYTES) {
        return -EINVAL;
    }
    Report report{};
    report.bluetooth = true;
    report.session = session;
    memcpy(report.bytes, data, size);
    return k_msgq_put(&received_reports, &report, K_NO_WAIT);
}
#endif

SYS_INIT(initialize_hid, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
K_THREAD_DEFINE(javelin_web_hid, 4096, hid_worker, nullptr, nullptr, nullptr, 6, 0, 200);
