/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2025 Intel Corporation.
 *
 */

/**
 * @file audio/level_multiplier/level_multiplier.h
 * @brief Level multiplier audio processing component interface.
 * @ingroup audio_components_api
 */

#ifndef __SOF_AUDIO_LEVEL_MULTIPLIER_H__
#define __SOF_AUDIO_LEVEL_MULTIPLIER_H__

#include <sof/audio/module_adapter/module/generic.h>
#include <stdbool.h>
#include <stdint.h>

/** \brief Level multiplier gain Qx.y integer x number of bits including sign bit.
 * With Q8.23 format the gain range is -138.47 to +48.17 dB.
 */
#define LEVEL_MULTIPLIER_QXY_X		9

/** \brief Level multiplier gain Qx.y fractional y number of bits. */
#define LEVEL_MULTIPLIER_QXY_Y		23

/** \brief Level multiplier unity gain */
#define LEVEL_MULTIPLIER_GAIN_ONE (1 << LEVEL_MULTIPLIER_QXY_Y)

/**
 * @typedef level_multiplier_func
 * @brief Function call pointer for process function.
 * @param mod Pointer to module data.
 * @param source Source for PCM samples data.
 * @param sink Sink for PCM samples data.
 * @param frames Number of audio data frames to process.
 */
typedef int (*level_multiplier_func)(const struct processing_module *mod,
				     struct sof_source *source,
				     struct sof_sink *sink,
				     uint32_t frames);

/* Level_Multiplier component private data */

/**
 * @struct level_multiplier_comp_data
 * @brief Level multiplier component private data.
 */
struct level_multiplier_comp_data {
	level_multiplier_func level_multiplier_func; /**< Pointer to used processing function. */
	int32_t gain; /**< Applied gain in linear Q9.23 format. */
	int source_format; /**< Source samples format. */
	int frame_bytes; /**< Number of bytes in an audio frame. */
	int channels; /**< Channels count. */
};

/**
 * @struct level_multiplier_proc_fnmap
 * @brief Processing functions for frame formats.
 */
struct level_multiplier_proc_fnmap {
	enum sof_ipc_frame frame_fmt; /**< Current frame format. */
	level_multiplier_func level_multiplier_proc_func; /**< Function pointer for the suitable processing function. */
};

/**
 * @brief Find suitable processing function.
 * @param src_fmt Enum value for PCM format.
 *
 * This function finds the suitable processing function to use for
 * the used PCM format. If not found, return NULL.
 *
 * @return Pointer to processing function for the requested PCM format.
 */
level_multiplier_func level_multiplier_find_proc_func(enum sof_ipc_frame src_fmt);

/**
 * @brief Handle controls set
 * @param mod Pointer to module data.
 * @param param_id Id to know control type, used to know ALSA control type.
 * @param pos Position of the fragment in the large message.
 * @param data_offset_size Size of the whole configuration if it is the first or only
 *                    fragment. Otherwise it is offset of the fragment.
 * @param fragment Message payload data.
 * @param fragment_size Size of this fragment.
 * @param response Message response data buffer.
 * @param response_size Size of response.
 *
 * This function handles the real-time controls. The ALSA controls have the
 * param_id set to indicate the control type. The control ID, from topology,
 * is used to separate the controls instances of same type. In control payload
 * the num_elems defines to how many channels the control is applied to.
 *
 * @return Zero if success, otherwise error code.
 */

#if CONFIG_IPC_MAJOR_3
static inline int level_multiplier_set_config(struct processing_module *mod,
					      uint32_t param_id,
					      enum module_cfg_fragment_position pos,
					      uint32_t data_offset_size,
					      const uint8_t *fragment,
					      size_t fragment_size,
					      uint8_t *response,
					      size_t response_size)
{
	/* No controls implementation for IPC3, add level_multiplier-ipc3.c
	 * handler if need.
	 */
	return 0;
}
#else
int level_multiplier_set_config(struct processing_module *mod,
				uint32_t param_id,
				enum module_cfg_fragment_position pos,
				uint32_t data_offset_size,
				const uint8_t *fragment,
				size_t fragment_size,
				uint8_t *response,
				size_t response_size);
#endif

#endif //  __SOF_AUDIO_LEVEL_MULTIPLIER_H__
