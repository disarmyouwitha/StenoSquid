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
void steno_squid_native_power_changed(void);
int32_t steno_squid_native_scrub_wait(void);
void steno_squid_native_scrub_step(void);
void steno_squid_native_install_command(const char *command,char *reply,size_t bytes,uint32_t bluetooth_session);
#ifdef __cplusplus
}
#endif
