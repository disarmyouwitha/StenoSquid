#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

uintptr_t dictionary_flash_mapped_address(void);

enum dictionary_flash_state {
    DICTIONARY_FLASH_LOADING,     /* Still being checked after a boot or wake. */
    DICTIONARY_FLASH_READY,       /* Mapped, or idle and restored on first use. */
    DICTIONARY_FLASH_UNAVAILABLE, /* Missing, unprovisioned, failed or asleep. */
};
enum dictionary_flash_state dictionary_flash_status(void);

/* Hold the flash on while a layer that uses the dictionaries is selected.
 * Taking the hold starts a flash that powered down while idle. Releasing it
 * lets the idle period run again. Never blocks.
 */
void dictionary_flash_hold(bool hold);

/* Weak hook, called when the status above changes. It runs on the flash
 * worker with the flash lock held, so an override must only submit work.
 */
void dictionary_flash_ready_changed(void);

#ifdef CONFIG_ZMK_JAVELIN_BATTERY_LOG
bool dictionary_flash_is_powered(void);
#endif

/* Hold across mapped reads and personal dictionary writes. */
bool dictionary_flash_acquire(void);
void dictionary_flash_release(void);
/* For a streaming reader: always reacquires, even if mapping became unavailable. */
bool dictionary_flash_resume_read(void);
void dictionary_flash_read_session(bool active);

#ifdef __cplusplus
}
#endif
