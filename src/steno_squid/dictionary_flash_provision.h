#pragma once

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Called only by the wired HID command worker, before the Javelin engine. */
void lf_provision_command(const char *command, char *reply, size_t reply_size);

#ifdef __cplusplus
}
#endif
