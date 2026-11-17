#ifndef STENO_SQUID_W25Q_QUAD_H
#define STENO_SQUID_W25Q_QUAD_H

#include <nrfx_qspi.h>
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

nrfx_err_t squid_qspi_wait_ready(uint32_t timeout_ms);

/* Status Register 2 bit that routes IO2 and IO3 to the data lines. */
#define W25Q_QUAD_ENABLE BIT(1)

/* Takes the QSPI peripheral back from the memory-mapped window. Uses only
 * task and event registers until READY (nRF52840 anomaly 215).
 */
bool squid_qspi_reactivate(void);

/* Sets the volatile Quad Enable bit unless it is already set. */
nrfx_err_t w25q128_enable_volatile_quad(void);

/* One-byte opcode, or two bytes when tx or rx is given. */
nrfx_err_t w25q_status_command(uint8_t opcode, const uint8_t *tx, uint8_t *rx);

/* Writes Status Register 2 without touching nonvolatile bits, and checks it. */
nrfx_err_t w25q_set_volatile_sr2(uint8_t value);

#ifdef __cplusplus
}
#endif

#endif
