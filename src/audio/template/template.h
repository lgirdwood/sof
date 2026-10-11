/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2025 Intel Corporation.
 *
 */

/**
 * @file audio/template/template.h
 * @brief Template audio processing component interface.
 * @ingroup audio_components_api
 */

#ifndef __SOF_AUDIO_TEMPLATE_H__
#define __SOF_AUDIO_TEMPLATE_H__

#include <sof/audio/module_adapter/module/generic.h>
#include <stdbool.h>
#include <stdint.h>

/**
 * @typedef template_func
 * @brief Function call pointer for process function.
 * @param mod Pointer to module data.
 * @param source Source for PCM samples data.
 * @param sink Sink for PCM samples data.
 * @param frames Number of audio data frames to process.
 */
typedef int (*template_func)(const struct processing_module *mod,
			     struct sof_source *source,
			     struct sof_sink *sink,
			     uint32_t frames);

/* Template component private data */

/**
 * @struct template_comp_data
 * @brief Template component private data.
 */
struct template_comp_data {
	template_func template_func; /**< Pointer to used processing function. */
	int channel_map[PLATFORM_MAX_CHANNELS]; /**< Vector with desired sink channels order. */
	int source_format; /**< Source samples format. */
	int frame_bytes; /**< Number of bytes in an audio frame. */
	int channels; /**< Channels count. */
	bool enable; /**< Control processing on/off, on - reorder channels */
};

/**
 * @struct template_proc_fnmap
 * @brief Processing functions for frame formats.
 */
struct template_proc_fnmap {
	enum sof_ipc_frame frame_fmt; /**< Current frame format. */
	template_func template_proc_func; /**< Function pointer for the suitable processing function. */
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
template_func template_find_proc_func(enum sof_ipc_frame src_fmt);

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
 * @param response_size Size of response buffer.
 *
 * This function handles the real-time controls. The ALSA controls have the
 * param_id set to indicate the control type. The control ID, from topology,
 * is used to separate the controls instances of same type. In control payload
 * the num_elems defines to how many channels the control is applied to.
 *
 * @return Zero if success, otherwise error code.
 */
int template_set_config(struct processing_module *mod,
			uint32_t param_id,
			enum module_cfg_fragment_position pos,
			uint32_t data_offset_size,
			const uint8_t *fragment,
			size_t fragment_size,
			uint8_t *response,
			size_t response_size);

/**
 * @brief Handle controls get
 * @param mod Pointer to module data.
 * @param config_id Configuration ID.
 * @param data_offset_size Size of the whole configuration if it is the first or only
 *                    fragment. Otherwise it is offset of the fragment.
 * @param fragment Message payload data.
 * @param fragment_size Size of this fragment.
 *
 * This function is used for controls get.
 *
 * @return Zero if success, otherwise error code.
 */
int template_get_config(struct processing_module *mod,
			uint32_t config_id, uint32_t *data_offset_size,
			uint8_t *fragment, size_t fragment_size);

#endif //  __SOF_AUDIO_TEMPLATE_H__
