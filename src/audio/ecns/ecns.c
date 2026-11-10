/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2026 Intel Corporation. All rights reserved.
 */

#include <sof/audio/component.h>
#include <sof/audio/audio_stream.h>
#include <sof/audio/format.h>
#include <sof/audio/module_adapter/module/generic.h>
#include <sof/audio/ecns.h>
#include <sof/common.h>
#include <sof/math/iir_df1.h>
#include <sof/trace/trace.h>
#include <module/audio/sink_api.h>
#include <module/audio/source_api.h>
#include <ipc4/base_fw.h>
#include <ipc4/header.h>
#include <ipc/stream.h>
#include <ipc4/base-config.h>
#include <user/eq.h>
#include <rtos/init.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

LOG_MODULE_REGISTER(ecns, CONFIG_SOF_LOG_LEVEL);

SOF_DEFINE_REG_UUID(ecns);

/* Determine the source-channel averaging window [start, start+cnt) used to
 * downmix the pin0 input to the mono KPB detection output. Selection is a
 * Kconfig choice; out-of-range channels fall back to all available channels.
 */
static inline void ecns_mono_window(uint32_t ch, uint32_t *start, uint32_t *cnt)
{
#if CONFIG_ECNS_KPB_MONO_AVG_CH_1_2
	*start = 0;
	*cnt = MIN(2, ch);
#elif CONFIG_ECNS_KPB_MONO_AVG_CH_3_4
	if (ch > 2) {
		*start = 2;
		*cnt = MIN(2, ch - 2);
	} else {
		*start = 0;
		*cnt = ch;
	}
#else /* CONFIG_ECNS_KPB_MONO_AVG_ALL */
	*start = 0;
	*cnt = ch;
#endif
	if (*cnt == 0)
		*cnt = 1;
}

static inline int32_t ecns_mono_s32(const int32_t *frame, uint32_t start, uint32_t cnt)
{
	int64_t acc = 0;

	for (uint32_t c = 0; c < cnt; c++)
		acc += frame[start + c];
	return (int32_t)(acc / (int32_t)cnt);
}

static inline int16_t ecns_mono_s16(const int16_t *frame, uint32_t start, uint32_t cnt)
{
	int32_t acc = 0;

	for (uint32_t c = 0; c < cnt; c++)
		acc += frame[start + c];
	return (int16_t)(acc / (int32_t)cnt);
}

#if CONFIG_ECNS_KPB_HPF
/**
 * \brief 100 Hz second-order high-pass filter with +30 dB gain at 16 kHz.
 *
 * Built-in coefficient set for the KPB detection path (Pin 0 mono mix).
 * Converted from tools/topology/topology2/include/components/eqiir/highpass_100hz_30db_16khz.conf
 * consisting of struct sof_eq_iir_header (6 words) followed by one
 * struct sof_eq_iir_biquad (7 words):
 *   header: num_sections=1, num_sections_in_series=1, reserved[4]={0,0,0,0}
 *   biquad: a2, a1, b2, b1, b0 in Q2.30, shift=-5, gain=0x7e33 (32307 in Q2.14)
 */
static const uint32_t hp_100hz_30db_16k[13] = {
	0x00000001, 0x00000001, 0x00000000, 0x00000000,
	0x00000000, 0x00000000, 0xc37518f5, 0x7c72526b,
	0x1f32926e, 0xc19adb24, 0x1f32926e, 0xfffffffb,
	0x00007e33
};

/** \brief As hp_100hz_30db_16k with 0 dB gain, from highpass_100hz_0db_16khz.conf. */
static const uint32_t hp_100hz_0db_16k[13] = {
	0x00000001, 0x00000001, 0x00000000, 0x00000000,
	0x00000000, 0x00000000, 0xc37518f5, 0x7c72526b,
	0x1f32926e, 0xc19adb24, 0x1f32926e, 0x00000000,
	0x00007fb4
};
#endif

#if CONFIG_ECNS_HOST_HPF
/**
 * \brief 40 Hz second-order high-pass filters with +20 dB gain for Pin 1.
 *
 * Converted from highpass_40hz_20db_48khz.conf and highpass_40hz_20db_16khz.conf
 * in tools/topology/topology2/include/components/eqiir/ with the same layout
 * as hp_100hz_30db_16k.
 */
static const uint32_t hp_40hz_20db_48k[13] = {
	0x00000001, 0x00000001, 0x00000000, 0x00000000,
	0x00000000, 0x00000000, 0xc078df4c, 0x7f86ae23,
	0x1ff51b12, 0xc015c9db, 0x1ff51b12, 0xfffffffc,
	0x00004fd0
};

static const uint32_t hp_40hz_20db_16k[13] = {
	0x00000001, 0x00000001, 0x00000000, 0x00000000,
	0x00000000, 0x00000000, 0xc167f2ba, 0x7e940dc3,
	0x1fb8bfff, 0xc08e8003, 0x1fb8bfff, 0xfffffffc,
	0x00004fd0
};

/**
 * \brief 40 Hz high-pass filters with 0 dB gain for Pin 1.
 *
 * From highpass_40hz_0db_48khz.conf and highpass_40hz_0db_16khz.conf.
 */
static const uint32_t hp_40hz_0db_48k[13] = {
	0x00000001, 0x00000001, 0x00000000, 0x00000000,
	0x00000000, 0x00000000, 0xc078df4c, 0x7f86ae23,
	0x1ff51b12, 0xc015c9db, 0x1ff51b12, 0x00000000,
	0x00007fb2
};

static const uint32_t hp_40hz_0db_16k[13] = {
	0x00000001, 0x00000001, 0x00000000, 0x00000000,
	0x00000000, 0x00000000, 0xc167f2ba, 0x7e940dc3,
	0x1fb8bfff, 0xc08e8003, 0x1fb8bfff, 0x00000000,
	0x00007fb2
};

#define ECNS_HOST_MAX_CHANNELS	4
#endif

/* Switch control indexes, mixer."1" and mixer."2" in ecns.conf */
#define ECNS_CTRL_INDEX_CAPTURE_BOOST	0
#define ECNS_CTRL_INDEX_TEST_SIGNAL	1

struct ecns_comp_data {
	uint32_t sample_rate;
	uint32_t channels;
	uint32_t sample_width;
	uint32_t test_signal_enabled;
	bool capture_boost;
	bool capture_boost_req;
	uint16_t test_signal_val;
	struct iir_state_df1 hp_iir;
	int32_t *hp_delay;
#if CONFIG_ECNS_HOST_HPF
	struct iir_state_df1 host_iir[ECNS_HOST_MAX_CHANNELS];
	int32_t *host_delay;
	int host_delay_size;
	uint32_t host_rate;
#endif
};

#if CONFIG_ECNS_KPB_HPF
static struct sof_eq_iir_header *ecns_kpb_iir_coef(struct ecns_comp_data *cd)
{
	return (struct sof_eq_iir_header *)
		(cd->capture_boost ? hp_100hz_30db_16k : hp_100hz_0db_16k);
}
#endif

#if CONFIG_ECNS_HOST_HPF
static struct sof_eq_iir_header *ecns_host_iir_coef(struct ecns_comp_data *cd)
{
	if (cd->host_rate == 16000)
		return (struct sof_eq_iir_header *)
			(cd->capture_boost ? hp_40hz_20db_16k : hp_40hz_0db_16k);

	return (struct sof_eq_iir_header *)
		(cd->capture_boost ? hp_40hz_20db_48k : hp_40hz_0db_48k);
}
#endif

/* Only the coefficient pointers move, the filter delay lines are kept. */
static void ecns_update_iir_coef(struct ecns_comp_data *cd)
{
#if CONFIG_ECNS_KPB_HPF
	iir_init_coef_df1(&cd->hp_iir, ecns_kpb_iir_coef(cd));
#endif
#if CONFIG_ECNS_HOST_HPF
	for (int c = 0; c < ECNS_HOST_MAX_CHANNELS; c++)
		iir_init_coef_df1(&cd->host_iir[c], ecns_host_iir_coef(cd));
#endif
}

static int ecns_init(struct processing_module *mod)
{
	const struct ipc4_base_module_cfg *base_cfg = &mod->priv.cfg.base_cfg;
	struct comp_dev *dev = mod->dev;
	struct ecns_comp_data *cd;

	cd = mod_zalloc(mod, sizeof(*cd));
	if (!cd)
		return -ENOMEM;

	cd->sample_rate  = base_cfg->audio_fmt.sampling_frequency;
	cd->channels     = base_cfg->audio_fmt.channels_count;
	cd->sample_width = base_cfg->audio_fmt.valid_bit_depth;

	module_set_private_data(mod, cd);
	/* 16 kHz and 48 kHz inputs, KPB and host outputs */
	mod->max_sources = 2;
	mod->max_sinks = 2;

	/* Boosted until the "ECNS capture boost" control says otherwise */
	cd->capture_boost = true;
	cd->capture_boost_req = true;

#if CONFIG_ECNS_KPB_HPF
	struct sof_eq_iir_header *hp_hdr = ecns_kpb_iir_coef(cd);
	int hp_size = iir_delay_size_df1(hp_hdr);
	int32_t *delay;

	if (hp_size < 0)
		goto err;

	cd->hp_delay = mod_zalloc(mod, hp_size);
	if (!cd->hp_delay)
		goto err;

	delay = cd->hp_delay;
	iir_init_coef_df1(&cd->hp_iir, hp_hdr);
	iir_init_delay_df1(&cd->hp_iir, &delay);
#endif

#if CONFIG_ECNS_HOST_HPF
	/* The 48 kHz and 16 kHz Pin 1 filters have the same section count */
	int host_size = iir_delay_size_df1((struct sof_eq_iir_header *)hp_40hz_20db_48k);

	if (host_size < 0)
		goto err;

	cd->host_delay_size = host_size * ECNS_HOST_MAX_CHANNELS;
	cd->host_delay = mod_zalloc(mod, cd->host_delay_size);
	if (!cd->host_delay)
		goto err;
#endif

	comp_info(dev, "ecns_init: rate=%u in_ch=%u sample_width=%u",
		  cd->sample_rate, cd->channels, cd->sample_width);

	return 0;

#if CONFIG_ECNS_KPB_HPF || CONFIG_ECNS_HOST_HPF
err:
	mod_free(mod, cd->hp_delay);
	mod_free(mod, cd);
	return -ENOMEM;
#endif
}

static int ecns_free(struct processing_module *mod)
{
	struct ecns_comp_data *cd = module_get_private_data(mod);

	comp_info(mod->dev, "ecns_free");
#if CONFIG_ECNS_HOST_HPF
	mod_free(mod, cd->host_delay);
#endif
	mod_free(mod, cd->hp_delay);
	mod_free(mod, cd);

	return 0;
}

#if CONFIG_ECNS_HOST_HPF
static void ecns_host_iir_init(struct ecns_comp_data *cd, uint32_t rate)
{
	struct sof_eq_iir_header *hdr;
	int32_t *delay = cd->host_delay;

	cd->host_rate = rate;
	hdr = ecns_host_iir_coef(cd);
	memset_s(cd->host_delay, cd->host_delay_size, 0, cd->host_delay_size);
	for (int c = 0; c < ECNS_HOST_MAX_CHANNELS; c++) {
		iir_init_coef_df1(&cd->host_iir[c], hdr);
		iir_init_delay_df1(&cd->host_iir[c], &delay);
	}
}

static inline int32_t ecns_host_filter(struct ecns_comp_data *cd, uint32_t c, int32_t x)
{
	return c < ECNS_HOST_MAX_CHANNELS ? iir_df1(&cd->host_iir[c], x) : x;
}
#else
static void ecns_host_iir_init(struct ecns_comp_data *cd, uint32_t rate)
{
}

static inline int32_t ecns_host_filter(struct ecns_comp_data *cd, uint32_t c, int32_t x)
{
	return x;
}
#endif

/* Find the sources and sinks of the ECNS pins */
static void ecns_get_pins(struct sof_source **sources, int num_of_sources,
			  struct sof_sink **sinks, int num_of_sinks,
			  struct sof_source **src0, struct sof_source **src1,
			  struct sof_sink **snk0, struct sof_sink **snk1)
{
	int i;

	*src0 = NULL;
	*src1 = NULL;
	*snk0 = NULL;
	*snk1 = NULL;

	for (i = 0; i < num_of_sources; i++) {
		uint32_t pin = IPC4_SINK_QUEUE_ID(source_get_id(sources[i]));

		if (pin == ECNS_PIN_16K_IN && !*src0)
			*src0 = sources[i];
		else if (pin == ECNS_PIN_48K_IN && !*src1)
			*src1 = sources[i];
	}

	for (i = 0; i < num_of_sinks; i++) {
		uint32_t pin = IPC4_SRC_QUEUE_ID(sink_get_id(sinks[i]));

		if (pin == ECNS_PIN_16K_OUT && !*snk0)
			*snk0 = sinks[i];
		else if (pin == ECNS_PIN_48K_OUT && !*snk1)
			*snk1 = sinks[i];
	}
}

static int ecns_prepare(struct processing_module *mod,
			struct sof_source **sources, int num_of_sources,
			struct sof_sink **sinks, int num_of_sinks)
{
	struct ecns_comp_data *cd = module_get_private_data(mod);
	struct sof_source *src0, *src1;
	struct sof_sink *snk0, *snk1;

	comp_info(mod->dev, "ecns_prepare");

	ecns_get_pins(sources, num_of_sources, sinks, num_of_sinks, &src0, &src1, &snk0, &snk1);

	/* Output buffer formats must match the topology-declared pins:
	 * pin0 -> KPB mono 16 kHz, pin1 -> host.
	 * When src1 (48 kHz producer) is present, use dual-rate 16-bit formats.
	 * When src1 is absent (single 16 kHz DMIC input), use 32-bit formats matching the manifest.
	 */

	/* Pin 1 output follows the dmic01 input channel count (2 or 4) */
	uint32_t pin1_ch = 2;

	if (src1)
		pin1_ch = source_get_channels(src1) == 2 ? 2 : 4;

	struct ipc4_audio_format pin0_fmt = {
		.sampling_frequency = 16000,
		.channels_count = 1,
		.depth = src1 ? 16 : 32,
		.valid_bit_depth = src1 ? 16 : 32,
		.s_type = src1 ? IPC4_TYPE_SIGNED_INTEGER : IPC4_TYPE_MSB_INTEGER,
		.interleaving_style = IPC4_CHANNELS_INTERLEAVED,
		.ch_map = ECNS_CH_MAP_MONO,
		.ch_cfg = IPC4_CHANNEL_CONFIG_MONO,
	};
	struct ipc4_audio_format pin1_fmt = {
		.sampling_frequency = src1 ? 48000 : 16000,
		.channels_count = pin1_ch,
		.depth = src1 ? 16 : 32,
		.valid_bit_depth = src1 ? 16 : 32,
		.s_type = src1 ? IPC4_TYPE_SIGNED_INTEGER : IPC4_TYPE_MSB_INTEGER,
		.interleaving_style = IPC4_CHANNELS_INTERLEAVED,
		.ch_map = pin1_ch == 4 ? ECNS_CH_MAP_4CH : ECNS_CH_MAP_STEREO,
		.ch_cfg = pin1_ch == 4 ? IPC4_CHANNEL_CONFIG_3_POINT_1 : IPC4_CHANNEL_CONFIG_STEREO,
	};

	if (snk0)
		ipc4_update_sink_format(snk0, &pin0_fmt);
	if (snk1)
		ipc4_update_sink_format(snk1, &pin1_fmt);

	cd->capture_boost = cd->capture_boost_req;
	ecns_update_iir_coef(cd);
	ecns_host_iir_init(cd, pin1_fmt.sampling_frequency);

	return 0;
}

static int ecns_reset(struct processing_module *mod)
{
	struct ecns_comp_data *cd = module_get_private_data(mod);

	comp_info(mod->dev, "ecns_reset");
	cd->test_signal_val = 0;
#if CONFIG_ECNS_KPB_HPF
	if (cd->hp_delay) {
		struct sof_eq_iir_header *hp_hdr = ecns_kpb_iir_coef(cd);
		int hp_size = iir_delay_size_df1(hp_hdr);

		if (hp_size > 0)
			memset_s(cd->hp_delay, hp_size, 0, hp_size);
	}
#endif
#if CONFIG_ECNS_HOST_HPF
	if (cd->host_delay)
		memset_s(cd->host_delay, cd->host_delay_size, 0, cd->host_delay_size);
#endif
	return 0;
}

/* A circular buffer region from the source or sink API */
struct ecns_buf {
	void *ptr;
	void *start;
	void *end;
	uint32_t channels;
	uint32_t sample_bytes;
};

/* Frames that fit before the end of the buffer */
static inline uint32_t ecns_frames_without_wrap(const struct ecns_buf *b, const void *ptr)
{
	return ((const uint8_t *)b->end - (const uint8_t *)ptr) /
	       (b->channels * b->sample_bytes);
}

#if CONFIG_ECNS_TEST_SIGNAL
/**
 * \brief Generate 32-bit test signal on Pin 0 (KPB)
 */
static void ecns_kpb_test_signal_s32(struct ecns_comp_data *cd,
				     const struct ecns_buf *out,
				     uint32_t frames)
{
	int32_t *snk_ptr = out->ptr;
	uint32_t snk0_ch = out->channels;
	uint32_t remaining = frames;

	while (remaining > 0) {
		uint32_t f_snk = ecns_frames_without_wrap(out, snk_ptr);
		uint32_t n = MIN(remaining, f_snk);

		for (uint32_t i = 0; i < n; i++) {
			*snk_ptr++ = ((int32_t)cd->test_signal_val++) << 16;
			for (uint32_t c = 1; c < snk0_ch; c++)
				*snk_ptr++ = 0;
		}

		remaining -= n;
		snk_ptr = cir_buf_wrap(snk_ptr, out->start, out->end);
	}
}

/**
 * \brief Generate 16-bit test signal on Pin 0 (KPB)
 */
static void ecns_kpb_test_signal_s16(struct ecns_comp_data *cd,
				     const struct ecns_buf *out,
				     uint32_t frames)
{
	int16_t *snk_ptr = out->ptr;
	uint32_t snk0_ch = out->channels;
	uint32_t remaining = frames;

	while (remaining > 0) {
		uint32_t f_snk = ecns_frames_without_wrap(out, snk_ptr);
		uint32_t n = MIN(remaining, f_snk);

		for (uint32_t i = 0; i < n; i++) {
			*snk_ptr++ = (int16_t)cd->test_signal_val++;
			for (uint32_t c = 1; c < snk0_ch; c++)
				*snk_ptr++ = 0;
		}

		remaining -= n;
		snk_ptr = cir_buf_wrap(snk_ptr, out->start, out->end);
	}
}
#endif

static inline int32_t ecns_kpb_filter(struct ecns_comp_data *cd, int32_t x)
{
#if CONFIG_ECNS_KPB_HPF
	return iir_df1(&cd->hp_iir, x);
#else
	return x;
#endif
}

/*
 * Pin 0 kernels: average the selected input channels to mono, filter and
 * write to the KPB output. Output channels beyond the first are zeroed.
 */
#define ECNS_KPB_KERNEL(name, src_t, snk_t, mono_in, mono_out)				\
static void name(struct ecns_comp_data *cd, const struct ecns_buf *in,		\
		 const struct ecns_buf *out, uint32_t frames,				\
		 uint32_t avg_start, uint32_t avg_cnt)					\
{											\
	const src_t *src_ptr = in->ptr;							\
	snk_t *snk_ptr = out->ptr;							\
	uint32_t src0_ch = in->channels;						\
	uint32_t snk0_ch = out->channels;						\
	uint32_t remaining = frames;							\
											\
	while (remaining > 0) {								\
		uint32_t f_src = ecns_frames_without_wrap(in, src_ptr);			\
		uint32_t f_snk = ecns_frames_without_wrap(out, snk_ptr);		\
		uint32_t n = MIN(remaining, MIN(f_src, f_snk));				\
											\
		for (uint32_t i = 0; i < n; i++) {					\
			int32_t mono = mono_in;						\
											\
			mono = ecns_kpb_filter(cd, mono);				\
			*snk_ptr++ = mono_out;						\
			for (uint32_t c = 1; c < snk0_ch; c++)				\
				*snk_ptr++ = 0;						\
			src_ptr += src0_ch;						\
		}									\
											\
		remaining -= n;								\
		src_ptr = cir_buf_wrap(src_ptr, in->start, in->end);			\
		snk_ptr = cir_buf_wrap(snk_ptr, out->start, out->end);			\
	}										\
}

/* Process 32-bit DMIC input to 32-bit KPB mono output */
ECNS_KPB_KERNEL(ecns_kpb_s32_to_s32, int32_t, int32_t,
		ecns_mono_s32(src_ptr, avg_start, avg_cnt), mono)
/* Process 16-bit DMIC input to 32-bit KPB mono output */
ECNS_KPB_KERNEL(ecns_kpb_s16_to_s32, int16_t, int32_t,
		((int32_t)ecns_mono_s16(src_ptr, avg_start, avg_cnt)) << 16, mono)
/* Process 32-bit DMIC input to 16-bit KPB mono output */
ECNS_KPB_KERNEL(ecns_kpb_s32_to_s16, int32_t, int16_t,
		ecns_mono_s32(src_ptr, avg_start, avg_cnt),
		sat_int16(Q_SHIFT_RND(mono, 31, 15)))
/* Process 16-bit DMIC input to 16-bit KPB mono output */
ECNS_KPB_KERNEL(ecns_kpb_s16_to_s16, int16_t, int16_t,
		((int32_t)ecns_mono_s16(src_ptr, avg_start, avg_cnt)) << 16,
		sat_int16(Q_SHIFT_RND(mono, 31, 15)))

/**
 * \brief Process Pin 0: 16 kHz detection stream -> Mono clean to KPB
 *
 * Averages selected source channels into a mono stream, filters with the
 * built-in 100 Hz HPF (+30 dB gain, CONFIG_ECNS_KPB_HPF), and writes to the sink buffer (either
 * 16-bit or 32-bit container). Supports test signal generation when enabled.
 *
 * \param[in,out] cd     ECNS component data
 * \param[in]     in0    Input data from 16 kHz DMIC
 * \param[in]     frames Input frames available
 * \param[in,out] snk0   Output to KPB
 * \return Number of frames produced to snk0
 */
static uint32_t ecns_process_kpb(struct ecns_comp_data *cd, const struct ecns_buf *in0,
				 uint32_t frames, struct sof_sink *snk0)
{
	struct ecns_buf out0;
	size_t snk0_frame_bytes = sink_get_frame_bytes(snk0);
	size_t size;

	out0.channels = sink_get_channels(snk0);
	if (!out0.channels || !snk0_frame_bytes)
		return 0;
	out0.sample_bytes = snk0_frame_bytes / out0.channels;

	/* Drain all available input bounded by sink space; a one-period cap
	 * would let the source ring overflow under DMIC vs DSP clock drift.
	 */
	uint32_t frames0 = MIN(frames, sink_get_free_frames(snk0));

	if (!frames0)
		return 0;

	if (sink_get_buffer(snk0, frames0 * snk0_frame_bytes, &out0.ptr, &out0.start, &size))
		return 0;
	out0.end = (uint8_t *)out0.start + size;

	uint32_t avg_start, avg_cnt;

	ecns_mono_window(in0->channels, &avg_start, &avg_cnt);

#if CONFIG_ECNS_TEST_SIGNAL
	if (cd->test_signal_enabled) {
		if (out0.sample_bytes == sizeof(int32_t))
			ecns_kpb_test_signal_s32(cd, &out0, frames0);
		else
			ecns_kpb_test_signal_s16(cd, &out0, frames0);
	} else
#endif
	if (out0.sample_bytes == sizeof(int32_t)) {
		if (in0->sample_bytes == sizeof(int32_t))
			ecns_kpb_s32_to_s32(cd, in0, &out0, frames0, avg_start, avg_cnt);
		else
			ecns_kpb_s16_to_s32(cd, in0, &out0, frames0, avg_start, avg_cnt);
	} else {
		if (in0->sample_bytes == sizeof(int32_t))
			ecns_kpb_s32_to_s16(cd, in0, &out0, frames0, avg_start, avg_cnt);
		else
			ecns_kpb_s16_to_s16(cd, in0, &out0, frames0, avg_start, avg_cnt);
	}

	sink_commit_buffer(snk0, frames0 * snk0_frame_bytes);

	return frames0;
}

/*
 * Pin 1 kernels: filter each input channel to the host output, output
 * channels without an input channel are zeroed.
 */
#define ECNS_HOST_KERNEL(name, src_t, snk_t, filtered)					\
static void name(struct ecns_comp_data *cd, const struct ecns_buf *in,		\
		 const struct ecns_buf *out, uint32_t frames)				\
{											\
	const src_t *src_ptr = in->ptr;							\
	snk_t *snk_ptr = out->ptr;							\
	uint32_t in1_ch = in->channels;							\
	uint32_t snk1_ch = out->channels;						\
	uint32_t remaining = frames;							\
											\
	while (remaining > 0) {								\
		uint32_t f_src = ecns_frames_without_wrap(in, src_ptr);			\
		uint32_t f_snk = ecns_frames_without_wrap(out, snk_ptr);		\
		uint32_t n = MIN(remaining, MIN(f_src, f_snk));				\
											\
		for (uint32_t i = 0; i < n; i++) {					\
			for (uint32_t c = 0; c < snk1_ch; c++)				\
				snk_ptr[c] = (c < in1_ch) ? filtered : 0;		\
			snk_ptr += snk1_ch;						\
			src_ptr += in1_ch;						\
		}									\
											\
		remaining -= n;								\
		src_ptr = cir_buf_wrap(src_ptr, in->start, in->end);			\
		snk_ptr = cir_buf_wrap(snk_ptr, out->start, out->end);			\
	}										\
}

/* Filter 32-bit input to 32-bit host capture sink */
ECNS_HOST_KERNEL(ecns_host_s32_to_s32, int32_t, int32_t,
		 ecns_host_filter(cd, c, src_ptr[c]))
/* Filter 16-bit input to 32-bit host capture sink */
ECNS_HOST_KERNEL(ecns_host_s16_to_s32, int16_t, int32_t,
		 ecns_host_filter(cd, c, ((int32_t)src_ptr[c]) << 16))
/* Filter 32-bit input to 16-bit host capture sink */
ECNS_HOST_KERNEL(ecns_host_s32_to_s16, int32_t, int16_t,
		 sat_int16(Q_SHIFT_RND(ecns_host_filter(cd, c, src_ptr[c]), 31, 15)))
/* Filter 16-bit input to 16-bit host capture sink */
ECNS_HOST_KERNEL(ecns_host_s16_to_s16, int16_t, int16_t,
		 sat_int16(Q_SHIFT_RND(ecns_host_filter(cd, c, ((int32_t)src_ptr[c]) << 16),
				       31, 15)))

/**
 * \brief Process Pin 1: 48 kHz host capture stream
 *
 * Filters the audio from the input producer (48 kHz dmic01 or fallback
 * src0) with the built-in 40 Hz HPF (+20 dB gain) per channel and writes
 * it to the host sink buffer (supporting s16 or s32).
 *
 * \param[in,out] cd     ECNS component data
 * \param[in]     in1    Input data
 * \param[in]     frames Input frames available
 * \param[in,out] snk1   Output to host
 * \return Number of frames produced to snk1
 */
static uint32_t ecns_process_host(struct ecns_comp_data *cd, const struct ecns_buf *in1,
				  uint32_t frames, struct sof_sink *snk1)
{
	struct ecns_buf out1;
	size_t snk1_frame_bytes = sink_get_frame_bytes(snk1);
	size_t size;

	out1.channels = sink_get_channels(snk1);
	if (!out1.channels || !snk1_frame_bytes)
		return 0;
	out1.sample_bytes = snk1_frame_bytes / out1.channels;

	/* Drain all available input bounded by sink space; a one-period cap
	 * would let the source ring overflow under DMIC vs DSP clock drift.
	 */
	uint32_t frames1 = MIN(frames, sink_get_free_frames(snk1));

	if (!frames1)
		return 0;

	if (sink_get_buffer(snk1, frames1 * snk1_frame_bytes, &out1.ptr, &out1.start, &size))
		return 0;
	out1.end = (uint8_t *)out1.start + size;

	if (out1.sample_bytes == sizeof(int32_t)) {
		if (in1->sample_bytes == sizeof(int32_t))
			ecns_host_s32_to_s32(cd, in1, &out1, frames1);
		else
			ecns_host_s16_to_s32(cd, in1, &out1, frames1);
	} else {
		if (in1->sample_bytes == sizeof(int32_t))
			ecns_host_s32_to_s16(cd, in1, &out1, frames1);
		else
			ecns_host_s16_to_s16(cd, in1, &out1, frames1);
	}

	sink_commit_buffer(snk1, frames1 * snk1_frame_bytes);

	return frames1;
}

/* Get all whole frames available on a source, returns the frame count */
static uint32_t ecns_get_source(struct sof_source *source, struct ecns_buf *in)
{
	size_t frame_bytes = source_get_frame_bytes(source);
	const void *ptr, *start;
	size_t frames, size;

	in->channels = source_get_channels(source);
	if (!in->channels || !frame_bytes)
		return 0;
	in->sample_bytes = frame_bytes / in->channels;

	frames = source_get_data_frames_available(source);
	if (!frames || source_get_data(source, frames * frame_bytes, &ptr, &start, &size))
		return 0;

	in->ptr = (void *)ptr;
	in->start = (void *)start;
	in->end = (uint8_t *)in->start + size;

	return frames;
}

/* No room or nobody listening on an output */
static inline bool ecns_sink_idle(struct sof_sink *sink)
{
	return !sink || sink_get_state(sink) != COMP_STATE_ACTIVE || !sink_get_free_size(sink);
}

/*
 * Main 20ms processing function:
 * Consumes:
 *   - Pin 0 in: 16 kHz stream from dmic16k (2 or 4 channels, 16/32-bit)
 *   - Pin 1 in: 48 kHz stream from dmic01 (2 or 4 channels, 16/32-bit)
 * Produces:
 *   - Pin 0 out: 16 kHz mono clean stream to KPB (averaged mono per Kconfig, 320 frames per 20ms)
 *   - Pin 1 out: 48 kHz stereo/4ch clean stream to Host PCM 11 (40 Hz HPF +20 dB, 960 frames per 20ms)
 * Sampling rates remain identical between input and output on each pin.
 *
 * Inputs are consumed as far as they were processed. When nothing could be
 * produced because an output is inactive or full, all input is drained to
 * prevent upstream DAI FIFO overruns.
 */
static int ecns_process(struct processing_module *mod,
			struct sof_source **sources, int num_of_sources,
			struct sof_sink **sinks, int num_of_sinks)
{
	struct ecns_comp_data *cd = module_get_private_data(mod);
	struct sof_source *src0, *src1;
	struct sof_sink *snk0, *snk1;
	struct ecns_buf in0, in1;
	uint32_t avail0 = 0;
	uint32_t avail1 = 0;
	uint32_t frames0_produced = 0;
	uint32_t frames1_produced = 0;
	uint32_t consumed0, consumed1;

	if (!num_of_sources)
		return 0;

	/* Apply a capture boost change from the control in the processing context */
	if (cd->capture_boost != cd->capture_boost_req) {
		cd->capture_boost = cd->capture_boost_req;
		ecns_update_iir_coef(cd);
		comp_info(mod->dev, "capture boost %s",
			  cd->capture_boost ? "enabled" : "disabled");
	}

	ecns_get_pins(sources, num_of_sources, sinks, num_of_sinks, &src0, &src1, &snk0, &snk1);

	if (src0)
		avail0 = ecns_get_source(src0, &in0);
	if (src1)
		avail1 = ecns_get_source(src1, &in1);

	/* Process Pin 0: 16 kHz stream -> Mono clean to KPB */
	if (avail0 && snk0)
		frames0_produced = ecns_process_kpb(cd, &in0, avail0, snk0);

	/* Process Pin 1: 48 kHz stream -> Host capture */
	if (snk1) {
		if (src1) {
			if (avail1)
				frames1_produced = ecns_process_host(cd, &in1, avail1, snk1);
		} else if (avail0) {
			frames1_produced = ecns_process_host(cd, &in0, avail0, snk1);
		}
	}

	/* Consume from the inputs */
	if (src1) {
		consumed1 = frames1_produced;
		if (!consumed1 && ecns_sink_idle(snk1))
			consumed1 = avail1;
		source_release_data(src1, consumed1 * source_get_frame_bytes(src1));
	}

	if (src0) {
		consumed0 = frames0_produced;
		if (!src1 && frames1_produced > consumed0)
			consumed0 = frames1_produced;
		if (!consumed0 && ecns_sink_idle(snk0) && (src1 || ecns_sink_idle(snk1)))
			consumed0 = avail0;
		source_release_data(src0, consumed0 * source_get_frame_bytes(src0));
	}

	return 0;
}

static int ecns_set_config(struct processing_module *mod, uint32_t param_id,
			   enum module_cfg_fragment_position pos, uint32_t data_offset_size,
			   const uint8_t *fragment, size_t fragment_size, uint8_t *response,
			   size_t response_size)
{
	struct ecns_comp_data *cd = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;

	switch (param_id) {
	case SOF_IPC4_SWITCH_CONTROL_PARAM_ID: {
		const struct sof_ipc4_control_msg_payload *cp =
			(const struct sof_ipc4_control_msg_payload *)fragment;

		if (fragment_size < sizeof(*cp) + sizeof(cp->chanv[0]) || cp->num_elems < 1)
			return -EINVAL;

		switch (cp->id) {
		case ECNS_CTRL_INDEX_TEST_SIGNAL:
#if CONFIG_ECNS_TEST_SIGNAL
			cd->test_signal_enabled = cp->chanv[0].value ? 1 : 0;
			if (cd->test_signal_enabled)
				cd->test_signal_val = 0;

			comp_info(dev, "test signal %s (kpb mono output)",
				  cd->test_signal_enabled ? "enabled" : "disabled");
#else
			if (cp->chanv[0].value)
				comp_warn(dev, "test signal not supported (CONFIG_ECNS_TEST_SIGNAL=n)");
#endif
			return 0;
		case ECNS_CTRL_INDEX_CAPTURE_BOOST:
			cd->capture_boost_req = !!cp->chanv[0].value;
			return 0;
		default:
			comp_err(dev, "illegal switch control id %u", cp->id);
			return -EINVAL;
		}
	}
	default:
		return -EINVAL;
	}
}

static int ecns_get_config(struct processing_module *mod,
			   uint32_t config_id, uint32_t *data_offset_size,
			   uint8_t *fragment, size_t fragment_size)
{
	struct ecns_comp_data *cd = module_get_private_data(mod);

	switch (config_id) {
	case SOF_IPC4_SWITCH_CONTROL_PARAM_ID: {
		struct sof_ipc4_control_msg_payload *cp =
			(struct sof_ipc4_control_msg_payload *)fragment;
		uint16_t ctl_id = cp->id;
		uint32_t resp_size = sizeof(struct sof_ipc4_control_msg_payload) +
				     sizeof(struct sof_ipc4_ctrl_value_chan);

		if (resp_size > *data_offset_size) {
			comp_err(mod->dev, "wrong switch control response size %u vs %u",
				 resp_size, *data_offset_size);
			return -EINVAL;
		}

		*data_offset_size = resp_size;
		memset_s(cp, resp_size, 0, resp_size);
		cp->id = ctl_id;
		cp->num_elems = 1;
		cp->chanv[0].channel = 0;
		cp->chanv[0].value = ctl_id == ECNS_CTRL_INDEX_CAPTURE_BOOST ?
				     cd->capture_boost_req : cd->test_signal_enabled;
		return 0;
	}
	default:
		return -EINVAL;
	}
}

static const struct module_interface ecns_interface = {
	.init = ecns_init,
	.prepare = ecns_prepare,
	.process = ecns_process,
	.set_configuration = ecns_set_config,
	.get_configuration = ecns_get_config,
	.reset = ecns_reset,
	.free = ecns_free,
};

#if CONFIG_COMP_ECNS_MODULE
/* modular: llext dynamic link */

#include <module/module/api_ver.h>
#include <module/module/llext.h>
#include <rimage/sof/user/manifest.h>

static const struct sof_man_module_manifest mod_manifest __section(".module") __used =
	SOF_LLEXT_MODULE_MANIFEST("ECNS", &ecns_interface, 1, SOF_REG_UUID(ecns), 2);

SOF_LLEXT_BUILDINFO;

#else

DECLARE_TR_CTX(ecns_tr, SOF_UUID(ecns_uuid), LOG_LEVEL_INFO);
DECLARE_MODULE_ADAPTER(ecns_interface, ecns_uuid, ecns_tr);
SOF_MODULE_INIT(ecns, sys_comp_module_ecns_interface_init);

#endif
