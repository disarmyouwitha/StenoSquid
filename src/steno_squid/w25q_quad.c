#include "w25q_quad.h"

#include <zephyr/kernel.h>

#define W25Q_STATUS_BUSY_OR_WEL (BIT(0) | BIT(1))

bool squid_qspi_reactivate(void)
{
	/* nRF52840 anomaly 215. Use only task/event registers until READY. */
	nrf_qspi_event_clear(NRF_QSPI, NRF_QSPI_EVENT_READY);
	nrf_qspi_task_trigger(NRF_QSPI, NRF_QSPI_TASK_ACTIVATE);
	for (unsigned int i = 0; i < 1000; i++) {
		if (nrf_qspi_event_check(NRF_QSPI, NRF_QSPI_EVENT_READY)) {
			nrf_qspi_event_clear(NRF_QSPI, NRF_QSPI_EVENT_READY);
			return true;
		}
		k_busy_wait(10);
	}
	return false;
}

nrfx_err_t w25q_status_command(uint8_t opcode, const uint8_t *tx, uint8_t *rx)
{
	const nrf_qspi_cinstr_conf_t command = {
		.opcode = opcode,
		.length = tx || rx ? NRF_QSPI_CINSTR_LEN_2B : NRF_QSPI_CINSTR_LEN_1B,
		.io2_level = true,
		.io3_level = true,
		.wipwait = false,
		.wren = false,
	};

	return nrfx_qspi_cinstr_xfer(&command, tx, rx);
}

nrfx_err_t w25q_set_volatile_sr2(uint8_t value)
{
	uint8_t status;
	nrfx_err_t rc = w25q_status_command(0x05, NULL, &status);

	if (rc != NRFX_SUCCESS) {
		return rc;
	}
	/* A preexisting WEL could turn the SR2 write into a nonvolatile write. */
	if (status & W25Q_STATUS_BUSY_OR_WEL) {
		return NRFX_ERROR_BUSY;
	}

	rc = w25q_status_command(0x50, NULL, NULL);
	if (rc != NRFX_SUCCESS) {
		return rc;
	}
	rc = w25q_status_command(0x31, &value, NULL);
	if (rc != NRFX_SUCCESS) {
		return rc;
	}

	k_msleep(2);
	rc = w25q_status_command(0x35, NULL, &status);
	if (rc != NRFX_SUCCESS) {
		return rc;
	}
	return status == value ? NRFX_SUCCESS : NRFX_ERROR_INTERNAL;
}

nrfx_err_t w25q128_enable_volatile_quad(void)
{
	uint8_t status;
	nrfx_err_t rc = w25q_status_command(0x35, NULL, &status);

	if (rc != NRFX_SUCCESS || (status & W25Q_QUAD_ENABLE)) {
		return rc;
	}
	return w25q_set_volatile_sr2(status | W25Q_QUAD_ENABLE);
}

nrfx_err_t w25q_enable_4byte_addressing(void)
{
	uint8_t status;
	nrfx_err_t rc = w25q_status_command(0xb7, NULL, NULL);
	if (rc != NRFX_SUCCESS) {
		return rc;
	}
	rc = w25q_status_command(0x15, NULL, &status);
	if (rc != NRFX_SUCCESS) {
		return rc;
	}
	return status != 0xff && (status & BIT(0)) ? NRFX_SUCCESS : NRFX_ERROR_INTERNAL;
}

nrfx_err_t squid_qspi_wait_ready(uint32_t timeout_ms)
{
    const int64_t deadline = k_uptime_get() + timeout_ms;
    nrfx_err_t rc;
    while ((rc = nrfx_qspi_mem_busy_check()) == NRFX_ERROR_BUSY && k_uptime_get() < deadline) {
        k_msleep(1);
    }
    return rc;
}
