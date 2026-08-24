// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2026 Intel Corporation. All rights reserved.

#include <sof/audio/module_adapter/module/generic.h>
#include <sof/audio/component.h>
#include <sof/audio/sink_api.h>
#include <sof/audio/source_api.h>
#include <sof/audio/data_blob.h>
#include <sof/audio/format.h>
#include <sof/audio/ipc-config.h>
#include <sof/audio/kpb.h>
#include <sof/ipc/msg.h>
#include <sof/lib/memory.h>
#include <sof/lib/uuid.h>
#include <sof/math/numbers.h>
#include <sof/trace/trace.h>
#include <ipc/control.h>
#include <ipc/stream.h>
#include <ipc/topology.h>
#include <module/module/llext.h>
#include <rtos/init.h>
#include <rtos/panic.h>
#include <rtos/string.h>
#include <sof/common.h>
#include <sof/list.h>
#include <sof/platform.h>
#include <sof/ut.h>
#include <user/trace.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>

#include <ipc4/base-config.h>
#include <ipc4/header.h>
#include <ipc4/module.h>
#include <ipc4/notification.h>

#include <sof/audio/mfcc/mfcc_comp.h>
#include "mww_model.h"

#if CONFIG_AMS
#include <sof/lib/ams.h>
#include <sof/lib/ams_msg.h>
#include <ipc4/ams_helpers.h>
#else
#include <sof/lib/notifier.h>
#endif

/* MFCC's non-compress output prepends a struct mfcc_data_header (24 bytes)
 * to each hop, followed by MWW_FEATURE_SIZE int32_t Q9.23 mel-log values.
 * This must match the frame size configured in the mww capture pipeline.
 */
#define MWW_HOP_BYTES (sizeof(struct mfcc_data_header) + MWW_FEATURE_SIZE * sizeof(int32_t))

/* Wake-word probability threshold above which KPB draining is triggered.
 * Starting point for hardware bring-up, tune once real detections are seen.
 */
#define MWW_DETECT_THRESHOLD 0.5f

SOF_DEFINE_REG_UUID(mww);
LOG_MODULE_REGISTER(mww, CONFIG_SOF_LOG_LEVEL);
#if CONFIG_COMP_MWW_MODULE
EXPORT_SYMBOL(mww_uuid);
EXPORT_SYMBOL(log_const_mww);
#endif

#if CONFIG_AMS
/* Key-phrase detected message, shared with src/samples/audio/detect_test.c
 * and consumed by kpb.c's AMS-consumer branch -- no kpb.c changes needed.
 */
static const ams_uuid_t ams_kpd_msg_uuid = AMS_KPD_MSG_UUID;
#endif

struct mww_comp_data {
	struct comp_data_blob_handler *model_handler;
	struct mww_classify mwc;
	struct kpb_client client_data;
	uint32_t drain_req;
#if CONFIG_AMS
	uint32_t kpd_uuid_id;
#else
	struct kpb_event_data event_data;
#endif
	bool initialized;
	int8_t feature_buf[MWW_FEATURE_ELEM_COUNT];
	int feature_slices_filled;
	uint32_t detections;
	uint32_t kpb_trigger_events;
};

#if CONFIG_AMS
static int mww_notify_kpb(struct processing_module *mod)
{
	struct mww_comp_data *cd = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;
	struct ams_message_payload ams_payload;

	comp_info(dev, "MWW keyword trigger -> notifying KPB to begin draining");

	cd->client_data.r_ptr = NULL;
	cd->client_data.sink = NULL;
	cd->client_data.id = 0; /**< TODO: acquire proper id from kpb */
	cd->client_data.drain_req = cd->drain_req;

	ams_helper_prepare_payload(dev, &ams_payload, cd->kpd_uuid_id,
				   (uint8_t *)&cd->client_data,
				   sizeof(struct kpb_client));

	return ams_send(&ams_payload);
}
#else
static int mww_notify_kpb(struct processing_module *mod)
{
	struct mww_comp_data *cd = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;

	comp_info(dev, "MWW keyword trigger -> notifying KPB to begin draining");

	cd->client_data.r_ptr = NULL;
	cd->client_data.sink = NULL;
	cd->client_data.id = 0;
	cd->client_data.drain_req = cd->drain_req;
	cd->event_data.event_id = KPB_EVENT_BEGIN_DRAINING;
	cd->event_data.client_data = &cd->client_data;

	notifier_event(dev, NOTIFIER_ID_KPB_CLIENT_EVT,
		       NOTIFIER_TARGET_CORE_ALL_MASK, &cd->event_data,
		       sizeof(cd->event_data));
	return 0;
}
#endif /* CONFIG_AMS */

/*
 * MFCC's mel-log output is Q9.23 fixed point, normalized by the MFCC
 * profile's mel_offset/mel_scale/top_db tuning to approximately the 0..1
 * range. Requantize that real-valued range against the model's own input
 * tensor scale/zero_point (as populated by MWW_InitOps()), instead of
 * assuming a fixed scale/zero_point.
 */
static inline int8_t mww_mel_q23_to_int8(int32_t mel_q23, float input_scale,
					  int input_zero_point)
{
	float norm = (float)mel_q23 / (float)(1 << 23); /* Q9.23 -> float, ~0..1 */
	float q = norm / input_scale + (float)input_zero_point;
	int32_t scaled = (int32_t)(q >= 0.0f ? q + 0.5f : q - 0.5f);

	if (scaled > 127)
		scaled = 127;
	else if (scaled < -128)
		scaled = -128;

	return (int8_t)scaled;
}

__cold static int mww_init(struct processing_module *mod)
{
	struct module_data *md = &mod->priv;
	struct comp_dev *dev = mod->dev;
	struct mww_comp_data *cd;

	comp_info(dev, "entry");

	cd = mod_zalloc(mod, sizeof(*cd));
	if (!cd)
		return -ENOMEM;

	md->private = cd;
	cd->drain_req = 1000; /* ms, initial default drain request */
#if CONFIG_AMS
	cd->kpd_uuid_id = AMS_INVALID_MSG_TYPE;
#endif

	return 0;
}

static int mww_prepare(struct processing_module *mod,
		       struct sof_source **sources, int num_of_sources,
		       struct sof_sink **sinks, int num_of_sinks)
{
	struct mww_comp_data *cd = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;
	int ret;

	comp_dbg(dev, "entry");

	if (cd->initialized)
		return 0;

	ret = MWW_SetModel(&cd->mwc, NULL);
	if (ret < 0) {
		comp_err(dev, "MWW_SetModel failed: %d (%s)", ret, cd->mwc.error);
		return ret;
	}

	ret = MWW_InitOps(&cd->mwc, NULL, 0);
	if (ret < 0) {
		comp_err(dev, "MWW_InitOps failed: %d (%s)", ret, cd->mwc.error);
		return ret;
	}

#if CONFIG_AMS
	/* Register KD as AMS producer */
	ret = ams_helper_register_producer(dev, &cd->kpd_uuid_id, ams_kpd_msg_uuid);
	if (ret)
		return ret;
#endif

	cd->initialized = true;
	cd->feature_slices_filled = 0;
	comp_info(dev, "MWW model initialized: input_scale=%d.%06d zero_point=%d",
		 (int)cd->mwc.input_scale,
		 (int)((cd->mwc.input_scale - (int)cd->mwc.input_scale) * 1000000),
		 cd->mwc.input_zero_point);

	return 0;
}

static int mww_process(struct processing_module *mod,
		       struct sof_source **sources, int num_of_sources,
		       struct sof_sink **sinks, int num_of_sinks)
{
	struct mww_comp_data *cd = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;
	size_t bytes_to_process;
	const void *data_ptr, *buf_start;
	size_t buf_size;
	int ret = 0;

	if (!cd->initialized) {
		size_t avail = source_get_data_available(sources[0]);

		if (avail > 0) {
			const void *dp, *bs;
			size_t bsz;

			if (source_get_data(sources[0], avail, &dp, &bs, &bsz) == 0)
				source_release_data(sources[0], avail);
		}
		return 0;
	}

	bytes_to_process = source_get_data_available(sources[0]);

	while (bytes_to_process >= MWW_HOP_BYTES) {
		const int32_t *mel;
		int8_t *slice;
		int i;

		ret = source_get_data(sources[0], MWW_HOP_BYTES,
				      &data_ptr, &buf_start, &buf_size);
		if (ret != 0 || !data_ptr)
			break;

		mel = (const int32_t *)((const uint8_t *)data_ptr +
					sizeof(struct mfcc_data_header));
		slice = &cd->feature_buf[cd->feature_slices_filled * MWW_FEATURE_SIZE];

		for (i = 0; i < MWW_FEATURE_SIZE; i++)
			slice[i] = mww_mel_q23_to_int8(mel[i], cd->mwc.input_scale,
							cd->mwc.input_zero_point);

		/* Copy source data to sink so downstream stages (e.g. drain
		 * path back to host) keep seeing the raw MFCC hops.
		 */
		if (num_of_sinks > 0 && sinks[0]) {
			void *snk_ptr, *snk_buf_start;
			size_t snk_buf_size;
			int sret = sink_get_buffer(sinks[0], MWW_HOP_BYTES,
						   &snk_ptr, &snk_buf_start, &snk_buf_size);
			if (sret == 0 && snk_ptr) {
				size_t size_to_wrap = (uint8_t *)snk_buf_start +
						       snk_buf_size - (uint8_t *)snk_ptr;
				if (MWW_HOP_BYTES <= size_to_wrap) {
					memcpy(snk_ptr, data_ptr, MWW_HOP_BYTES);
				} else {
					memcpy(snk_ptr, data_ptr, size_to_wrap);
					memcpy(snk_buf_start,
					       (const uint8_t *)data_ptr + size_to_wrap,
					       MWW_HOP_BYTES - size_to_wrap);
				}
				sink_commit_buffer(sinks[0], MWW_HOP_BYTES);
			}
		}

		source_release_data(sources[0], MWW_HOP_BYTES);
		bytes_to_process -= MWW_HOP_BYTES;
		cd->feature_slices_filled++;

		if (cd->feature_slices_filled >= MWW_FEATURE_SLICE_COUNT) {
			int8_t fmin = cd->feature_buf[0];
			int8_t fmax = cd->feature_buf[0];
			int j;

			for (j = 1; j < MWW_FEATURE_ELEM_COUNT; j++) {
				if (cd->feature_buf[j] < fmin)
					fmin = cd->feature_buf[j];
				if (cd->feature_buf[j] > fmax)
					fmax = cd->feature_buf[j];
			}
			comp_info(dev, "MWW DBG feature range: min=%d max=%d", fmin, fmax);

			cd->feature_slices_filled = 0;
			cd->mwc.audio_features = cd->feature_buf;
			cd->mwc.audio_data_size = MWW_FEATURE_ELEM_COUNT;

			ret = MWW_ProcessClassify(&cd->mwc);
			if (ret < 0) {
				comp_err(dev, "MWW_ProcessClassify failed: %d (%s)",
					 ret, cd->mwc.error);
				continue;
			}

			comp_info(dev, "MWW probability=%d raw=%u",
				 (int)(cd->mwc.probability * 100.0f), cd->mwc.raw_output);

			if (cd->mwc.probability >= MWW_DETECT_THRESHOLD) {
				cd->detections++;
				comp_info(dev, "MWW keyword detected: probability=%d pct (total=%u)",
					 (int)(cd->mwc.probability * 100.0f), cd->detections);
				cd->kpb_trigger_events++;
				mww_notify_kpb(mod);
			}
		}
	}

	return ret;
}

static int mww_reset(struct processing_module *mod)
{
	struct mww_comp_data *cd = module_get_private_data(mod);

	comp_dbg(mod->dev, "entry");
	cd->feature_slices_filled = 0;
	return 0;
}

__cold static int mww_free(struct processing_module *mod)
{
	struct mww_comp_data *cd = module_get_private_data(mod);

	assert_can_be_cold();

	comp_dbg(mod->dev, "entry");

#if CONFIG_AMS
	if (cd->kpd_uuid_id != AMS_INVALID_MSG_TYPE) {
		int ret = ams_helper_unregister_producer(mod->dev, cd->kpd_uuid_id);

		if (ret)
			comp_err(mod->dev, "unregister ams error %d", ret);
	}
#endif

	mod_free(mod, cd);
	return 0;
}

static int mww_set_config(struct processing_module *mod, uint32_t param_id,
	enum module_cfg_fragment_position pos, uint32_t data_offset_size,
	const uint8_t *fragment, size_t fragment_size, uint8_t *response,
	size_t response_size)
{
	return 0;
}

static const struct module_interface mww_interface = {
	.init = mww_init,
	.prepare = mww_prepare,
	.process = mww_process,
	.set_configuration = mww_set_config,
	.reset = mww_reset,
	.free = mww_free
};

/* This controls build of the module. If COMP_MODULE is selected in kconfig
 * this is build as dynamically loadable module.
 */
#if CONFIG_COMP_MWW_MODULE

#include <module/module/api_ver.h>
#include <rimage/sof/user/manifest.h>

static const struct sof_man_module_manifest mod_manifest __section(".module") __used =
	SOF_LLEXT_MODULE_MANIFEST("MWW", &mww_interface, 1,
				  SOF_REG_UUID(mww), 40);

SOF_LLEXT_BUILDINFO;

#else

/* Only used for the module adapter trace context, soon to be deprecated */
DECLARE_TR_CTX(mww_tr, SOF_UUID(mww_uuid), LOG_LEVEL_INFO);
DECLARE_MODULE_ADAPTER(mww_interface, mww_uuid, mww_tr);
SOF_MODULE_INIT(mww, sys_comp_module_mww_interface_init);

#endif
