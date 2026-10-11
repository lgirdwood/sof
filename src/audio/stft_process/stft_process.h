/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2025 Intel Corporation.
 *
 */

/**
 * \file audio/stft_process/stft_process.h
 * \brief Short-Time Fourier Transform (STFT) processing module header
 * \ingroup audio_components_api
 */

#ifndef __SOF_AUDIO_STFT_PROCESS_H__
#define __SOF_AUDIO_STFT_PROCESS_H__

#include <sof/audio/module_adapter/module/generic.h>
#include <sof/math/auditory.h>
#include <sof/math/dct.h>
#include <sof/math/fft.h>

#include <stdbool.h>
#include <stdint.h>

#if CONFIG_LIBRARY
#define STFT_DEBUG	0	/* Keep zero, produces large files with fprintf() */
#else
#define STFT_DEBUG	0
#endif

#define SOF_STFT_PROCESS_CONFIG_MAX_SIZE 256 /* Max size for configuration data in bytes */

enum sof_stft_process_fft_pad_type {
	STFT_PAD_END = 0,
	STFT_PAD_CENTER = 1,
	STFT_PAD_START = 2,
};

enum sof_stft_process_fft_window_type {
	STFT_RECTANGULAR_WINDOW = 0,
	STFT_BLACKMAN_WINDOW = 1,
	STFT_HAMMING_WINDOW = 2,
	STFT_HANN_WINDOW = 3,
	STFT_POVEY_WINDOW = 4,
};

struct sof_stft_process_config {
	uint32_t size; /**< Size of this struct in bytes */
	uint32_t reserved[8];
	int32_t sample_frequency; /**< Hz. e.g. 16000 */
	int32_t window_gain_comp; /**< Q1.31 gain for IFFT */
	int32_t reserved_32;
	int16_t channel; /**< -1 expect mono, 0 left, 1 right, ... */
	int16_t frame_length; /**< samples, e.g. 400 for 25 ms @ 16 kHz*/
	int16_t frame_shift; /**< samples, e.g. 160 for 10 ms @ 16 kHz */
	int16_t reserved_16;
	enum sof_stft_process_fft_pad_type pad; /**< Use PAD_END, PAD_CENTER, PAD_START */
	enum sof_stft_process_fft_window_type window; /**< Use RECTANGULAR_WINDOW, etc. */
} __attribute__((packed));

struct stft_process_buffer {
	int32_t *addr;
	int32_t *end_addr;
	int32_t *r_ptr;
	int32_t *w_ptr;
	int s_avail; /**< samples count */
	int s_free; /**< samples count */
	int s_length; /**< length in samples for wrap */
};

struct stft_process_fft {
	struct icomplex32 *fft_buf; /**< fft_padded_size */
	struct icomplex32 *fft_out; /**< fft_padded_size */
	struct ipolar32 *fft_polar;
	struct fft_multi_plan *fft_plan;
	struct fft_multi_plan *ifft_plan;
	int fft_size;
	int fft_padded_size;
	int fft_hop_size;
	int fft_buf_size;
	int half_fft_size;
	size_t fft_buffer_size; /**< bytes */
};

struct stft_process_state {
	struct stft_process_buffer ibuf[PLATFORM_MAX_CHANNELS]; /**< Buffer for input data */
	struct stft_process_buffer obuf[PLATFORM_MAX_CHANNELS]; /**< Buffer for output data */
	struct stft_process_fft fft; /**< FFT related */
	int32_t *prev_data[PLATFORM_MAX_CHANNELS]; /**< prev_data_size */
	int32_t gain_comp; /**< Gain to compensate window gain */
	int32_t *buffers;
	int32_t *window; /**< fft_size */
	int source_channel;
	int prev_data_size;
	int sample_rate;
	bool waiting_fill; /**< booleans */
	bool prev_samples_valid;
};

/**
 * @brief Function pointer for STFT audio processing function
 * @param mod Pointer to module data.
 * @param source Source for PCM samples data.
 * @param sink Sink for PCM samples data.
 * @param frames Number of audio data frames to process.
 * @return Zero on success, negative error code on failure.
 */
typedef int (*stft_process_func)(const struct processing_module *mod,
			     struct sof_source *source,
			     struct sof_sink *sink,
			     uint32_t frames);

/**
 * @struct stft_comp_data
 * @brief STFT component private data.
 */
struct stft_comp_data {
	stft_process_func stft_process_func; /**< Processing function */
	struct stft_process_state state; /**< STFT processing state */
	struct sof_stft_process_config *config; /**< STFT configuration */
	size_t frame_bytes; /**< Frame size in bytes */
	int source_channel; /**< Source channel index */
	int max_frames; /**< Maximum number of frames */
	int channels; /**< Channel count */
	bool fft_done; /**< FFT execution flag */
};

/**
 * @brief Calculate number of samples available in buffer without wrapping
 * @param buffer Pointer to STFT circular buffer.
 * @param ptr Current read or write pointer within buffer.
 * @return Number of contiguous samples before the end of the buffer.
 */
static inline int stft_process_buffer_samples_without_wrap(struct stft_process_buffer *buffer,
							   int32_t *ptr)
{
	return buffer->end_addr - ptr;
}

/**
 * @brief Wrap buffer pointer if it reaches or exceeds the buffer end address
 * @param buffer Pointer to STFT circular buffer.
 * @param ptr Pointer to wrap.
 * @return Wrapped pointer within the buffer boundary.
 */
static inline int32_t *stft_process_buffer_wrap(struct stft_process_buffer *buffer, int32_t *ptr)
{
	if (ptr >= buffer->end_addr)
		ptr -= buffer->s_length;

	return ptr;
}

/**
 * @struct stft_process_proc_fnmap
 * @brief Processing functions for frame formats.
 */
struct stft_process_proc_fnmap {
	enum sof_ipc_frame frame_fmt; /**< Current frame format */
	stft_process_func stft_process_function; /**< Function pointer for the suitable processing function */
};

/**
 * @brief Find suitable processing function.
 * @param src_fmt Enum value for PCM format.
 * @return Pointer to processing function for the requested PCM format, or NULL if not found.
 */
stft_process_func stft_process_find_proc_func(enum sof_ipc_frame src_fmt);

#if CONFIG_IPC_MAJOR_4
/**
 * @brief Handle controls set for STFT process module.
 * @param mod Pointer to module data.
 * @param param_id Id to know control type, used to know ALSA control type.
 * @param pos Position of the fragment in the large message.
 * @param data_offset_size Size of the whole configuration if it is the first or only
 *                         fragment. Otherwise it is offset of the fragment.
 * @param fragment Message payload data.
 * @param fragment_size Size of this fragment.
 * @param response Size of response buffer.
 * @param response_size Size of response.
 * @return Zero if success, otherwise error code.
 */
int stft_process_set_config(struct processing_module *mod, uint32_t param_id,
			    enum module_cfg_fragment_position pos, uint32_t data_offset_size,
			    const uint8_t *fragment, size_t fragment_size,
			    uint8_t *response, size_t response_size);
/**
 * @brief Handle controls get for STFT process module.
 * @param mod Pointer to module data.
 * @param config_id Configuration ID.
 * @param data_offset_size Size of the whole configuration if it is the first or only
 *                         fragment. Otherwise it is offset of the fragment.
 * @param fragment Message payload data.
 * @param fragment_size Size of this fragment.
 * @return Zero if success, otherwise error code.
 */
int stft_process_get_config(struct processing_module *mod, uint32_t config_id,
			    uint32_t *data_offset_size, uint8_t *fragment, size_t fragment_size);
#else
static inline int stft_process_set_config(struct processing_module *mod, uint32_t param_id,
					  enum module_cfg_fragment_position pos,
					  uint32_t data_offset_size,
					  const uint8_t *fragment, size_t fragment_size,
					  uint8_t *response, size_t response_size)
{
	return 0;
}

static inline int stft_process_get_config(struct processing_module *mod, uint32_t config_id,
					  uint32_t *data_offset_size, uint8_t *fragment,
					  size_t fragment_size)
{
	return 0;
}
#endif

/**
 * @brief Initialize STFT processing state and allocate buffers.
 * @param mod Pointer to processing module.
 * @param max_frames Maximum number of frames per processing call.
 * @param rate Audio sample rate in Hz.
 * @param channels Number of audio channels.
 * @return Zero on success, otherwise a negative error code.
 */
int stft_process_setup(struct processing_module *mod, int max_frames, int rate, int channels);

/**
 * @brief Copy S16_LE source data to STFT internal buffers.
 * @param cd STFT component data.
 * @param source Source for PCM samples data.
 * @param frames Number of audio data frames to process.
 * @return Zero on success, otherwise an error code.
 */
int stft_process_source_s16(struct stft_comp_data *cd, struct sof_source *source, int frames);

/**
 * @brief Copy STFT internal buffers to S16_LE sink.
 * @param cd STFT component data.
 * @param sink Sink for PCM samples data.
 * @param frames Number of audio data frames to produce.
 * @return Zero on success, otherwise an error code.
 */
int stft_process_sink_s16(struct stft_comp_data *cd, struct sof_sink *sink, int frames);

/**
 * @brief Copy S32_LE source data to STFT internal buffers.
 * @param cd STFT component data.
 * @param source Source for PCM samples data.
 * @param frames Number of audio data frames to process.
 * @return Zero on success, otherwise an error code.
 */
int stft_process_source_s32(struct stft_comp_data *cd, struct sof_source *source, int frames);

/**
 * @brief Copy STFT internal buffers to S32_LE sink.
 * @param cd STFT component data.
 * @param sink Sink for PCM samples data.
 * @param frames Number of audio data frames to produce.
 * @return Zero on success, otherwise an error code.
 */
int stft_process_sink_s32(struct stft_comp_data *cd, struct sof_sink *sink, int frames);

/**
 * @brief Free all STFT processing buffers.
 * @param mod Pointer to processing module.
 */
void stft_process_free_buffers(struct processing_module *mod);

/**
 * @brief Save overlap samples for next STFT frame.
 * @param buf Circular buffer to read overlap samples from.
 * @param prev_data Destination array for the overlap data.
 * @param prev_data_length Number of samples to copy.
 */
void stft_process_fill_prev_samples(struct stft_process_buffer *buf, int32_t *prev_data,
				    int prev_data_length);

/**
 * @brief Assemble FFT input from overlap and new data.
 * @param state STFT processing state.
 * @param ch Channel index.
 */
void stft_process_fill_fft_buffer(struct stft_process_state *state, int ch);

/**
 * @brief Multiply FFT buffer by the analysis window.
 * @param state STFT processing state that contains the FFT buffer and window.
 */
void stft_process_apply_window(struct stft_process_state *state);

/**
 * @brief Overlap-add IFFT output to circular output buffer.
 * @param state STFT processing state.
 * @param ch Channel index.
 */
void stft_process_overlap_add_ifft_buffer(struct stft_process_state *state, int ch);

#endif //  __SOF_AUDIO_STFT_PROCESS_H__
