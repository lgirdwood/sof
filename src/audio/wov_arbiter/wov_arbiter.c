// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2026 Intel Corporation
//
// WOV Arbiter -- routes the drain output of one-of-N KPB host sinks to the
// single host PCM copier, while silencing the idle inputs and coordinating
// pause/resume of sibling WOV detectors via the SOF notifier system.
//
// Topology connectivity (per KPB slot i):
//   KPB_i host_sink (output pin 1) --> wov_arbiter input pin i
//   wov_arbiter output pin 0       --> host copier
//
// Notifier events:
//   Subscribes to NOTIFIER_ID_WOV_DETECT  (detector -> arbiter on keyword)
//   Publishes   NOTIFIER_ID_WOV_CTRL      (arbiter -> detectors: pause/resume)

#include <sof/audio/component.h>
#include <sof/audio/module_adapter/module/generic.h>
#include <sof/audio/wov_arbiter.h>
#include <sof/common.h>
#include <sof/lib/notifier.h>
#include <sof/lib/uuid.h>
#include <rtos/init.h>
#include <sof/trace/trace.h>
#include <module/audio/sink_api.h>
#include <module/audio/source_api.h>
#include <ipc/stream.h>
#include <ipc/topology.h>
#include <ipc4/base-config.h>
#include <ipc4/header.h>
#include <ipc4/module.h>
#include <ipc4/notification.h>
#include <sof/ipc/msg.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

LOG_MODULE_REGISTER(wov_arbiter, CONFIG_SOF_LOG_LEVEL);

SOF_DEFINE_REG_UUID(wov_arbiter);

/* Private runtime data. */
struct wov_arb_data {
	/*
	 * Index of the currently active KPB slot (0..num_slots-1).
	 * WOV_ARB_NO_ACTIVE when no drain is in progress.
	 * Protected by the scheduler; no extra lock needed.
	 */
	uint8_t active_slot;
	/* Number of detector slots that may report a detection */
	uint8_t num_slots;
	uint16_t active_slot_ctl_id;
	uint32_t copy_count;
	uint32_t drain_frames_total;
#if CONFIG_IPC_MAJOR_4
	/* Notification messages, allocated on first use and reused */
	struct ipc_msg *ctl_msg;
	struct ipc_msg *detect_msg;
#endif
};

/*
 * Only one WOV graph (DMIC or headset) may be active at a time: detectors
 * share NOTIFIER_ID_WOV_DETECT and DSP memory only fits one detector set.
 * IPC handling is serialized, so no lock is needed.
 */
static unsigned int wov_arb_instances;

#if CONFIG_IPC_MAJOR_4
static void notify_control_change(struct processing_module *mod, struct wov_arb_data *cd,
				  uint16_t control_id, uint32_t val)
{
	const struct comp_dev *dev = mod->dev;
	struct sof_ipc4_notify_module_data *msg_module_data;
	struct sof_ipc4_control_msg_payload *msg_payload;
	struct ipc_msg *msg;
	uint32_t data_size = sizeof(struct sof_ipc4_notify_module_data) +
			     sizeof(struct sof_ipc4_control_msg_payload) +
			     sizeof(struct sof_ipc4_ctrl_value_chan);
	struct ipc4_voice_cmd_notification notif;

	if (!cd->ctl_msg) {
		memset_s(&notif, sizeof(notif), 0, sizeof(notif));
		notif.primary.r.notif_type = SOF_IPC4_MODULE_NOTIFICATION;
		notif.primary.r.type = SOF_IPC4_GLB_NOTIFICATION;
		notif.primary.r.rsp = SOF_IPC4_MESSAGE_DIR_MSG_REQUEST;
		notif.primary.r.msg_tgt = SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG;

		cd->ctl_msg = mod_ipc_msg_w_ext_init(mod, notif.primary.dat, 0, data_size);
		if (!cd->ctl_msg)
			return;
	}
	msg = cd->ctl_msg;

	msg_module_data = (struct sof_ipc4_notify_module_data *)msg->tx_data;
	msg_module_data->instance_id = IPC4_INST_ID(dev->ipc_config.id);
	msg_module_data->module_id = IPC4_MOD_ID(dev->ipc_config.id);
	msg_module_data->event_id = SOF_IPC4_NOTIFY_MODULE_EVENTID_ALSA_MAGIC_VAL |
				    SOF_IPC4_ENUM_CONTROL_PARAM_ID;
	msg_module_data->event_data_size = sizeof(struct sof_ipc4_control_msg_payload) +
					   sizeof(struct sof_ipc4_ctrl_value_chan);

	msg_payload = (struct sof_ipc4_control_msg_payload *)msg_module_data->event_data;
	msg_payload->id = control_id;
	msg_payload->num_elems = 1;
	msg_payload->chanv[0].channel = 0;
	msg_payload->chanv[0].value = val;

	ipc_msg_send(msg, NULL, true);
}

/* Wake the host: a blocking read() on the WOV capture PCM has no other way
 * to learn that a detection just pushed a burst of drained KPB data into the
 * host DMA buffer (the stream runs with SNDRV_PCM_INFO_NO_PERIOD_WAKEUP so
 * ALSA's normal period-elapsed IRQ path is not relied on here). This mirrors
 * detect_test.c's notify_host() so the kernel's sof_ipc4_rx_msg() can key off
 * SOF_IPC4_NOTIFY_PHRASE_DETECTED and call snd_sof_pcm_period_elapsed().
 * Sent once per detection (from arb_on_detect()), not repeated per process():
 * it is a one-shot simulated interrupt, not a substitute for the host's own
 * period-elapsed mechanism on streams that never requested no-period-wakeup.
 */
static void notify_host_detect(struct processing_module *mod, struct wov_arb_data *cd,
			       uint32_t slot_id)
{
	const struct comp_dev *dev = mod->dev;
	struct ipc4_voice_cmd_notification notif;

	memset_s(&notif, sizeof(notif), 0, sizeof(notif));
	notif.primary.r.word_id = slot_id;
	notif.primary.r.notif_type = SOF_IPC4_NOTIFY_PHRASE_DETECTED;
	notif.primary.r.type = SOF_IPC4_GLB_NOTIFICATION;
	notif.primary.r.rsp = SOF_IPC4_MESSAGE_DIR_MSG_REQUEST;
	notif.primary.r.msg_tgt = SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG;

	notif.extension.r.sv_score = (uint16_t)(dev_comp_id(dev) & 0xffff);
	notif.extension.r.rsvd1 = (uint32_t)(dev_comp_id(dev) >> 16);

	if (!cd->detect_msg) {
		cd->detect_msg = mod_ipc_msg_w_ext_init(mod, notif.primary.dat,
							notif.extension.dat, 0);
		if (!cd->detect_msg)
			return;
	}
	/* The header carries the slot, update it for each detection */
	cd->detect_msg->header = notif.primary.dat;

	ipc_msg_send(cd->detect_msg, NULL, true);
}
#endif

/* Leave the active slot and let all detectors listen again. */
static void wov_arb_resume_all(struct processing_module *mod, struct wov_arb_data *cd)
{
	struct wov_ctrl_notif ctrl = { .cmd = WOV_ARB_CMD_RESUME };

	cd->active_slot = WOV_ARB_NO_ACTIVE;
	notifier_event(mod->dev, NOTIFIER_ID_WOV_CTRL, NOTIFIER_TARGET_CORE_ALL_MASK,
		       &ctrl, sizeof(ctrl));
}

/* -------------------------------------------------------------------------
 * Notifier callbacks
 * ---------------------------------------------------------------------- */

/* Notifier callback: a WOV detector has fired.  Activates the winning slot
 * and broadcasts PAUSE to all detectors via NOTIFIER_ID_WOV_CTRL. */
static void arb_on_detect(void *arg, enum notify_id id, void *data)
{
	struct comp_dev *dev = arg;
	struct processing_module *mod = comp_mod(dev);
	struct wov_arb_data *cd = module_get_private_data(mod);
	const struct wov_detect_notif *det = data;

	if (det->slot_id >= cd->num_slots) {
		comp_err(dev, "wov_arb: bad slot_id %u num_slots=%u",
			 det->slot_id, cd->num_slots);
		return;
	}

	/* First-wins: ignore if another slot is already draining. */
	if (cd->active_slot != WOV_ARB_NO_ACTIVE) {
		comp_warn(dev, "wov_arb: slot %u detected but slot %u active, ignoring",
			  det->slot_id, cd->active_slot);
		return;
	}

	comp_info(dev, "wov_arb: activating slot %u", det->slot_id);
	cd->active_slot = det->slot_id;

#if CONFIG_IPC_MAJOR_4
	/* Notify host ALSA enum control: 1..N corresponds to Slot 1..N (0 is Listening) */
	notify_control_change(mod, cd, cd->active_slot_ctl_id, (uint32_t)cd->active_slot + 1);
	/* Wake a blocking read() on the host WOV capture PCM - see notify_host_detect(). */
	notify_host_detect(mod, cd, det->slot_id);
#endif

	/* Broadcast PAUSE to all detectors. The winning slot continues draining
	 * its KPB history buffer; all others suspend detection until RESUME. */
	struct wov_ctrl_notif ctrl = { .cmd = WOV_ARB_CMD_PAUSE, .slot_id = det->slot_id };
	notifier_event(dev, NOTIFIER_ID_WOV_CTRL, NOTIFIER_TARGET_CORE_ALL_MASK,
		       &ctrl, sizeof(ctrl));
}

/* -------------------------------------------------------------------------
 * Module lifecycle
 * ---------------------------------------------------------------------- */

static int wov_arb_init(struct processing_module *mod)
{
	struct comp_dev *dev = mod->dev;
	struct wov_arb_data *cd;

	if (wov_arb_instances) {
		comp_err(dev, "wov_arb_init: another WOV PCM is already active");
		return -EBUSY;
	}

	cd = mod_zalloc(mod, sizeof(*cd));
	if (!cd)
		return -ENOMEM;

	/* Detection notifications arrive via notifier bus across pipelines, so support up to MAX_SLOTS */
	cd->num_slots = WOV_ARB_MAX_SLOTS;
	cd->active_slot_ctl_id = 1;

	/* Start with no active slot; first WOV_DETECT notifier will activate one. */
	cd->active_slot = WOV_ARB_NO_ACTIVE;

	module_set_private_data(mod, cd);
	/* Pin 0 from KPB and the detector outputs */
	mod->max_sources = CONFIG_MODULE_MAX_CONNECTIONS;
	wov_arb_instances++;

	comp_info(dev, "wov_arb_init: num_slots=%u, period=%u, frames=%u",
		  cd->num_slots, dev->period, dev->frames);

	return 0;
}

static int wov_arb_free(struct processing_module *mod)
{
	struct wov_arb_data *cd = module_get_private_data(mod);

	comp_info(mod->dev, "wov_arb_free");

	if (cd->active_slot != WOV_ARB_NO_ACTIVE)
		wov_arb_resume_all(mod, cd);

	notifier_unregister(mod->dev, NULL, NOTIFIER_ID_WOV_DETECT);

#if CONFIG_IPC_MAJOR_4
	mod_ipc_msg_free(mod, cd->ctl_msg);
	mod_ipc_msg_free(mod, cd->detect_msg);
#endif
	mod_free(mod, cd);

	if (wov_arb_instances)
		wov_arb_instances--;

	return 0;
}

static int wov_arb_prepare(struct processing_module *mod,
			   struct sof_source **sources, int num_of_sources,
			   struct sof_sink **sinks, int num_of_sinks)
{
	struct wov_arb_data *cd = module_get_private_data(mod);
	const struct ipc4_audio_format *fmt = &mod->priv.cfg.base_cfg.audio_fmt;
	struct comp_dev *dev = mod->dev;
	int i;

	comp_info(dev, "wov_arb_prepare: period=%u, frames=%u", dev->period, dev->frames);

	cd->active_slot = WOV_ARB_NO_ACTIVE;
	cd->copy_count = 0;
	cd->drain_frames_total = 0;

#if CONFIG_IPC_MAJOR_4
	notify_control_change(mod, cd, cd->active_slot_ctl_id, 0);
#endif

	if (num_of_sinks)
		ipc4_update_sink_format(sinks[0], fmt);

	/* Pin 0 is the drained audio from KPB */
	for (i = 0; i < num_of_sources; i++)
		if (IPC4_SINK_QUEUE_ID(source_get_id(sources[i])) == 0)
			ipc4_update_source_format(sources[i], fmt);

	/* Subscribe to keyword-detected events from any WOV detector. */
	notifier_unregister(dev, NULL, NOTIFIER_ID_WOV_DETECT);
	notifier_register(dev, NULL, NOTIFIER_ID_WOV_DETECT, arb_on_detect, 0);

	/* Broadcast RESUME so all WOV detector slots start unpaused. */
	wov_arb_resume_all(mod, cd);

	return 0;
}

static int wov_arb_reset(struct processing_module *mod)
{
	struct wov_arb_data *cd = module_get_private_data(mod);

	comp_info(mod->dev, "wov_arb_reset");

	if (cd->active_slot != WOV_ARB_NO_ACTIVE)
		wov_arb_resume_all(mod, cd);

	cd->drain_frames_total = 0;

	notifier_unregister(mod->dev, NULL, NOTIFIER_ID_WOV_DETECT);

	return 0;
}

static int wov_arb_trigger(struct processing_module *mod, int cmd)
{
	struct wov_arb_data *cd = module_get_private_data(mod);
	int ret;

	comp_info(mod->dev, "wov_arb_trigger cmd %d", cmd);

	ret = module_adapter_set_state(mod, mod->dev, cmd);
	if (ret)
		return ret;

	/*
	 * Stream stopped or paused: deactivate the active slot and resume
	 * all detectors so they return to listening mode.
	 */
	if (cmd == COMP_TRIGGER_STOP || cmd == COMP_TRIGGER_PAUSE) {
		cd->drain_frames_total = 0;
		if (cd->active_slot != WOV_ARB_NO_ACTIVE) {
			comp_info(mod->dev, "wov_arb: stream stopped, resuming all slots");
#if CONFIG_IPC_MAJOR_4
			notify_control_change(mod, cd, cd->active_slot_ctl_id, 0);
#endif
			wov_arb_resume_all(mod, cd);
		}
	}

	return 0;
}

/* -------------------------------------------------------------------------
 * IPC4 large-config: allow host to force-select a slot (debug/test use).
 * ---------------------------------------------------------------------- */

static int wov_arb_set_config(struct processing_module *mod, uint32_t param_id,
			      enum module_cfg_fragment_position pos, uint32_t data_offset_size,
			      const uint8_t *fragment, size_t fragment_size, uint8_t *response,
			      size_t response_size)
{
	struct wov_arb_data *cd = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;

	switch (param_id) {
	case SOF_IPC4_ENUM_CONTROL_PARAM_ID: {
		const struct sof_ipc4_control_msg_payload *cp =
			(const struct sof_ipc4_control_msg_payload *)fragment;

		if (fragment_size < sizeof(*cp))
			return -EINVAL;

		cd->active_slot_ctl_id = cp->id;
		if (cp->num_elems > 0) {
			uint32_t val = cp->chanv[0].value;

			if (val == 0) {
				comp_info(dev, "wov_arb: reset active_slot to listening (0)");
				wov_arb_resume_all(mod, cd);
			} else {
				uint8_t selected_slot = (uint8_t)(val - 1);

				comp_info(dev, "wov_arb: filter listening to slot=%u via kcontrol",
					  selected_slot);
				/*
				 * Do not force cd->active_slot: the arbiter must remain
				 * in listening mode (WOV_ARB_NO_ACTIVE) so the selected
				 * detector can still trigger when the keyword is spoken,
				 * and so the host PCM stays idle until actual detection.
				 *
				 * Broadcast RESUME so the target slot unpauses even if
				 * it was previously paused, then broadcast PAUSE with
				 * slot_id = selected_slot so every OTHER slot suspends
				 * inference and yields CPU time to the selected detector.
				 */
				wov_arb_resume_all(mod, cd);
				struct wov_ctrl_notif p = {
					.cmd = WOV_ARB_CMD_PAUSE,
					.slot_id = selected_slot,
				};
				notifier_event(dev, NOTIFIER_ID_WOV_CTRL,
					       NOTIFIER_TARGET_CORE_ALL_MASK,
					       &p, sizeof(p));
			}
		}
		return 0;
	}
	case SOF_IPC4_BYTES_CONTROL_PARAM_ID:
	case IPC4_WOV_ARB_GET_ACTIVE_SLOT:
		return 0;
	case IPC4_WOV_ARB_SET_ACTIVE_SLOT:
		if (!fragment_size)
			return -EINVAL;
		cd->active_slot = *fragment;
		comp_info(dev, "wov_arb: force active_slot=%u", cd->active_slot);
		return 0;
	default:
		return -EINVAL;
	}
}

/* -------------------------------------------------------------------------
 * IPC4 large-config (get): expose active slot to userspace as a volatile
 * RO enum kcontrol; the host reads this via GET_MODULE_LARGE_CONFIG.
 * ---------------------------------------------------------------------- */

static int wov_arb_get_config(struct processing_module *mod,
			      uint32_t config_id, uint32_t *data_offset_size,
			      uint8_t *fragment, size_t fragment_size)
{
	struct wov_arb_data *cd = module_get_private_data(mod);

	switch (config_id) {
	case SOF_IPC4_ENUM_CONTROL_PARAM_ID: {
		struct sof_ipc4_control_msg_payload *cp =
			(struct sof_ipc4_control_msg_payload *)fragment;
		uint16_t ctl_id = cp->id;
		uint32_t resp_size = sizeof(struct sof_ipc4_control_msg_payload) +
				     sizeof(struct sof_ipc4_ctrl_value_chan);

		if (resp_size > *data_offset_size) {
			comp_err(mod->dev, "wrong enum control response size %u vs %u",
				 resp_size, *data_offset_size);
			return -EINVAL;
		}

		cd->active_slot_ctl_id = ctl_id;
		*data_offset_size = resp_size;
		memset_s(cp, resp_size, 0, resp_size);
		cp->id = ctl_id;
		cp->num_elems = 1;
		cp->chanv[0].channel = 0;
		/* 0 for Listening, 1..N for triggered slots */
		if (cd->active_slot == WOV_ARB_NO_ACTIVE)
			cp->chanv[0].value = 0;
		else
			cp->chanv[0].value = (uint32_t)cd->active_slot + 1;
		return 0;
	}
	case IPC4_WOV_ARB_GET_ACTIVE_SLOT:
		*(uint32_t *)fragment = (uint32_t)cd->active_slot;
		*data_offset_size = sizeof(uint32_t);
		return 0;
	default:
		return -EINVAL;
	}
}

/* -------------------------------------------------------------------------
 * process() -- main audio processing
 *
 * While a slot is active: forward the drained KPB audio on input pin 0 to
 * the output, converting channels and sample size as needed.
 * All other input pins, and pin 0 while listening: consume and discard to
 * prevent buffer stalls.
 * ---------------------------------------------------------------------- */

/* Read one sample from a circular buffer as S32, S16 is MSB aligned */
static inline int32_t wov_arb_read_sample(const uint8_t **p, const uint8_t *start,
					  const uint8_t *end, size_t sample_bytes)
{
	int32_t s = sample_bytes == sizeof(int32_t) ? *(const int32_t *)*p :
		    (int32_t)*(const int16_t *)*p << 16;

	*p += sample_bytes;
	if (*p >= end)
		*p = start;

	return s;
}

/* Write one S32 sample to a circular buffer, S16 takes the MSBs */
static inline void wov_arb_write_sample(uint8_t **p, uint8_t *start, uint8_t *end,
					size_t sample_bytes, int32_t s)
{
	if (sample_bytes == sizeof(int32_t))
		*(int32_t *)*p = s;
	else
		*(int16_t *)*p = (int16_t)(s >> 16);

	*p += sample_bytes;
	if (*p >= end)
		*p = start;
}

/* Copy frames from source to sink. Mono input is copied to all output
 * channels, otherwise channels map 1:1 and extra input channels are dropped.
 */
static int wov_arb_copy_frames(struct sof_source *source, struct sof_sink *sink,
			       uint32_t frames)
{
	size_t src_frame_bytes = source_get_frame_bytes(source);
	size_t dst_frame_bytes = sink_get_frame_bytes(sink);
	unsigned int src_ch = source_get_channels(source);
	unsigned int dst_ch = sink_get_channels(sink);
	size_t src_sb = src_frame_bytes / src_ch;
	size_t dst_sb = dst_frame_bytes / dst_ch;
	int32_t frame[PLATFORM_MAX_CHANNELS];
	const uint8_t *src_start, *src_end, *in;
	uint8_t *dst_start, *dst_end, *out;
	const void *src_ptr, *src_buf;
	void *dst_ptr, *dst_buf;
	size_t src_size, dst_size;
	unsigned int ch;
	uint32_t i;
	int ret;

	if (src_ch > PLATFORM_MAX_CHANNELS)
		return -EINVAL;

	ret = source_get_data(source, frames * src_frame_bytes, &src_ptr, &src_buf, &src_size);
	if (ret)
		return ret;

	ret = sink_get_buffer(sink, frames * dst_frame_bytes, &dst_ptr, &dst_buf, &dst_size);
	if (ret) {
		source_release_data(source, 0);
		return ret;
	}

	in = src_ptr;
	src_start = src_buf;
	src_end = src_start + src_size;
	out = dst_ptr;
	dst_start = dst_buf;
	dst_end = dst_start + dst_size;

	for (i = 0; i < frames; i++) {
		for (ch = 0; ch < src_ch; ch++)
			frame[ch] = wov_arb_read_sample(&in, src_start, src_end, src_sb);
		for (ch = 0; ch < dst_ch; ch++)
			wov_arb_write_sample(&out, dst_start, dst_end, dst_sb,
					     frame[src_ch == 1 ? 0 : MIN(ch, src_ch - 1)]);
	}

	source_release_data(source, frames * src_frame_bytes);
	sink_commit_buffer(sink, frames * dst_frame_bytes);

	return 0;
}

/* Consume and discard everything available on a source */
static void wov_arb_drop(struct sof_source *source)
{
	size_t avail = source_get_data_available(source);
	const void *ptr, *buf;
	size_t size;

	if (avail && !source_get_data(source, avail, &ptr, &buf, &size))
		source_release_data(source, avail);
}

static int wov_arb_process(struct processing_module *mod,
			   struct sof_source **sources, int num_of_sources,
			   struct sof_sink **sinks, int num_of_sinks)
{
	struct wov_arb_data *cd = module_get_private_data(mod);
	struct sof_sink *sink = num_of_sinks ? sinks[0] : NULL;
	uint32_t copied_frames = 0;
	uint32_t frames;
	int ret;
	int i;

	/*
	 * Do NOT forward anything while listening: the host PCM must stay
	 * silent/idle (no data, no host-copier IRQs) until a real WOV
	 * detection (or the fake-wake test path) sets cd->active_slot via
	 * arb_on_detect(). That is what lets the host block in read() and the
	 * platform actually reach D0i3/suspend instead of being kept awake by a
	 * continuous keep-alive stream.
	 */
	for (i = 0; i < num_of_sources; i++) {
		struct sof_source *source = sources[i];

		if (sink && cd->active_slot != WOV_ARB_NO_ACTIVE &&
		    IPC4_SINK_QUEUE_ID(source_get_id(source)) == 0) {
			frames = MIN(source_get_data_frames_available(source),
				     sink_get_free_frames(sink));
			if (frames) {
				ret = wov_arb_copy_frames(source, sink, frames);
				if (ret)
					return ret;
				copied_frames = frames;
				cd->drain_frames_total += frames;
			}
		} else {
			wov_arb_drop(source);
		}
	}

	cd->copy_count++;
	if ((cd->copy_count % 100) == 1)
		comp_dbg(mod->dev, "wov_arb_process #%u: active=%u, num_src=%d, copied=%u",
			 cd->copy_count, cd->active_slot, num_of_sources, copied_frames);

	return 0;
}

static const struct module_interface wov_arbiter_interface = {
	.init = wov_arb_init,
	.prepare = wov_arb_prepare,
	.process = wov_arb_process,
	.set_configuration = wov_arb_set_config,
	.get_configuration = wov_arb_get_config,
	.trigger = wov_arb_trigger,
	.reset = wov_arb_reset,
	.free = wov_arb_free,
};

#if CONFIG_COMP_WOV_ARBITER_MODULE
/* modular: llext dynamic link */

#include <module/module/api_ver.h>
#include <module/module/llext.h>
#include <rimage/sof/user/manifest.h>

static const struct sof_man_module_manifest mod_manifest __section(".module") __used =
	SOF_LLEXT_MODULE_MANIFEST("WOVARB", &wov_arbiter_interface, 1,
				  SOF_REG_UUID(wov_arbiter), 3);

SOF_LLEXT_BUILDINFO;

#else

DECLARE_TR_CTX(wov_arbiter_tr, SOF_UUID(wov_arbiter_uuid), LOG_LEVEL_INFO);
DECLARE_MODULE_ADAPTER(wov_arbiter_interface, wov_arbiter_uuid, wov_arbiter_tr);
SOF_MODULE_INIT(wov_arbiter, sys_comp_module_wov_arbiter_interface_init);

#endif
