// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2019 Intel Corporation. All rights reserved.
//
// Author: Marcin Rajwa <marcin.rajwa@linux.intel.com>

/*
 * Key phrase buffer (KPB), module adapter implementation for IPC4.
 *
 * KPB has one input and two outputs with the same format as the input:
 *   output pin 0 - real time feed to the key phrase detectors (sel_sink)
 *   output pin 1 - drain path towards the host (host_sink)
 *
 * In the RUN state the input is copied to sel_sink and recorded in a history
 * buffer. On a KPB_EVENT_BEGIN_DRAINING notification the requested amount of
 * history is drained into host_sink by an EDF task, while new input keeps
 * being recorded. When the history and the data recorded while draining have
 * been drained, KPB copies the input straight to host_sink (HOST_COPY) until
 * the WoV arbiter sends WOV_ARB_CMD_RESUME.
 *
 * The legacy comp_driver implementation, still used with IPC3, is in
 * kpb_ipc3.c.
 */

#include <sof/audio/component.h>
#include <sof/audio/module_adapter/module/generic.h>
#include <sof/audio/kpb.h>
#include <sof/audio/wov_arbiter.h>
#include <sof/common.h>
#include <sof/lib/pm_runtime.h>
#include <sof/lib/uuid.h>
#include <sof/platform.h>
#include <sof/schedule/edf_schedule.h>
#include <sof/schedule/schedule.h>
#include <sof/trace/trace.h>
#include <module/audio/sink_api.h>
#include <module/audio/source_api.h>
#include <rtos/alloc.h>
#include <rtos/cache.h>
#include <rtos/init.h>
#include <rtos/string.h>
#include <rtos/task.h>
#include <rtos/timer.h>
#include <ipc4/base-config.h>
#include <ipc4/kpb.h>
#include <ipc4/module.h>
#include <user/kpb.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#if CONFIG_AMS
#include <sof/lib/ams.h>
#include <sof/lib/ams_msg.h>
#include <ipc4/ams_helpers.h>
#else
#include <sof/lib/notifier.h>
#endif

#if CONFIG_COMP_KPB_MODULE && CONFIG_AMS
#error "KPB cannot be built as a loadable module with CONFIG_AMS"
#endif

LOG_MODULE_REGISTER(kpb, CONFIG_SOF_LOG_LEVEL);

SOF_DEFINE_REG_UUID(kpb4);
SOF_DEFINE_REG_UUID(kpb_task);

/* Draining task data */
struct kpb_drain_data {
	struct processing_module *mod;
	struct sof_sink *sink;
	struct history_buffer *hb;
	size_t drain_req;
	size_t drained;
	size_t sample_width;
	size_t buffered_while_draining;
	uint64_t draining_time_start;
	size_t drain_interval;
	uint64_t period_copy_start;
	size_t pb_limit; /**< Period bytes limit */
	size_t period_bytes;
	uint64_t next_copy_time;
	bool sync_mode_on;
	size_t task_iteration;
	uint64_t prev_adjustment_time;
	size_t prev_adjustment_drained;
};

/* KPB private data, runtime data */
struct kpb_comp_data {
	enum kpb_state state; /**< current state of KPB component */
	uint32_t state_log; /**< keeps record of KPB recent states */
	struct k_mutex lock;
	struct sof_kpb_config config;   /**< component configuration data */
	struct history_data hd; /** data related to history buffer */
	struct task draining_task;
	struct kpb_drain_data draining_task_data;
	struct sof_sink *sel_sink; /**< real time sink, output pin 0 */
	struct sof_sink *host_sink; /**< draining sink, output pin 1 */
	size_t frame_bytes; /**< bytes per frame, input and outputs */
	size_t host_buffer_size; /**< size of host buffer */
	size_t host_period_size; /**< size of history period */
	bool sync_draining_mode; /**< should we synchronize draining with host? */
	bool pm_lock_taken; /**< KPB holds the D0i3 prevent lock */
	uint32_t buff_time_ms; /**< history depth in ms; 0 = CONFIG_KPB_MAX_BUFF_TIME */
#if CONFIG_AMS
	uint32_t kpd_uuid_id;
#endif
};

static void kpb_init_draining(struct processing_module *mod, struct kpb_client *cli);

static uint64_t kpb_task_deadline(void *data)
{
	struct kpb_drain_data *dd = data;
	uint64_t now;

	if (dd->next_copy_time == 0)
		return 0;	/* run immediately */

	now = sof_cycle_get_64();
	return dd->next_copy_time > now ?
		k_uptime_ticks() + k_cyc_to_ticks_near64(dd->next_copy_time - now) : 0;
}

static void kpb_lock(struct kpb_comp_data *kpb)
{
	k_mutex_lock(&kpb->lock, K_FOREVER);
}

static void kpb_unlock(struct kpb_comp_data *kpb)
{
	k_mutex_unlock(&kpb->lock);
}

/**
 * \brief Change KPB state and log this change internally.
 * \param[in] mod - KPB module.
 * \param[in] kpb - KPB component data pointer.
 * \param[in] state - new KPB state.
 */
static inline void kpb_change_state(struct processing_module *mod, struct kpb_comp_data *kpb,
				    enum kpb_state state)
{
	comp_dbg(mod->dev, "change state from %d to %d", kpb->state, state);
	kpb->state = state;
	kpb->state_log = (kpb->state_log << 4) | state;
}

/* Return per-instance history depth (ms); fall back to Kconfig if not set via topology. */
static inline uint32_t kpb_get_buff_time_ms(const struct kpb_comp_data *kpb)
{
	return kpb->buff_time_ms ? kpb->buff_time_ms : CONFIG_KPB_MAX_BUFF_TIME;
}

/* Release the D0i3 prevent lock taken when draining started */
static void kpb_pm_release(struct kpb_comp_data *kpb)
{
	if (kpb->pm_lock_taken) {
		pm_runtime_enable(PM_RUNTIME_DSP, PLATFORM_PRIMARY_CORE_ID);
		kpb->pm_lock_taken = false;
	}
}

static inline bool kpb_is_sample_width_supported(uint32_t sampling_width)
{
	switch (sampling_width) {
#if CONFIG_FORMAT_S16LE
	case 16:
#endif
#if CONFIG_FORMAT_S24LE
	case 24:
#endif
#if CONFIG_FORMAT_S32LE
	case 32:
#endif
		return true;
	default:
		return false;
	}
}

/**
 * \brief Copy bytes between two circular buffers.
 *
 * A linear buffer is a circular buffer of exactly the copied size, so this
 * also copies from or to linear memory.
 */
static void kpb_circ_copy(uint8_t **wptr, uint8_t *wstart, uint8_t *wend,
			  const uint8_t **rptr, const uint8_t *rstart, const uint8_t *rend,
			  size_t bytes)
{
	size_t n;

	while (bytes) {
		n = MIN(bytes, MIN((size_t)(rend - *rptr), (size_t)(wend - *wptr)));
		memcpy_s(*wptr, n, *rptr, n);
		bytes -= n;
		*rptr += n;
		if (*rptr >= rend)
			*rptr = rstart;
		*wptr += n;
		if (*wptr >= wend)
			*wptr = wstart;
	}
}

/* Copy bytes from a source buffer region to a sink buffer region */
static void kpb_copy_to_sink(void *dst_ptr, void *dst_start, size_t dst_size,
			     const void *src_ptr, const void *src_start, size_t src_size,
			     size_t bytes)
{
	uint8_t *w = dst_ptr;
	const uint8_t *r = src_ptr;

	kpb_circ_copy(&w, dst_start, (uint8_t *)dst_start + dst_size,
		      &r, src_start, (const uint8_t *)src_start + src_size, bytes);
}

/**
 * \brief Write linear history data into a sink.
 * \param[in] sink - sink to write to.
 * \param[in] src - linear source data.
 * \param[in] bytes - number of bytes, must not exceed the sink free space.
 */
static int kpb_drain_to_sink(struct sof_sink *sink, const void *src, size_t bytes)
{
	void *dst_ptr, *dst_start;
	size_t dst_size;
	int ret;

	ret = sink_get_buffer(sink, bytes, &dst_ptr, &dst_start, &dst_size);
	if (ret)
		return ret;

	kpb_copy_to_sink(dst_ptr, dst_start, dst_size, src, src, bytes, bytes);

	return sink_commit_buffer(sink, bytes);
}

/**
 * \brief Allocate history buffer.
 * \param[in] mod - KPB module.
 * \param[in] kpb - KPB component data pointer.
 * \param[in] hb_size_req - requested size in bytes.
 *
 * \return: allocated size in bytes.
 */
static size_t kpb_allocate_history_buffer(struct processing_module *mod,
					  struct kpb_comp_data *kpb, size_t hb_size_req)
{
	struct history_buffer *hb;
	struct history_buffer *new_hb = NULL;
	/*! Total allocation size */
	size_t hb_size = hb_size_req;
	/*! Current allocation size */
	size_t ca_size = hb_size;
	/*! Memory caps priorites for history buffer */
	int hb_mcp[KPB_NO_OF_MEM_POOLS] = {SOF_MEM_FLAG_USER | SOF_MEM_FLAG_LOW_POWER,
					   SOF_MEM_FLAG_USER};
	void *new_mem_block = NULL;
	size_t temp_ca_size;
	int i = 0;
	size_t allocated_size = 0;

	comp_info(mod->dev, "entry");

	/* Initialize history buffer */
	kpb->hd.c_hb = rzalloc(SOF_MEM_FLAG_USER, sizeof(struct history_buffer));
	if (!kpb->hd.c_hb)
		return 0;
	kpb->hd.c_hb->next = kpb->hd.c_hb;
	kpb->hd.c_hb->prev = kpb->hd.c_hb;
	hb = kpb->hd.c_hb;

	/* Allocate history buffer/s. If there is no single memory block big
	 * enough, allocate a couple of smaller blocks which linked together
	 * form the history buffer.
	 */
	while (hb_size > 0 && i < ARRAY_SIZE(hb_mcp)) {
		/* Try to allocate ca_size (current allocation size). At first
		 * attempt it will be equal to hb_size (history buffer size).
		 */
		new_mem_block = rballoc(hb_mcp[i], ca_size);

		if (new_mem_block) {
			/* We managed to allocate a block of ca_size.
			 * Now we initialize it.
			 */
			comp_info(mod->dev, "kpb new memory block: %zu", ca_size);
			allocated_size += ca_size;
			hb->start_addr = new_mem_block;
			hb->end_addr = (char *)new_mem_block + ca_size;
			hb->w_ptr = new_mem_block;
			hb->r_ptr = new_mem_block;
			hb->state = KPB_BUFFER_FREE;
			hb_size -= ca_size;
			hb->next = kpb->hd.c_hb;
			/* Do we need another buffer? */
			if (hb_size > 0 && i + 1 < ARRAY_SIZE(hb_mcp)) {
				/* Yes, we still need at least one more buffer.
				 * Let's first create new container for it.
				 */
				new_hb = rzalloc(SOF_MEM_FLAG_USER, sizeof(struct history_buffer));
				if (!new_hb)
					return 0;
				hb->next = new_hb;
				new_hb->next = kpb->hd.c_hb;
				new_hb->state = KPB_BUFFER_OFF;
				new_hb->prev = hb;
				hb = new_hb;
				kpb->hd.c_hb->prev = new_hb;
				ca_size = hb_size;
				i++;
			}
		} else {
			/* We've failed to allocate ca_size of that hb_mcp
			 * let's try again with some smaller size.
			 * NOTE! If we decrement by some small value,
			 * the allocation will take significant time.
			 * However, bigger values like
			 * HEAP_HP_BUFFER_BLOCK_SIZE will result in lower
			 * accuracy of allocation.
			 */
			temp_ca_size = ca_size - KPB_ALLOCATION_STEP;
			ca_size = (ca_size < temp_ca_size) ? 0 : temp_ca_size;
			if (ca_size == 0) {
				ca_size = hb_size;
				i++;
			}
			continue;
		}
	}

	comp_info(mod->dev, "allocated %zu bytes for history buffer", allocated_size);

	return allocated_size;
}

/**
 * \brief Reclaim memory of a history buffer.
 * \param[in] buff - pointer to current history buffer.
 */
static void kpb_free_history_buffer(struct history_buffer *buff)
{
	struct history_buffer *_buff;
	struct history_buffer *first_buff = buff;

	if (!buff)
		return;

	/* Free history buffer/s */
	do {
		/* First reclaim HB internal memory, then HB itself */
		if (buff->start_addr)
			rfree(buff->start_addr);

		_buff = buff->next;
		rfree(buff);
		buff = _buff;
	} while (buff && buff != first_buff);
}

/**
 * \brief Reset history buffer.
 * \param[in] buff - pointer to current history buffer.
 */
static void kpb_reset_history_buffer(struct history_buffer *buff)
{
	struct history_buffer *first_buff = buff;

	if (!buff)
		return;

	do {
		/* Reset to start so no stale data from a prior drain session is
		 * re-played on the next KPB activation.
		 */
		buff->w_ptr = buff->start_addr;
		buff->r_ptr = buff->start_addr;
		buff->state = KPB_BUFFER_FREE;

		buff = buff->next;
	} while (buff != first_buff);
}

/**
 * \brief Buffer real time data stream in the internal history buffer.
 *
 * \param[in] mod - KPB module.
 * \param[in] src_ptr - source data, as returned by source_get_data().
 * \param[in] src_start - start of the source circular buffer.
 * \param[in] src_size - size of the source circular buffer.
 * \param[in] size - number of bytes to buffer.
 */
static int kpb_buffer_data(struct processing_module *mod, const void *src_ptr,
			   const void *src_start, size_t src_size, size_t size)
{
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	struct history_buffer *buff = kpb->hd.c_hb;
	const uint8_t *r = src_ptr;
	const uint8_t *rstart = src_start;
	const uint8_t *rend = rstart + src_size;
	size_t size_to_copy = size;
	size_t space_avail;
	size_t n;
	uint64_t timeout;
	uint64_t current_time;
	uint8_t *w;

	comp_dbg(mod->dev, "entry");

	/* We are allowed to buffer data in internal history buffer
	 * only in KPB_STATE_RUN, KPB_STATE_DRAINING or KPB_STATE_INIT_DRAINING
	 * states.
	 */
	if (kpb->state != KPB_STATE_RUN &&
	    kpb->state != KPB_STATE_DRAINING &&
	    kpb->state != KPB_STATE_INIT_DRAINING) {
		comp_err(mod->dev, "wrong state! (current state %d, state log %x)",
			 kpb->state, kpb->state_log);
		return -EIO;
	}

	timeout = sof_cycle_get_64() + k_ms_to_cyc_ceil64(1);
	/* Let's store audio stream data in internal history buffer */
	while (size_to_copy) {
		/* Are we stuck in buffering? */
		current_time = sof_cycle_get_64();
		if (timeout < current_time) {
			timeout = k_cyc_to_ms_near64(current_time - timeout);
			if (timeout <= UINT_MAX)
				comp_err(mod->dev,
					 "timeout of %u [ms] (current state %d, state log %x)",
					 (unsigned int)(timeout), kpb->state,
					 kpb->state_log);
			else
				comp_err(mod->dev,
					 "timeout > %u [ms] (current state %d, state log %x)",
					 UINT_MAX, kpb->state,
					 kpb->state_log);
			return -ETIME;
		}

		/* Copy what fits in the current write buffer */
		space_avail = (uintptr_t)buff->end_addr - (uintptr_t)buff->w_ptr;
		n = MIN(size_to_copy, space_avail);
		w = buff->w_ptr;
		kpb_circ_copy(&w, w, w + n, &r, rstart, rend, n);
		buff->w_ptr = (char *)buff->w_ptr + n;
		size_to_copy -= n;

		/* Have we filled whole buffer? */
		if (buff->w_ptr == buff->end_addr) {
			/* Reset write pointer back to the beginning
			 * of the buffer.
			 */
			buff->w_ptr = buff->start_addr;
			/* If we have more buffers use them */
			if (buff->next && buff->next != buff) {
				/* Mark current buffer FULL */
				buff->state = KPB_BUFFER_FULL;
				/* Use next buffer available on the list
				 * of buffers.
				 */
				buff = buff->next;
				/* Update also component container,
				 * so next time we enter buffering function
				 * we will know right away what is the current
				 * write buffer
				 */
				kpb->hd.c_hb = buff;
			}
			/* Mark buffer as FREE */
			buff->state = KPB_BUFFER_FREE;
		}
	}

	return 0;
}

#if CONFIG_AMS

/* Key-phrase detected message*/
static const ams_uuid_t ams_kpd_msg_uuid = AMS_KPD_MSG_UUID;

/* Key-phrase detected notification handler*/
static void kpb_ams_kpd_notification(const struct ams_message_payload *const ams_message_payload,
				     void *ctx)
{
	struct kpb_client *cli_data = (struct kpb_client *)ams_message_payload->message;
	struct comp_dev *dev = ctx;

	comp_dbg(dev, "entry");

	kpb_init_draining(comp_mod(dev), cli_data);
}

#else /* CONFIG_AMS */

static void kpb_on_wov_ctrl(void *arg, enum notify_id id, void *data)
{
	struct comp_dev *dev = arg;
	struct processing_module *mod = comp_mod(dev);
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	const struct wov_ctrl_notif *n = data;

	if (n->cmd != WOV_ARB_CMD_RESUME)
		return;

	comp_info(dev, "kpb: received WOV_ARB_CMD_RESUME, current state=%d", kpb->state);
	kpb_lock(kpb);
	if (kpb->state == KPB_STATE_DRAINING ||
	    kpb->state == KPB_STATE_INIT_DRAINING ||
	    kpb->state == KPB_STATE_HOST_COPY) {
		schedule_task_cancel(&kpb->draining_task);
		kpb->draining_task_data.drain_req = 0;
		kpb->draining_task_data.buffered_while_draining = 0;
		if (kpb->hd.c_hb)
			kpb_reset_history_buffer(kpb->hd.c_hb);
		kpb->hd.buffered = 0;
		kpb->hd.free = kpb->hd.buffer_size;
		kpb_change_state(mod, kpb, KPB_STATE_RUN);
		comp_info(dev, "kpb: resumed to RUN state");
	}
	/* draining is over, let the DSP enter D0i3 again */
	kpb_pm_release(kpb);
	kpb_unlock(kpb);
}

/**
 * \brief Main event dispatcher.
 * \param[in] arg - KPB component device.
 * \param[in] type - notification type
 * \param[in] event_data - event specific data.
 */
static void kpb_event_handler(void *arg, enum notify_id type, void *event_data)
{
	struct comp_dev *dev = arg;
	struct kpb_event_data *evd = event_data;
	struct kpb_client *cli = evd->client_data;

	/* client data may have been written by another core */
	if (cli)
		dcache_invalidate_region((__sparse_force void __sparse_cache *)cli,
					 sizeof(*cli));

	comp_info(dev, "received event with ID: %d ", evd->event_id);

	switch (evd->event_id) {
	case KPB_EVENT_REGISTER_CLIENT:
	case KPB_EVENT_UNREGISTER_CLIENT:
	case KPB_EVENT_STOP_DRAINING:
		/* no client registry, any client may request draining */
		break;
	case KPB_EVENT_BEGIN_DRAINING:
		if (!cli) {
			comp_err(dev, "no client data");
			break;
		}
		kpb_init_draining(comp_mod(dev), cli);
		break;
	default:
		comp_err(dev, "unsupported command");
		break;
	}
}

#endif /* CONFIG_AMS */

/**
 * \brief Prepare history buffer for draining.
 *
 * \param[in] mod - KPB module.
 * \param[in] cli - client's data.
 */
static void kpb_init_draining(struct processing_module *mod, struct kpb_client *cli)
{
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;
	struct kpb_drain_data *dd = &kpb->draining_task_data;
	struct history_buffer *buff;
	struct history_buffer *first_buff;
	size_t sample_width = kpb->config.sampling_width;
	size_t bytes_per_ms = (size_t)(kpb->config.sampling_freq / 1000) *
			      (KPB_SAMPLE_CONTAINER_SIZE(sample_width) / 8) *
			      kpb->config.channels;
	size_t drain_req = (size_t)cli->drain_req * bytes_per_ms;
	size_t host_period_size = kpb->host_period_size;
	size_t buffered = 0;
	size_t local_buffered;
	size_t drain_interval;
	size_t period_bytes_limit;
	int sink_state;

	if (!kpb->host_sink) {
		if (!kpb->sel_sink) {
			comp_warn(dev, "kpb_init_draining: no drain path, skipping");
			return;
		}
		comp_warn(dev, "kpb_init_draining: no host_sink, draining via sel_sink");
		kpb->host_sink = kpb->sel_sink;
	}

	sink_state = sink_get_state(kpb->host_sink);

	comp_info(dev, "requested draining of %d [ms] from history buffer",
		  cli->drain_req);

	if (kpb->state != KPB_STATE_RUN) {
		comp_err(dev, "wrong KPB state: %d", kpb->state);
		return;
	}
	if (cli->id > KPB_MAX_NO_OF_CLIENTS) {
		comp_err(dev, "wrong client id: %u", cli->id);
		return;
	}
	if (sink_state != COMP_STATE_ACTIVE && sink_state != COMP_STATE_PREPARE) {
		comp_err(dev, "sink not ready for draining (sink_state=%d)", sink_state);
		return;
	}
	if (cli->drain_req > kpb_get_buff_time_ms(kpb)) {
		comp_err(dev, "drain request exceeds max");
		return;
	}

	if (kpb->hd.buffered < drain_req) {
		comp_warn(dev, "partial pre-roll: capping drain to buffered");
		drain_req = kpb->hd.buffered;
	}

	/* Draining accepted, find proper buffer to start reading.
	 * If less history than requested is buffered, drain_req is
	 * capped above so we drain whatever is available.
	 */
	kpb_lock(kpb);

	/* nothing recorded while draining yet, process() counts from here */
	dd->buffered_while_draining = 0;
	kpb_change_state(mod, kpb, KPB_STATE_INIT_DRAINING);

	/* Set history buffer size so new data won't overwrite those
	 * staged for draining.
	 */
	kpb->hd.free = kpb->hd.buffer_size - drain_req;

	/* Find buffer to start draining from */
	buff = kpb->hd.c_hb;
	first_buff = buff;
	do {
		/* Calculate how much data we have stored in
		 * current buffer.
		 */
		buff->r_ptr = buff->start_addr;
		if (buff->state == KPB_BUFFER_FREE) {
			local_buffered = (uintptr_t)buff->w_ptr -
					 (uintptr_t)buff->start_addr;
			buffered += local_buffered;
		} else if (buff->state == KPB_BUFFER_FULL) {
			local_buffered = (uintptr_t)buff->end_addr -
					 (uintptr_t)buff->start_addr;
			buffered += local_buffered;
		} else {
			comp_err(dev, "incorrect buffer label");
		}
		/* Check if this is already sufficient to start draining
		 * if not, go to previous buffer and continue
		 * calculations.
		 */
		if (drain_req > buffered) {
			if (buff->prev == first_buff) {
				/* We went full circle and still don't
				 * have sufficient data for draining.
				 * That means we need to look up the
				 * first buffer again. Our read pointer
				 * is somewhere between write pointer
				 * and buffer's end address.
				 */
				buff = buff->prev;
				buffered += (uintptr_t)buff->end_addr -
					    (uintptr_t)buff->w_ptr;
				buff->r_ptr = (char *)buff->w_ptr +
					      (buffered - drain_req);
				break;
			}
			buff = buff->prev;
		} else if (drain_req == buffered) {
			buff->r_ptr = buff->start_addr;
			break;
		} else {
			buff->r_ptr = (char *)buff->start_addr +
				      (buffered - drain_req);
			break;
		}

	} while (buff != first_buff);

	kpb_unlock(kpb);

	/* Should we drain in synchronized mode (sync_draining_mode)?
	 * Note! We have already verified host params during
	 * kpb_prepare().
	 */
	if (kpb->sync_draining_mode) {
		/* Calculate time in clock ticks each draining event
		 * shall take place. This time will be used to
		 * synchronize us with application interrupts.
		 */
		drain_interval = k_ms_to_cyc_ceil64(host_period_size / bytes_per_ms);
		period_bytes_limit = host_period_size;
		comp_info(dev, "sync_draining_mode selected with interval %u [uS].",
			  (unsigned int)k_cyc_to_us_near64(drain_interval));
	} else {
		/* Unlimited draining */
		drain_interval = 0;
		period_bytes_limit = 0;
		comp_info(dev, "unlimited draining speed selected.");
	}

	comp_info(dev, "schedule draining task");

	/* Add one-time draining task into the scheduler. */
	dd->sink = kpb->host_sink;
	dd->hb = buff;
	dd->drain_req = drain_req;
	dd->drained = 0;
	dd->sample_width = sample_width;
	dd->drain_interval = drain_interval;
	dd->period_copy_start = 0;
	dd->pb_limit = period_bytes_limit;
	dd->period_bytes = 0;
	dd->next_copy_time = 0;
	dd->mod = mod;
	dd->sync_mode_on = kpb->sync_draining_mode;
	dd->task_iteration = 0;
	dd->prev_adjustment_time = 0;
	dd->prev_adjustment_drained = 0;

	/* Keep the DSP out of D0i3 until the arbiter resumes listening */
	if (!kpb->pm_lock_taken &&
	    !pm_runtime_is_active(PM_RUNTIME_DSP, PLATFORM_PRIMARY_CORE_ID)) {
		pm_runtime_disable(PM_RUNTIME_DSP, PLATFORM_PRIMARY_CORE_ID);
		kpb->pm_lock_taken = true;
	}

	comp_info(dev, "Scheduling draining task");

	/* Change KPB internal state to DRAINING */
	kpb_change_state(mod, kpb, KPB_STATE_DRAINING);

	dd->draining_time_start = sof_cycle_get_64();

	/* Schedule draining task */
	schedule_task(&kpb->draining_task, 0, 0);
}

static void adjust_drain_interval(struct kpb_comp_data *kpb, struct kpb_drain_data *dd)
{
	uint64_t now;	/* timestamp in wall-clock cycles */

	/* readjast drain_interval every 32 task iterations */
	if (dd->task_iteration++ % 32)
		return;

	now = sof_cycle_get_64();

	if (dd->prev_adjustment_time) {
		size_t drained;
		size_t elapsed;
		size_t actual_pace, optimal_pace;
		size_t pipeline_period;

		drained = dd->drained - dd->prev_adjustment_drained;
		elapsed = now - dd->prev_adjustment_time;
		assert(elapsed);
		/* average drained bytes per second */
		actual_pace = (size_t)k_ms_to_cyc_ceil64(1000) / elapsed * drained;

		pipeline_period = (size_t)(kpb->config.sampling_freq / 1000) *
			(KPB_SAMPLE_CONTAINER_SIZE(dd->sample_width) / 8) * kpb->config.channels;
		/* desired draining pace in bytes per second */
		optimal_pace = pipeline_period * 1000;

		/* just in case to prevent div by 0 if draining is stuck (e.g. because of host) */
		if (actual_pace) {
			if (actual_pace < optimal_pace) {
				dd->drain_interval /= optimal_pace / actual_pace;
				dd->drain_interval -= dd->drain_interval / 8;
			} else if (actual_pace > optimal_pace) {
				dd->drain_interval *= actual_pace / optimal_pace;
				dd->drain_interval += dd->drain_interval / 8;
			}
			/* the above algorithm will get stuck if the drain_interval is below 8 */
			if (dd->drain_interval < 8)
				dd->drain_interval = 8;
		}
	}

	dd->prev_adjustment_time = now;
	dd->prev_adjustment_drained = dd->drained;
}

/**
 * \brief Draining task.
 *
 * \param[in] arg - pointer keeping draining data previously prepared
 * by kpb_init_draining().
 *
 * \return task state.
 */
static enum task_state kpb_draining_task(void *arg)
{
	struct kpb_drain_data *draining_data = arg;
	struct processing_module *mod = draining_data->mod;
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	struct sof_sink *sink = draining_data->sink;
	struct history_buffer *buff = draining_data->hb;
	size_t avail;
	size_t size_to_copy;
	uint64_t draining_time_end;
	uint64_t draining_time_ms;
	size_t period_bytes_limit = draining_data->pb_limit;
	size_t *rt_stream_update = &draining_data->buffered_while_draining;
	bool sync_mode_on = draining_data->sync_mode_on;
	int ret;

	/*
	 * WORKAROUND: The code below accesses the KPB sink buffer. The EDF task
	 * thread has preemptible low priority and so can be preempted by the LL
	 * thread, which could result in a broken state of the sink buffer.
	 * Hence k_sched_lock() is used temporary to block LL from preempting the
	 * EDF task thread.
	 */
	k_sched_lock();

	comp_dbg(mod->dev, "entry");

	if (kpb->state != KPB_STATE_DRAINING || !sink) {
		draining_data->drain_req = 0;
		goto out;
	}

	adjust_drain_interval(kpb, draining_data);

	if (draining_data->drain_req > 0) {
		/* Are we ready to drain further or host still need some time
		 * to read the data already provided?
		 */
		if (sync_mode_on && draining_data->next_copy_time > sof_cycle_get_64()) {
			/* Restore original EDF thread priority */
			k_sched_unlock();
			return SOF_TASK_STATE_RESCHEDULE;
		}

		if (draining_data->period_copy_start == 0) {
			/* starting new draining period */
			draining_data->period_copy_start = sof_cycle_get_64();
			draining_data->period_bytes = 0;
		}

		avail = (uintptr_t)buff->end_addr - (uintptr_t)buff->r_ptr;
		size_to_copy = MIN(avail,
				   MIN(draining_data->drain_req, sink_get_free_size(sink)));

		if (size_to_copy) {
			ret = kpb_drain_to_sink(sink, buff->r_ptr, size_to_copy);
			if (ret) {
				comp_err(mod->dev, "draining failed %d", ret);
				size_to_copy = 0;
			}
		}

		buff->r_ptr = (char *)buff->r_ptr + (uint32_t)size_to_copy;
		draining_data->drain_req -= size_to_copy;
		draining_data->drained += size_to_copy;
		draining_data->period_bytes += size_to_copy;
		kpb->hd.free += MIN(kpb->hd.buffer_size -
				    kpb->hd.free, size_to_copy);

		/* no data left in the current buffer -- switch to the next buffer */
		if (size_to_copy == avail) {
			buff->r_ptr = buff->start_addr;
			draining_data->hb = buff->next;
		}

		if (sync_mode_on && draining_data->period_bytes >= period_bytes_limit) {
			draining_data->next_copy_time = draining_data->period_copy_start +
				draining_data->drain_interval;
			draining_data->period_copy_start = 0;
		} else if (size_to_copy == 0 && draining_data->drain_req > 0) {
			/* sink is full, retry when its consumer had time to run */
			draining_data->next_copy_time = sof_cycle_get_64() +
				(draining_data->drain_interval ? draining_data->drain_interval :
				 k_ms_to_cyc_ceil64(1));
		} else {
			draining_data->next_copy_time = 0;
		}

		if (draining_data->drain_req == 0) {
			kpb_lock(kpb);
			/*
			 * While draining of pre-roll history was underway, the real-time
			 * stream buffered newly arrived frames into the history buffer
			 * (*rt_stream_update). Drain them directly into the sink before
			 * transitioning to HOST_COPY so no frames are lost at the boundary.
			 */
			while (*rt_stream_update > 0) {
				buff = draining_data->hb;
				avail = (uintptr_t)buff->end_addr - (uintptr_t)buff->r_ptr;
				size_to_copy = MIN(avail,
						   MIN(*rt_stream_update, sink_get_free_size(sink)));
				if (!size_to_copy)
					break;

				ret = kpb_drain_to_sink(sink, buff->r_ptr, size_to_copy);
				if (ret) {
					comp_err(mod->dev, "draining failed %d", ret);
					break;
				}

				buff->r_ptr = (char *)buff->r_ptr + (uint32_t)size_to_copy;
				*rt_stream_update -= size_to_copy;
				draining_data->drained += size_to_copy;
				kpb->hd.free += MIN(kpb->hd.buffer_size -
						    kpb->hd.free, size_to_copy);

				if (size_to_copy == avail) {
					buff->r_ptr = buff->start_addr;
					draining_data->hb = buff->next;
				}
			}

			if (*rt_stream_update == 0 && kpb->state == KPB_STATE_DRAINING) {
				/* Draining is completely done. Switch KPB to copy real time
				 * stream to client's sink (HOST_COPY state).
				 */
				kpb_change_state(mod, kpb, KPB_STATE_HOST_COPY);
			} else if (*rt_stream_update > 0) {
				draining_data->drain_req = *rt_stream_update;
				*rt_stream_update = 0;
			}
			kpb_unlock(kpb);
		}
	}

out:
	if (draining_data->drain_req > 0) {
		/* Restore original EDF thread priority */
		k_sched_unlock();

		/* continue draining on next task iteration */
		return SOF_TASK_STATE_RESCHEDULE;
	}

	/* finished draining */
	draining_time_end = sof_cycle_get_64();

	draining_time_ms = k_cyc_to_ms_near64(draining_time_end -
					      draining_data->draining_time_start);
	if (draining_time_ms <= UINT_MAX)
		comp_info(mod->dev, "KPB: done. %zu drained in %u ms",
			  draining_data->drained, (unsigned int)draining_time_ms);
	else
		comp_info(mod->dev, "KPB: done. %zu drained in > %u ms",
			  draining_data->drained, UINT_MAX);

	/* Restore original EDF thread priority */
	k_sched_unlock();

	return SOF_TASK_STATE_COMPLETED;
}

static inline bool validate_host_params(struct processing_module *mod,
					size_t host_period_size,
					size_t host_buffer_size,
					size_t hb_size_req)
{
	/* The aim of this function is to perform basic check of host params
	 * and reject them if they won't allow for stable draining.
	 * Note however that this is highly recommended for host buffer to
	 * be at least twice the history buffer size. This will quarantee
	 * "safe" draining.
	 * By safe we mean no XRUNs(host was unable to read data on time),
	 * or loss of data due to host delayed read. The later condition
	 * is very likely after wake up from power state like d0ix.
	 */
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;
	size_t sample_width = kpb->config.sampling_width;
	size_t bytes_per_ms = (size_t)(kpb->config.sampling_freq / 1000) *
			      (KPB_SAMPLE_CONTAINER_SIZE(sample_width) / 8) *
			      kpb->config.channels;
	size_t pipeline_period_size = (dev->period / 1000) * bytes_per_ms;

	if (!host_period_size || !host_buffer_size) {
		/* Wrong host params */
		comp_err(dev, "kpb: host_period_size (%zu) cannot be 0 and host_buffer_size (%zu) cannot be 0",
			 host_period_size, host_buffer_size);
		return false;
	} else if (HOST_BUFFER_MIN_SIZE(hb_size_req, kpb->config.channels) >
		   host_buffer_size) {
		/* Host buffer size is too small - history data
		 * may get overwritten.
		 */
		comp_warn(dev, "kpb: host_buffer_size (%zu) must be at least %zu",
			  host_buffer_size,
			  HOST_BUFFER_MIN_SIZE(hb_size_req, kpb->config.channels));
	} else if (kpb->sync_draining_mode) {
		/* Sync draining allowed. Check if we can perform draining
		 * with current settings.
		 * In this mode we copy host period size to host
		 * (to avoid overwrite of buffered data by real time stream
		 * this period shall be bigger than pipeline period) and
		 * give host some time to read it. Therefore, in worst
		 * case scenario, we copy one period of real time data + some
		 * of buffered data.
		 */
		if ((host_period_size / KPB_DRAIN_NUM_OF_PPL_PERIODS_AT_ONCE) <
		    pipeline_period_size) {
			comp_err(dev, "kpb: host_period_size (%zu) must be at least %d * %zu",
				 host_period_size,
				 KPB_DRAIN_NUM_OF_PPL_PERIODS_AT_ONCE,
				 pipeline_period_size);
			return false;
		}
	}

	return true;
}

/* Derive the stream parameters from the IPC4 base config */
static void kpb_set_params(struct kpb_comp_data *kpb, const struct ipc4_base_module_cfg *base_cfg)
{
	const struct ipc4_audio_format *fmt = &base_cfg->audio_fmt;

	kpb->host_buffer_size = base_cfg->ibs * kpb_get_buff_time_ms(kpb) * fmt->channels_count;
	kpb->host_period_size = fmt->channels_count * (fmt->depth / 8) *
				(fmt->sampling_frequency / 1000);
	kpb->config.sampling_width = fmt->depth;
	kpb->frame_bytes = (fmt->depth / 8) * fmt->channels_count;
}

/*
 * \brief Create a key phrase buffer component.
 * \param[in] mod - KPB module.
 */
static int kpb_init(struct processing_module *mod)
{
	const struct ipc4_base_module_cfg *base_cfg = &mod->priv.cfg.base_cfg;
	struct comp_dev *dev = mod->dev;
	struct task_ops ops = {
		.run = kpb_draining_task,
		.get_deadline = kpb_task_deadline,
	};
	struct kpb_comp_data *kpb;
	int ret;

	comp_info(dev, "entry");

	kpb = mod_zalloc(mod, sizeof(*kpb));
	if (!kpb)
		return -ENOMEM;

	module_set_private_data(mod, kpb);

	kpb->config.channels = base_cfg->audio_fmt.channels_count;
	kpb->config.sampling_freq = base_cfg->audio_fmt.sampling_frequency;
	kpb->config.sampling_width = base_cfg->audio_fmt.valid_bit_depth;

	if (!kpb_is_sample_width_supported(kpb->config.sampling_width)) {
		comp_err(dev, "requested sampling width not supported");
		ret = -EINVAL;
		goto err;
	}

	if (kpb->config.channels > KPB_MAX_SUPPORTED_CHANNELS) {
		comp_err(dev, "no of channels exceeded the limit");
		ret = -EINVAL;
		goto err;
	}

	if (kpb->config.sampling_freq != 16000 && kpb->config.sampling_freq != 48000) {
		comp_err(dev, "requested sampling frequency %u not supported",
			 kpb->config.sampling_freq);
		ret = -EINVAL;
		goto err;
	}

	k_mutex_init(&kpb->lock);

	/* Initialize draining task */
	ret = schedule_task_init_edf(&kpb->draining_task, /* task structure */
				     SOF_UUID(kpb_task_uuid), /* task uuid */
				     &ops, /* task ops */
				     &kpb->draining_task_data, /* task private data */
				     0, /* core on which we should run */
				     0); /* no flags */
	if (ret < 0) {
		comp_err(dev, "draining task init failed %d", ret);
		goto err;
	}

	kpb->draining_task_data.mod = mod;
	kpb->hd.c_hb = NULL;
	kpb->state_log = 0;
	kpb_set_params(kpb, base_cfg);
#if CONFIG_AMS
	kpb->kpd_uuid_id = AMS_INVALID_MSG_TYPE;
#endif

	kpb_change_state(mod, kpb, KPB_STATE_CREATED);

	/* one input, real time and drain outputs */
	mod->max_sources = 1;
	mod->max_sinks = 2;

	return 0;

err:
	module_set_private_data(mod, NULL);
	mod_free(mod, kpb);
	return ret;
}

/**
 * \brief Reclaim memory of a key phrase buffer.
 * \param[in] mod - KPB module.
 */
static int kpb_free(struct processing_module *mod)
{
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;

	comp_info(dev, "entry");

	if (!kpb)
		return 0;

#if CONFIG_AMS
	/* Unregister KPB as AMS consumer */
	int ret;

	ret = ams_helper_unregister_consumer(dev, kpb->kpd_uuid_id,
					     kpb_ams_kpd_notification);
	if (ret)
		comp_err(dev, "AMS unregister error %d", ret);
#else
	/* Unregister KPB from notifications */
	notifier_unregister(dev, NULL, NOTIFIER_ID_KPB_CLIENT_EVT);
	notifier_unregister(dev, NULL, NOTIFIER_ID_WOV_CTRL);
#endif /* CONFIG_AMS */

	/* remove scheduling */
	schedule_task_cancel(&kpb->draining_task);
	schedule_task_free(&kpb->draining_task);

	kpb_pm_release(kpb);

	/* Reclaim memory occupied by history buffer */
	kpb_free_history_buffer(kpb->hd.c_hb);
	kpb->hd.c_hb = NULL;
	kpb->hd.buffer_size = 0;

	kpb_change_state(mod, kpb, KPB_STATE_DISABLED);

	mod_free(mod, kpb);

	return 0;
}

/**
 * \brief Prepare key phrase buffer.
 * \param[in] mod - KPB module.
 */
static int kpb_prepare(struct processing_module *mod,
		       struct sof_source **sources, int num_of_sources,
		       struct sof_sink **sinks, int num_of_sinks)
{
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	const struct ipc4_base_module_cfg *base_cfg = &mod->priv.cfg.base_cfg;
	struct comp_dev *dev = mod->dev;
	size_t hb_size_req;
	uint32_t pin;
	int ret;
	int i;

	comp_dbg(dev, "entry");

	/* The module adapter calls prepare before it checks the component
	 * state, don't drop the history of a prepared or running KPB.
	 */
	if (dev->state == COMP_STATE_PREPARE || dev->state == COMP_STATE_ACTIVE) {
		comp_warn(dev, "kpb already prepared, state %d", dev->state);
		return 0;
	}

	kpb_set_params(kpb, base_cfg);

	/* Use topology-set depth if provided; otherwise Kconfig default. */
	hb_size_req = (kpb->config.sampling_freq / 1000) *
		(KPB_SAMPLE_CONTAINER_SIZE(kpb->config.sampling_width) / 8) *
		kpb_get_buff_time_ms(kpb) * kpb->config.channels;

	if (!validate_host_params(mod, kpb->host_period_size,
				  kpb->host_buffer_size, hb_size_req))
		return -EINVAL;

	kpb_change_state(mod, kpb, KPB_STATE_PREPARING);

	/* Init private data */
	kpb->hd.buffered = 0;

	if (kpb->hd.c_hb && kpb->hd.buffer_size < hb_size_req) {
		/* Host params has changed, we need to allocate new buffer */
		kpb_free_history_buffer(kpb->hd.c_hb);
		kpb->hd.c_hb = NULL;
	}

	if (!kpb->hd.c_hb) {
		/* Allocate history buffer */
		kpb->hd.buffer_size = kpb_allocate_history_buffer(mod, kpb, hb_size_req);

		/* Have we allocated what we requested? */
		if (kpb->hd.buffer_size < hb_size_req) {
			comp_err(dev, "failed to allocate space for KPB buffer");
			kpb_free_history_buffer(kpb->hd.c_hb);
			kpb->hd.c_hb = NULL;
			kpb->hd.buffer_size = 0;
			return -EINVAL;
		}
	}
	/* Init history buffer */
	kpb_reset_history_buffer(kpb->hd.c_hb);
	kpb->hd.free = kpb->hd.buffer_size;

	/* Output pins: in a multi-KPB topology each KPB connects to a different
	 * arbiter input pin so the sink queue varies, but the source queue is
	 * always 0 (sel_sink) or 1 (host_sink).
	 */
	kpb->sel_sink = NULL;
	kpb->host_sink = NULL;
	for (i = 0; i < num_of_sinks; i++) {
		pin = IPC4_SRC_QUEUE_ID(sink_get_id(sinks[i]));
		if (pin == 0)
			kpb->sel_sink = sinks[i];
		else if (pin == 1 && !kpb->host_sink)
			kpb->host_sink = sinks[i];
		else
			comp_warn(dev, "unexpected output pin, id=0x%x", sink_get_id(sinks[i]));
	}

	/* Fallback: assign sel_sink and host_sink in order */
	if (!kpb->sel_sink && !kpb->host_sink) {
		for (i = 0; i < num_of_sinks; i++) {
			if (!kpb->sel_sink)
				kpb->sel_sink = sinks[i];
			else if (!kpb->host_sink)
				kpb->host_sink = sinks[i];
		}
	}

	comp_dbg(dev, "sel_sink=%p host_sink=%p", kpb->sel_sink, kpb->host_sink);

	if (!kpb->sel_sink) {
		comp_err(dev, "could not find sink: sel_sink %p", kpb->sel_sink);
		return -EIO;
	}

	/* input and outputs have the same format */
	for (i = 0; i < num_of_sources; i++)
		ipc4_update_source_format(sources[i], &base_cfg->audio_fmt);
	for (i = 0; i < num_of_sinks; i++)
		ipc4_update_sink_format(sinks[i], &base_cfg->audio_fmt);

#if CONFIG_AMS
	/* AMS Register KPB for notification */
	ret = ams_helper_register_consumer(dev, &kpb->kpd_uuid_id,
					   ams_kpd_msg_uuid,
					   kpb_ams_kpd_notification);
#else
	/* Register KPB for notification (ensure no duplicate registrations) */
	notifier_unregister(dev, NULL, NOTIFIER_ID_KPB_CLIENT_EVT);
	notifier_unregister(dev, NULL, NOTIFIER_ID_WOV_CTRL);
	ret = notifier_register(dev, NULL, NOTIFIER_ID_KPB_CLIENT_EVT,
				kpb_event_handler, 0);
	if (ret >= 0)
		ret = notifier_register(dev, NULL, NOTIFIER_ID_WOV_CTRL,
					kpb_on_wov_ctrl, 0);
#endif /* CONFIG_AMS */

	if (ret < 0) {
#if !CONFIG_AMS
		notifier_unregister(dev, NULL, NOTIFIER_ID_KPB_CLIENT_EVT);
#endif
		kpb_free_history_buffer(kpb->hd.c_hb);
		kpb->hd.c_hb = NULL;
		kpb->hd.buffer_size = 0;
		return -ENOMEM;
	}

	kpb->sync_draining_mode = false;

	kpb_change_state(mod, kpb, KPB_STATE_RUN);

	return 0;
}

/**
 * \brief Resets KPB component.
 * \param[in] mod - KPB module.
 */
static int kpb_reset(struct processing_module *mod)
{
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	struct comp_dev *dev = mod->dev;

	comp_info(dev, "resetting from state %d, state log %x",
		  kpb->state, kpb->state_log);

#if !CONFIG_AMS
	/* Unregister KPB from notifications */
	notifier_unregister(dev, NULL, NOTIFIER_ID_KPB_CLIENT_EVT);
	notifier_unregister(dev, NULL, NOTIFIER_ID_WOV_CTRL);
#endif

	switch (kpb->state) {
	case KPB_STATE_DISABLED:
	case KPB_STATE_CREATED:
		/* Nothing to reset */
		break;
	default:
		/* Cancel any scheduled draining task immediately on reset */
		schedule_task_cancel(&kpb->draining_task);
		kpb->draining_task_data.drain_req = 0;
		kpb->draining_task_data.buffered_while_draining = 0;

		kpb->hd.buffered = 0;
		kpb->sel_sink = NULL;
		kpb->host_sink = NULL;
		kpb->host_buffer_size = 0;
		kpb->host_period_size = 0;

		/* Reset history buffer - reset pointers and states */
		if (kpb->hd.c_hb)
			kpb_reset_history_buffer(kpb->hd.c_hb);

		/* Must transition away from KPB_STATE_RUN so that a subsequent
		 * process() does not see a stale RUN state and immediately
		 * begin copying before the next prepare completes.
		 */
		kpb_change_state(mod, kpb, KPB_STATE_PREPARING);
		break;
	}

	kpb_pm_release(kpb);

	return 0;
}

static int kpb_trigger(struct processing_module *mod, int cmd)
{
	comp_info(mod->dev, "cmd %d", cmd);

	return module_adapter_set_state(mod, mod->dev, cmd);
}

/* Forget an output that is being disconnected, it must not be used any more */
static int kpb_unbind(struct processing_module *mod, const struct bind_info *unbind_data)
{
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	struct sof_sink *sink;

	if (unbind_data->bind_type != COMP_BIND_TYPE_SINK)
		return 0;

	sink = unbind_data->sink;

	if (kpb->draining_task_data.sink == sink) {
		schedule_task_cancel(&kpb->draining_task);
		kpb->draining_task_data.drain_req = 0;
		kpb->draining_task_data.sink = NULL;
	}
	if (kpb->host_sink == sink)
		kpb->host_sink = NULL;
	if (kpb->sel_sink == sink)
		kpb->sel_sink = NULL;

	return 0;
}

/* RUN: copy whole frames to sel_sink and record the consumed input in history */
static int kpb_process_run(struct processing_module *mod, struct kpb_comp_data *kpb,
			   struct sof_source *source)
{
	struct comp_dev *dev = mod->dev;
	struct sof_sink *sink = kpb->sel_sink;
	size_t frame_bytes = kpb->frame_bytes;
	const void *src_ptr, *src_start;
	void *dst_ptr, *dst_start;
	size_t src_size, dst_size;
	size_t src_avail, copy_bytes, consume_bytes;
	int ret;

	if (!sink) {
		comp_warn(dev, "no sink.");
		return -EINVAL;
	}

	src_avail = source_get_data_available(source);
	if (!src_avail)
		return 0;

	copy_bytes = MIN(src_avail, sink_get_free_size(sink));
	if (frame_bytes)
		copy_bytes = ROUND_DOWN(copy_bytes, frame_bytes);

	/* Consume the input even if sel_sink is full, so the history keeps
	 * filling and upstream never stalls.
	 */
	consume_bytes = copy_bytes ? copy_bytes :
			(frame_bytes ? ROUND_DOWN(src_avail, frame_bytes) : 0);
	if (!consume_bytes)
		return 0;

	ret = source_get_data(source, consume_bytes, &src_ptr, &src_start, &src_size);
	if (ret)
		return ret;

	if (copy_bytes) {
		ret = sink_get_buffer(sink, copy_bytes, &dst_ptr, &dst_start, &dst_size);
		if (ret) {
			source_release_data(source, 0);
			return ret;
		}
		kpb_copy_to_sink(dst_ptr, dst_start, dst_size, src_ptr, src_start, src_size,
				 copy_bytes);
	}

	if (consume_bytes <= kpb->hd.buffer_size) {
		ret = kpb_buffer_data(mod, src_ptr, src_start, src_size, consume_bytes);
		if (ret) {
			comp_err(dev, "internal buffering failed.");
			if (copy_bytes)
				sink_commit_buffer(sink, 0);
			source_release_data(source, 0);
			return ret;
		}

		/* Update buffered size. NOTE! We only record buffered
		 * data up to the size of history buffer.
		 */
		kpb->hd.buffered += MIN(kpb->hd.buffer_size - kpb->hd.buffered,
					consume_bytes);
	} else {
		comp_err(dev, "too much data to buffer.");
	}

	if (copy_bytes)
		sink_commit_buffer(sink, copy_bytes);
	source_release_data(source, consume_bytes);

	return 0;
}

/* HOST_COPY: draining is done, copy the input straight to host_sink */
static int kpb_process_host_copy(struct processing_module *mod, struct kpb_comp_data *kpb,
				 struct sof_source *source)
{
	struct comp_dev *dev = mod->dev;
	struct sof_sink *sink = kpb->host_sink;
	const void *src_ptr, *src_start;
	void *dst_ptr, *dst_start;
	size_t src_size, dst_size;
	size_t copy_bytes;
	int ret;

	if (!sink) {
		comp_err(dev, "no sink.");
		return -EINVAL;
	}

	copy_bytes = MIN(source_get_data_available(source), sink_get_free_size(sink));
	if (kpb->frame_bytes)
		copy_bytes = ROUND_DOWN(copy_bytes, kpb->frame_bytes);
	if (!copy_bytes) {
		comp_dbg(dev, "nothing to copy sink->free %zu source->avail %zu",
			 sink_get_free_size(sink), source_get_data_available(source));
		return 0;
	}

	ret = source_get_data(source, copy_bytes, &src_ptr, &src_start, &src_size);
	if (ret)
		return ret;

	ret = sink_get_buffer(sink, copy_bytes, &dst_ptr, &dst_start, &dst_size);
	if (ret) {
		source_release_data(source, 0);
		return ret;
	}

	kpb_copy_to_sink(dst_ptr, dst_start, dst_size, src_ptr, src_start, src_size, copy_bytes);

	sink_commit_buffer(sink, copy_bytes);
	source_release_data(source, copy_bytes);

	return 0;
}

/* INIT_DRAINING / DRAINING: only record the input in history */
static int kpb_process_draining(struct processing_module *mod, struct kpb_comp_data *kpb,
				struct sof_source *source)
{
	struct comp_dev *dev = mod->dev;
	const void *src_ptr, *src_start;
	size_t src_size;
	size_t copy_bytes;
	int ret;

	copy_bytes = MIN(source_get_data_available(source), kpb->hd.free);
	if (!copy_bytes) {
		comp_warn(dev, "buffering skipped (no data to copy, avail %zu, free %zu",
			  source_get_data_available(source), kpb->hd.free);
		return 0;
	}

	ret = source_get_data(source, copy_bytes, &src_ptr, &src_start, &src_size);
	if (ret)
		return ret;

	ret = kpb_buffer_data(mod, src_ptr, src_start, src_size, copy_bytes);
	if (ret) {
		comp_err(dev, "internal buffering failed.");
		source_release_data(source, 0);
		return ret;
	}

	kpb->draining_task_data.buffered_while_draining += copy_bytes;
	kpb->hd.free -= copy_bytes;

	source_release_data(source, copy_bytes);

	return 0;
}

static int kpb_process(struct processing_module *mod,
		       struct sof_source **sources, int num_of_sources,
		       struct sof_sink **sinks, int num_of_sinks)
{
	struct kpb_comp_data *kpb = module_get_private_data(mod);

	comp_dbg(mod->dev, "entry");

	if (!num_of_sources) {
		comp_err(mod->dev, "no source.");
		return -EINVAL;
	}

	switch (kpb->state) {
	case KPB_STATE_RUN:
		return kpb_process_run(mod, kpb, sources[0]);
	case KPB_STATE_HOST_COPY:
		return kpb_process_host_copy(mod, kpb, sources[0]);
	case KPB_STATE_INIT_DRAINING:
	case KPB_STATE_DRAINING:
		return kpb_process_draining(mod, kpb, sources[0]);
	default:
		comp_err(mod->dev, "wrong state (state %d, state log %x)",
			 kpb->state, kpb->state_log);
		return -EIO;
	}
}

static int kpb_set_config(struct processing_module *mod, uint32_t param_id,
			  enum module_cfg_fragment_position pos, uint32_t data_offset_size,
			  const uint8_t *fragment, size_t fragment_size, uint8_t *response,
			  size_t response_size)
{
	struct kpb_comp_data *kpb = module_get_private_data(mod);
	/* We can use extended param id for both extended and standard param id */
	union ipc4_extended_param_id extended_param_id;

	comp_info(mod->dev, "entry");

	extended_param_id.full = param_id;

	switch (extended_param_id.part.parameter_type) {
	case KP_BUF_CFG_BUFF_TIME_MS:
		if (!fragment || fragment_size < sizeof(uint32_t))
			return -EINVAL;
		kpb->buff_time_ms = *(const uint32_t *)fragment;
		comp_info(mod->dev, "kpb: buff_time_ms set to %u ms", kpb->buff_time_ms);
		return 0;
	case KP_BUF_CFG_FM_MODULE:
	case KP_BUF_CLIENT_MIC_SELECT:
		comp_err(mod->dev, "parameter %u not supported",
			 extended_param_id.part.parameter_type);
		return -EINVAL;
	default:
		return -EINVAL;
	}
}

static const struct module_interface kpb_interface = {
	.init = kpb_init,
	.prepare = kpb_prepare,
	.process = kpb_process,
	.set_configuration = kpb_set_config,
	.trigger = kpb_trigger,
	.unbind = kpb_unbind,
	.reset = kpb_reset,
	.free = kpb_free,
};

DECLARE_TR_CTX(kpb_tr, SOF_UUID(kpb4_uuid), LOG_LEVEL_INFO);
DECLARE_MODULE_ADAPTER(kpb_interface, kpb4_uuid, kpb_tr);
SOF_MODULE_INIT(kpb, sys_comp_module_kpb_interface_init);
