#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
class Console;
class StenoDictionary;
#ifdef CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS
uint8_t steno_squid_host_layout_index();
#endif
StenoDictionary *steno_squid_native_dictionary(size_t index);
size_t steno_squid_native_dictionary_count();
bool steno_squid_native_dictionary_ready();
bool steno_squid_native_dictionary_enabled(size_t index);
bool steno_squid_native_select(const char *name,int operation,bool *enabled);
void steno_squid_native_register(Console &console,StenoDictionary &parent);
extern "C" {
#endif
// Called by flash startup under flash_lock, after four-line reads are enabled.
bool steno_squid_native_flash_open(void);
#ifdef CONFIG_ZMK_STENO_SQUID_BOOT_PROFILE
void steno_squid_boot_stage(const char *stage, uint32_t detail);
#endif
void steno_squid_native_power_changed(void);
int32_t steno_squid_native_scrub_wait(void);
void steno_squid_native_scrub_step(void);
void steno_squid_native_install_command(const char *command,char *reply,size_t bytes,uint32_t bluetooth_session);
#ifdef CONFIG_ZMK_STENO_SQUID_TRANSFER_SESSION
// One binary-frame report in, packed reply frames out (bytes written). source: 0 USB, else BLE session.
size_t steno_squid_native_transfer_report(const uint8_t *report, size_t size, uint32_t source, uint8_t *out, size_t capacity);
#endif
#ifdef __cplusplus
}
#endif
