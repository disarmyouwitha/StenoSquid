#!/usr/bin/env python3
"""Run the real console transport worker with native USB/BLE driver doubles."""
from hostlib import compiler as host_compiler
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]

STUBS = r'''
#pragma once
#define BUILD_ASSERT(x, ...) static_assert(x)
#define CONFIG_USB_HID_DEVICE_COUNT 3
#include <algorithm>
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <deque>
#include <stdexcept>
#include <string>
#include <vector>
struct Done {};
struct device {};
inline device dev;
inline bool usb_ready = true;
inline uint32_t live_session = 2;
inline int64_t now = 100;
struct k_sem { int count; };
struct k_msgq { size_t size; std::deque<std::vector<uint8_t>> messages; };
#define K_SEM_DEFINE(n,a,b) k_sem n{a}
#define K_MSGQ_DEFINE(n,s,c,a) k_msgq n{s,{}}
#define K_SECONDS(n) (n)
#define K_MSEC(n) (n)
#define K_NO_WAIT 0
#define K_FOREVER -1
#define SYS_INIT(...)
#define K_THREAD_DEFINE(...)
#define MIN(a,b) std::min<size_t>((a),(b))
inline int k_sem_take(k_sem *s, int) { if (!s->count) return -1; s->count=0; return 0; }
inline void k_sem_give(k_sem *s) { s->count=1; }
inline int64_t k_uptime_get() { return now; }
inline int k_msgq_put(k_msgq *q, const void *p, int) {
    q->messages.emplace_back((const uint8_t *)p,(const uint8_t *)p+q->size); return 0;
}
inline int k_msgq_get(k_msgq *q, void *p, int) {
    if(q->messages.empty()) throw Done{};
    memcpy(p,q->messages.front().data(),q->size); q->messages.pop_front(); now++; return 0;
}
inline const device *device_get_binding(const char *) { return &dev; }
struct usb_setup_packet { uint16_t wValue; };
struct hid_ops {
    int (*set_report)(const device *,usb_setup_packet *,int32_t *,uint8_t **);
    void (*int_in_ready)(const device *);
};
inline const hid_ops *usb_ops;
inline bool usb_complete = true;
inline int usb_write_result = 0, ble_write_result = 0;
inline std::vector<std::vector<uint8_t>> usb_tx, ble_tx;
inline void usb_hid_register_device(const device *,const uint8_t *,size_t,const hid_ops *ops) { usb_ops=ops; }
inline int usb_hid_init(const device *) { return 0; }
inline int hid_int_ep_write(const device *,const uint8_t *p,size_t n,void *) {
    if (usb_write_result) return usb_write_result;
    usb_tx.emplace_back(p,p+n);
    if (usb_complete) usb_ops->int_in_ready(&dev);
    return 0;
}
inline uint16_t sys_le16_to_cpu(uint16_t n) { return n; }
class IWriter { public: virtual void Write(const char *,size_t)=0; virtual ~IWriter()=default; };
class Console { public: static constexpr size_t BUFFER_SIZE=2048; };
'''

HARNESS = r'''
#define CONFIG_ZMK_JAVELIN_STENO_BLE_HID 1
#define CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS 1
#define CONFIG_ZMK_JAVELIN_STENO_HID_PROVISIONING 1
#define CONFIG_ZMK_STENO_SQUID_NATIVE_STORE 1
#include "stubs.h"
extern "C" bool steno_squid_usb_hid_ready() { return usb_ready; }
std::vector<std::string> commands;
bool console_ready = true;
std::string provision_command;
uint32_t provision_session;
extern "C" void steno_squid_native_install_command(const char *command, char *reply, size_t size, uint32_t session) {
    provision_session = session;
    provision_command = command;
    std::snprintf(reply, size, "2 INSTALL");
}
bool steno_squid_javelin_console_line(const char *p,size_t n,IWriter &w,
                                        bool (*cancelled)(void *), void *context, uint32_t) {
    assert(!cancelled(context));
    if (!console_ready) {
        return false;
    }
    commands.emplace_back(p,n);
    std::string reply="ACK "+std::string(p,n)+std::string(140,'x')+"\n\n";
    w.Write(reply.data(),reply.size());
    return true;
}
extern "C" int steno_squid_javelin_ble_session_valid(uint32_t s) { return s==live_session; }
extern "C" int steno_squid_javelin_ble_send(const void *p,size_t n,uint32_t s) {
    if (ble_write_result) return ble_write_result;
    if(s!=live_session) return -ENOTCONN;
    ble_tx.emplace_back((const uint8_t *)p,(const uint8_t *)p+n); return 0;
}
#include "javelin_web_hid.cc"
void reset() {
    commands.clear();usb_tx.clear();ble_tx.clear();received_reports.messages.clear();
    for(auto &b:buffers) reset_buffer(b);
    k_sem_give(&transmit_ready);usb_ready=true;live_session=2;
    console_ready = true;
    provision_command.clear();
    usb_complete = true;
    usb_write_result = ble_write_result = 0;
}
void usb(const std::string &s) {
    usb_setup_packet setup{0x0200};int32_t n=s.size();auto *p=(uint8_t *)s.data();
    assert(output_report(&dev,&setup,&n,&p)==0);
}
void ble(const std::string &s,uint32_t session=2) {
    assert(steno_squid_javelin_ble_receive(s.data(),s.size(),session)==0);
}
void run() { try { hid_worker(nullptr,nullptr,nullptr); } catch(Done &) {} }
int main() {
    initialize_hid();
    reset();usb_ready=false;
    ble("c01 list_diction");usb("c01 hello\n");ble("c01 aries\n");run();
    assert(commands==std::vector<std::string>{"c01 list_dictionaries\n"});
    assert(usb_tx.empty() && ble_tx.size()>=3);
    for(auto &r:ble_tx) assert(r.size()==63);

    reset();usb("c07 lookup_");ble("c07 hello\n");usb("c07 stroke KAT\n");run();
    assert(commands.size()==2 && commands[0]=="c07 hello\n" && commands[1]=="c07 lookup_stroke KAT\n");
    assert(!usb_tx.empty() && !ble_tx.empty());

    reset();ble("c03 add_user_entry KAT stale\n",1);ble("c03 hello\n");run();
    assert(commands==std::vector<std::string>{"c03 hello\n"});

    reset();ble("c02 flash_erase\n");run();assert(commands.empty());
    std::string reply;for(auto &r:ble_tx) for(auto c:r) if(c) reply+=(char)c;
    assert(reply.find("ERR Command unavailable")!=std::string::npos);
    for (const auto *query : {"lookup a", "lookup_prefix 10 a", "lookup_partial_outline KAT 10"}) {
        reset();
        usb(std::string("c06 ") + query + "\n");
        run();
        assert(commands.empty());
        std::string denied;
        for (const auto &report : usb_tx) for (auto byte : report) if (byte) denied += char(byte);
        assert(denied == "c06 ERR Command unavailable on this keyboard\n\n");
    }


    reset();
    const std::string import_line = "native_entry data 0 " + std::string(512, 'a') + "\n";
    for (size_t off=0; off<import_line.size(); off+=59)
        usb("c14 " + import_line.substr(off, 59));
    ble("c15 native_entry commit\n");
    run();
    assert((commands == std::vector<std::string>{
        "c14 " + import_line, "c15 native_entry commit\n"}));
    assert(!ble_tx.empty());

    reset();
    console_ready = false;
    ble("c09 list_dictionaries\n");
    usb("c10 lookup_stroke KAT\n");
    run();
    assert(commands.empty());
    std::string ble_error, usb_error;
    for (const auto &report : ble_tx) {
        for (auto byte : report) if (byte) ble_error += char(byte);
    }
    for (const auto &report : usb_tx) {
        for (auto byte : report) if (byte) usb_error += char(byte);
    }
    assert(ble_error == "c09 ERR Dictionary flash is not ready\n\n");
    assert(usb_error == "c10 ERR Dictionary flash is not ready\n\n");

    reset();console_ready = false;
    usb("c15 native_install status\n");
    run();
    assert(provision_command == "status\n" && provision_session == 0 && commands.empty());
    ble("c15 native_install begin CLEAR 0\n");
    run();
    assert(provision_command == "begin CLEAR 0\n" && provision_session == live_session);
    for (const auto *operation : {"update repack-capabilities", "update recovery-capabilities",
                                  "begin REPACK 0", "begin RECOVERY 0", "match RECOVERY 0"}) {
        ble("c16 native_install " + std::string(operation) + "\n");
        run();
        assert(provision_command == "begin CLEAR 0\n");
    }
    usb_error.clear();ble_error.clear();
    for (const auto &report : usb_tx)
        for (auto byte : report) if (byte) usb_error += char(byte);
    for (const auto &report : ble_tx)
        for (auto byte : report) if (byte) ble_error += char(byte);
    assert(usb_error == "c15 2 INSTALL\n\n");
    std::string expected_ble = "c15 2 INSTALL\n\n";
    for (int i = 0; i < 5; ++i) expected_ble += "c16 ERR Connect over USB for repack or recovery\n\n";
    assert(ble_error == expected_ble);

    reset();ble("c04 add_user_entry KAT ");
    for(int i=0;i<40;i++) ble("c04 "+std::string(59,'a'));
    ble("c04 \n");run();assert(commands.empty());
    reply.clear();for(auto &r:ble_tx) for(auto c:r) if(c) reply+=(char)c;
    assert(reply.find("ERR Command too long")!=std::string::npos);

    reset();usb_ready=false;transmit_ready.count=0;run();
    usb_ready=true;usb("c00 hello\n");run();assert(!usb_tx.empty());

    // A timeout leaves the in-flight USB buffer untouched and cancels the
    // rest of this response. Completion permits a later response to recover.
    reset();
    Report report{};
    HidWriter stalled(report);
    usb_complete = false;
    stalled.Write("first", 5);
    stalled.Flush();
    assert(usb_tx.size() == 1);
    const auto flight = usb_tx[0];
    stalled.Write("second", 6);
    stalled.Flush();
    stalled.Write("third", 5);
    stalled.Flush();
    assert(stalled.Failed() && usb_tx.size() == 1);
    assert(!memcmp(transmit_report, flight.data(), REPORT_BYTES));
    usb_complete = true;
    input_complete(&dev);
    HidWriter resumed(report);
    resumed.Write("ok", 2);
    resumed.Flush();
    assert(!resumed.Failed() && usb_tx.size() == 2);

    reset();
    HidWriter rejected(report);
    usb_write_result = -EIO;
    rejected.Write("bad", 3);
    rejected.Flush();
    usb_write_result = 0;
    rejected.Write("again", 5);
    rejected.Flush();
    assert(rejected.Failed() && usb_tx.empty());

    reset(); report.bluetooth = true; report.session = live_session;
    HidWriter wireless(report);
    ble_write_result = -ETIMEDOUT;
    wireless.Write("bad", 3);
    wireless.Flush();
    ble_write_result = 0;
    wireless.Write("again", 5);
    wireless.Flush();
    assert(wireless.Failed() && ble_tx.empty());

    assert(steno_squid_javelin_ble_receive("x",64,2)==-EINVAL);
    assert(steno_squid_javelin_ble_receive("",0,2)==-EINVAL);
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix="javelin-hid-test-") as temp:
        root = Path(temp)
        (root / "stubs.h").write_text(STUBS, encoding="utf-8")
        for name in ("console.h", "zephyr/device.h", "zephyr/init.h", "zephyr/kernel.h",
                     "zephyr/sys/byteorder.h", "zephyr/usb/class/usb_hid.h", "zmk/usb.h"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#include "stubs.h"\n', encoding="utf-8")
        (root / "test.cc").write_text(HARNESS, encoding="utf-8")
        subprocess.run([*host_compiler('c++'), "-std=c++20", "-I", str(root), "-I",
                        str(ROOT / "src/steno_squid"), str(root / "test.cc"),
                        "-o", str(root / "test")], check=True)
        subprocess.run([str(root / "test")], check=True)
    print("PASS: BLE without USB, transport isolation, stale sessions, allowlist, flash not ready, Bluetooth provisioning and USB-only recovery, overflow, USB reconnect, stalled response and in-flight ownership")


if __name__ == "__main__":
    main()
