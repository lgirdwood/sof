/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2025 Intel Corporation.
 *
 */

/**
 * @file audio/sound_dose/sound_dose.h
 * @brief Sound dose audio processing component interface.
 * @ingroup audio_components_api
 */

#ifndef __SOF_AUDIO_SOUND_DOSE_H__
#define __SOF_AUDIO_SOUND_DOSE_H__

#include <sof/audio/module_adapter/module/generic.h>
#include <sof/math/iir_df1.h>
#include <user/sound_dose.h>
#include <stdbool.h>
#include <stdint.h>

#define SOUND_DOSE_DEBUG	0

#define SOUND_DOSE_1M_OVER_44K_Q26		1521742948	/* int32(1000/44.1 * 2^26) */
#define SOUND_DOSE_1M_OVER_48K_Q26		1398101333	/* int32(1000/48 * 2^26) */
#define SOUND_DOSE_ONE_OVER_100_Q24		167772		/* int32(0.01*2^24) */
#define SOUND_DOSE_GAIN_ONE_Q30			1073741824	/* int32(2^30) */
#define SOUND_DOSE_GAIN_UP_Q30			1079940603	/* int32(10^(+0.05/20)*2^30) */
#define SOUND_DOSE_GAIN_DOWN_Q30		1067578625	/* int32(10^(-0.05/20)*2^30) */
#define SOUND_DOSE_LOG2_INV_44K_Q16		-1011122	/* int32(log2(1/44.1e3) * 2^16) */
#define SOUND_DOSE_LOG2_INV_48K_Q16		-1019134	/* int32(log2(1/48e3) * 2^16) */
#define SOUND_DOSE_TEN_OVER_LOG2_10_Q29		1616142483	/* int32(10 / log2(10) * 2^29) */
#define SOUND_DOSE_WEIGHT_FILTERS_OFFS_Q16	196608		/* int32(3 * 2^16) */
#define SOUND_DOSE_DFBS_OFFS_Q16		197263		/* int32(3.01 * 2^16) */
#define SOUND_DOSE_MEL_CHANNELS_SUM_FIX		-98304		/* int32(-1.5 * 2^16) */
#define SOUND_DOSE_ENERGY_SHIFT			19		/* Scale shift for 1s energy */
#define SOUND_DOSE_LOG_FIXED_OFFSET		(65536 * (SOUND_DOSE_ENERGY_SHIFT - 30))

#define SOUND_DOSE_S16_Q	15	/* Q1.15 samples */
#define SOUND_DOSE_S32_Q	31	/* Q1.31 samples */
#define SOUND_DOSE_GAIN_Q	30	/* Q2.30 gain */
#define SOUND_DOSE_LOGOFFS_Q	16	/* see SOUND_DOSE_LOG2_INV_48K_Q16 */
#define SOUND_DOSE_LOGMULT_Q	29	/* see SOUND_DOSE_TEN_OVER_LOG2_10_Q29 */

/**
 * @typedef sound_dose_func
 * @brief Function call pointer for process function.
 * @param mod Pointer to module data.
 * @param source Source for PCM samples data.
 * @param sink Sink for PCM samples data.
 * @param frames Number of audio data frames to process.
 */
typedef int (*sound_dose_func)(const struct processing_module *mod,
	struct sof_source *source,
	struct sof_sink *sink,
	uint32_t frames);

/* Sound Dose component private data */

/**
 * @struct sound_dose_comp_data
 * @brief Sound dose component private data.
 */
struct sound_dose_comp_data {
	struct iir_state_df1 iir[PLATFORM_MAX_CHANNELS]; /**< IIR filter state per channel. */
	struct sound_dose_setup_config setup; /**< Setup configuration. */
	struct sound_dose_volume_config vol; /**< Volume configuration. */
	struct sound_dose_gain_config att; /**< Gain configuration. */
	struct sof_abi_hdr *abi; /**< ABI header. */
	struct sof_audio_feature *feature; /**< Audio feature descriptor. */
	struct sof_sound_dose *dose; /**< Sound dose descriptor. */
	struct ipc_msg *msg; /**< IPC message. */
	sound_dose_func sound_dose_func; /**< Pointer to used processing function. */
	int64_t energy[PLATFORM_MAX_CHANNELS]; /**< Energy accumulator per channel. */
	int64_t total_frames_count; /**< Total processed frames count. */
	int32_t log_offset_for_mean; /**< Logarithmic offset for mean. */
	int32_t rate_to_us_coef; /**< Sample rate conversion coefficient. */
	int32_t *delay_lines; /**< Delay lines buffer pointer. */
	int32_t level_dbfs; /**< Measured level in dBFS. */
	int32_t new_gain; /**< New gain target. */
	int32_t gain; /**< Current active gain. */
	bool gain_update; /**< Flag indicating gain update requested. */
	int report_count; /**< Reports counter. */
	int frames_count; /**< Current frame counter. */
	int frame_bytes; /**< Bytes per frame. */
	int channels; /**< Channels count. */
	int rate; /**< Sample rate. */
};

/**
 * @struct sound_dose_proc_fnmap
 * @brief Processing functions for frame formats.
 */
struct sound_dose_proc_fnmap {
	enum sof_ipc_frame frame_fmt; /**< Current frame format. */
	sound_dose_func sound_dose_proc_func; /**< Function pointer for the suitable processing function. */
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
sound_dose_func sound_dose_find_proc_func(enum sof_ipc_frame src_fmt);

/**
 * @brief Handle controls set request
 * @param mod Pointer to module data.
 * @param param_id Id to know control type, used to know ALSA control type.
 * @param pos Position of the fragment in the large message.
 * @param data_offset_size Size of the whole configuration if it is the first or only
 *                    fragment. Otherwise it is offset of the fragment.
 * @param fragment Message payload data.
 * @param fragment_size Size of this fragment.
 * @param response Message response buffer.
 * @param response_size Size of response buffer.
 *
 * This function handles the real-time controls. The ALSA controls have the
 * param_id set to indicate the control type. The control ID, from topology,
 * is used to separate the controls instances of same type. In control payload
 * the num_elems defines to how many channels the control is applied to.
 *
 * @return Zero if success, otherwise error code.
 */
int sound_dose_set_config(struct processing_module *mod,
			  uint32_t param_id,
			  enum module_cfg_fragment_position pos,
			  uint32_t data_offset_size,
			  const uint8_t *fragment,
			  size_t fragment_size,
			  uint8_t *response,
			  size_t response_size);

/**
 * @brief Handle controls get request
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
int sound_dose_get_config(struct processing_module *mod,
			  uint32_t config_id, uint32_t *data_offset_size,
			  uint8_t *fragment, size_t fragment_size);

void sound_dose_filters_free(struct sound_dose_comp_data *cd);
int sound_dose_filters_init(struct processing_module *mod);
void sound_dose_report_mel(const struct processing_module *mod);
int sound_dose_ipc_notification_init(struct processing_module *mod);

#endif //  __SOF_AUDIO_SOUND_DOSE_H__
