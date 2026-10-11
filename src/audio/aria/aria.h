/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2022 Intel Corporation. All rights reserved.
 *
 * Author: Adrian Bonislawski <adrian.bonislawski@intel.com>
 */

/**
 * \file aria.h
 * \brief Aria component header file
 * \ingroup audio_components_api
 * \authors Adrian Bonislawski <adrian.bonislawski@intel.com>
 */

#ifndef __SOF_AUDIO_ARIA_H__
#define __SOF_AUDIO_ARIA_H__

#include <sof/audio/component_ext.h>
#include <sof/audio/ipc-config.h>
#include <sof/compiler_attributes.h>
#include <rtos/bit.h>
#include <sof/common.h>
#include <sof/trace/trace.h>
#include <ipc/stream.h>
#include <ipc4/module.h>
#include <sof/audio/module_adapter/module/generic.h>
#include <ipc4/base-config.h>
#include <user/trace.h>
#include <stddef.h>
#include <stdint.h>

/** \brief Aria max gain states */
#define ARIA_MAX_GAIN_STATES 10

/** \brief Aria max att value */
#define ARIA_MAX_ATT 3

/**
 * \brief aria get data function interface
 * \param[in] mod Pointer to processing module.
 * \param[in,out] sink Sink circular buffer to write processed samples to.
 * \param[in] frames Number of frames to process.
 */
typedef void (*aria_get_data_func)(struct processing_module *mod,
				   struct cir_buf_sink *sink, size_t frames);

struct aria_data;

/**
 * \brief Aria gain processing function
 * \param[in,out] cd Pointer to Aria component data.
 * \param[in] gain_idx Index into gain table.
 * \param[in] source Pointer to audio stream source.
 * \param[in] frames Number of frames to process.
 */
void aria_algo_calc_gain(struct aria_data *cd, size_t gain_idx,
			 struct cir_buf_source *source, size_t frames);

/**
 * \brief Get appropriate data processing function for Aria component
 * \param[in] mod Pointer to processing module.
 * \return Function pointer to aria_get_data_func suitable for the module stream format.
 */
aria_get_data_func aria_algo_get_data_func(struct processing_module *mod);

/**
 * \brief Aria component private data.
 */
struct aria_data {
	/* channels count */
	size_t chan_cnt;
	/* sample groups to process count */
	size_t smpl_group_cnt;
	/* Size of chunk (1ms) in samples */
	size_t buff_size;
	/* Current gain state */
	size_t gain_state;
	/* current data position in circular buffer */
	size_t buff_pos;
	/* Attenuation parameter */
	uint32_t att;
	/* Gain states */
	int32_t gains[ARIA_MAX_GAIN_STATES];
	/* cyclic buffer pointer data */
	int32_t *data_ptr;
	/* cyclic buffer end address of data */
	int32_t *data_end;
	/* cyclic buffer start address of data */
	int32_t *data_addr;

	/* internal buffer offset helps to keep algorithmic delay constant */
	size_t offset;

	aria_get_data_func aria_get_data;
};

struct ipc4_aria_module_cfg {
	struct ipc4_base_module_cfg base_cfg;
	uint32_t attenuation;
} __packed __aligned(8);

#endif /* __SOF_AUDIO_ARIA_H__ */
