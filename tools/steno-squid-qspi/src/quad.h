#ifndef STENO_SQUID_QUAD_H
#define STENO_SQUID_QUAD_H

#include "w25q_quad.h"
#include <nrfx_qspi.h>
#ifdef XIP_PROBE
#include "mapped.h"
#endif

struct quad_result {
	const char *outcome;
	nrfx_err_t error;
	nrfx_err_t restore_error;
	uint32_t compared_bytes;
	uint8_t lane_coverage;
#ifdef XIP_PROBE
	struct mapped_result mapped;
#endif
};

#define QUAD_SAMPLE_SIZE 256

void run_quad_probe(nrfx_qspi_config_t *config, struct quad_result *result,
		    uint32_t start, uint32_t length);

#endif
