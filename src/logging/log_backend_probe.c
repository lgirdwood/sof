// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2022 Intel Corporation. All rights reserved.

#include <zephyr/logging/log_backend.h>
#include <zephyr/logging/log_core.h>
#include <zephyr/logging/log_output.h>
#include <zephyr/logging/log_backend_std.h>
#include <zephyr/kernel.h>

#include <sof/audio/buffer.h>
#include <sof/ipc/topology.h>
#include <sof/lib/memory.h>
#include <sof/lib/notifier.h>
#include <sof/probe/probe.h>

#ifdef PROBE_LOG_DEBUG
#include <stdio.h>
#endif

/*
 * A lock is needed as log_process() and log_panic() have no internal locks
 * to prevent concurrency. Meaning if log_process is called after
 * log_panic was called previously, log_process may happen from another
 * CPU and calling context than the log processing thread running in
 * the background. On an SMP system this is a race.
 *
 * This caused a race on the output trace such that the logging output
 * was garbled and useless.
 */
static struct k_spinlock probe_lock;

static uint32_t probe_log_format_current = CONFIG_LOG_BACKEND_SOF_PROBE_OUTPUT;

#define LOG_BUF_SIZE 80
static uint8_t log_buf[LOG_BUF_SIZE];

static probe_logging_hook_t probe_hook;

#define PROBE_LOG_FLUSH_INTERVAL_MS 500

static void log_push(uint8_t *data, size_t length)
{
	size_t pos = 0;

	do {
		int ret = probe_hook(data + pos, length - pos);

		if (ret < 0)
			break;
		pos += ret;
	} while (pos < length);
}

#define PRE_BUFFER_SIZE 4096
static struct probe_pre_buffer {
	uint8_t *buf;
	size_t wpos;
	size_t len;
} prebuf;

static void pre_buffer_drain(void)
{
#ifdef PROBE_LOG_DEBUG
	/* NOTE: The debug code only works with ascii/text log output */
	uint64_t stamp = sof_cycle_get_64();
	char msg[80];
	int mlen;

	mlen = snprintf(msg, sizeof(msg), "[Drain %zu bytes of pre buffered logs]\n", prebuf.wpos);
	if (prebuf.len > prebuf.wpos && mlen < sizeof(msg))
		mlen += snprintf(msg + mlen, sizeof(msg) - mlen, "[%zu bytes dropped]\n",
			      prebuf.len - prebuf.wpos);
	log_push(msg, MIN(mlen, sizeof(msg)));
#endif

	log_push(prebuf.buf, prebuf.wpos);
	rfree(prebuf.buf);
	prebuf.buf = NULL;
	prebuf.len = 0;
	prebuf.wpos = 0;

#ifdef PROBE_LOG_DEBUG
	mlen = snprintf(msg, sizeof(msg), "[Buffer drained in %llu us]\n",
			k_cyc_to_us_near64(sof_cycle_get_64() - stamp));

	log_push(msg, MIN(mlen, sizeof(msg)));
#endif
}

static void pre_buffer(uint8_t *data, size_t length)
{
	int ret;

	prebuf.len += length;
	if (!prebuf.buf) {
		prebuf.buf = rzalloc(SOF_MEM_FLAG_USER, PRE_BUFFER_SIZE);
		if (!prebuf.buf)
			return;
	}
	/* Protection against buffer over flow relies on memcpy_s() */
	ret = memcpy_s(&prebuf.buf[prebuf.wpos], PRE_BUFFER_SIZE - prebuf.wpos, data, length);
	if (!ret)
		prebuf.wpos += length;
}

static int probe_char_out(uint8_t *data, size_t length, void *ctx)
{
	if (!probe_hook) {
		pre_buffer(data, length);
	} else {
		if (prebuf.wpos)
			pre_buffer_drain();

		log_push(data, length);
	}

	return length;
}

LOG_OUTPUT_DEFINE(log_output_adsp_probe, probe_char_out, log_buf, sizeof(log_buf));

/*
 * Periodic flush work: every PROBE_LOG_FLUSH_INTERVAL_MS milliseconds:
 *
 * 1. Drain the FW pre-buffer (prebuf) -- messages generated before the
 *    probe DMA was set up.  Normally probe_char_out() drains this lazily
 *    on the next incoming log message, which can be 20+ seconds on an
 *    idle DSP.  Draining here ensures boot and RTD3-resume logs reach the
 *    host within 500ms of probe_logging_init() being called.
 *
 * 2. Flush any partial bytes sitting in the 80-byte Zephyr log_output
 *    buffer (log_buf) that haven't yet been pushed out by a complete
 *    log message.
 *
 * Runs in the system workqueue (thread context), so all Zephyr log and
 * spinlock APIs are safe to call here.
 */
static void probe_log_flush_work_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(probe_log_flush_work, probe_log_flush_work_fn);

static void probe_log_flush_work_fn(struct k_work *work)
{
	bool drained = false;

	if (!probe_hook)
		return; /* hook was cleared; don't reschedule */

	{
		k_spinlock_key_t key = k_spin_lock(&probe_lock);

		/* Drain the FW pre-buffer first so boot logs don't wait for
		 * the next organic log message to trigger the drain path in
		 * probe_char_out(). */
		if (prebuf.wpos) {
			pre_buffer_drain();
			drained = true;
		}

		/* Flush any residual bytes from the 80-byte output buffer. */
		log_output_flush(&log_output_adsp_probe);
		k_spin_unlock(&probe_lock, key);
	}

	/* If neither the pre-buffer nor the log output buffer had anything
	 * to send, write a keep-alive probe packet to the DMA ring so that
	 * probe_task calls dma_reload() and the resulting HDA DMA transfer
	 * fires the host-side interrupt, unblocking compress_read().
	 *
	 * The payload must be large enough that ALIGN_DOWN(total_bytes,
	 * copy_align) > 0.  Intel HDA copy_align is typically 64–128 bytes,
	 * so we use 256 bytes of zeros to guarantee the threshold is always
	 * exceeded regardless of platform.
	 *
	 * Overhead: ~280 bytes per 500 ms = 0.5 KB/s — negligible.
	 */
	if (!drained) {
		static const uint8_t heartbeat_payload[256];

		probe_hook((uint8_t *)heartbeat_payload, sizeof(heartbeat_payload));
	}

	/* Re-arm for next interval */
	k_work_schedule(CONTAINER_OF(work, struct k_work_delayable, work),
			K_MSEC(PROBE_LOG_FLUSH_INTERVAL_MS));
}



static uint32_t format_flags(void)
{
	uint32_t flags = LOG_OUTPUT_FLAG_LEVEL | LOG_OUTPUT_FLAG_TIMESTAMP;

	if (IS_ENABLED(CONFIG_LOG_BACKEND_FORMAT_TIMESTAMP))
		flags |= LOG_OUTPUT_FLAG_FORMAT_TIMESTAMP;

	return flags;
}

static void probe_log_panic(struct log_backend const *const backend)
{
	k_spinlock_key_t key = k_spin_lock(&probe_lock);

	log_backend_std_panic(&log_output_adsp_probe);

	k_spin_unlock(&probe_lock, key);
}

static void probe_log_dropped(const struct log_backend *const backend,
			      uint32_t cnt)
{
	log_output_dropped_process(&log_output_adsp_probe, cnt);
}

static void probe_log_process(const struct log_backend *const backend,
			      union log_msg_generic *msg)
{
	log_format_func_t log_output_func = log_format_func_t_get(probe_log_format_current);

	k_spinlock_key_t key = k_spin_lock(&probe_lock);

	log_output_func(&log_output_adsp_probe, &msg->log, format_flags());

	k_spin_unlock(&probe_lock, key);
}

static int probe_log_format_set(const struct log_backend *const backend, uint32_t log_type)
{
	probe_log_format_current = log_type;
	return 0;
}

/**
 * Lazily initialized, while the DMA may not be setup we continue
 * to buffer log messages untilt he buffer is full.
 */
static void probe_log_init(const struct log_backend *const backend)
{
	ARG_UNUSED(backend);
}

const struct log_backend_api log_backend_adsp_probe_api = {
	.process = probe_log_process,
	.dropped = IS_ENABLED(CONFIG_LOG_MODE_IMMEDIATE) ? NULL : probe_log_dropped,
	.panic = probe_log_panic,
	.format_set = probe_log_format_set,
	.init = probe_log_init,
};

LOG_BACKEND_DEFINE(log_backend_adsp_probe, log_backend_adsp_probe_api, true);

void probe_logging_init(probe_logging_hook_t fn)
{
	probe_hook = fn;
	if (fn) {
		/* Start the periodic flush when the DMA is live */
		k_work_schedule(&probe_log_flush_work,
				K_MSEC(PROBE_LOG_FLUSH_INTERVAL_MS));
	} else {
		/* Stop flushing when the hook is cleared */
		k_work_cancel_delayable(&probe_log_flush_work);
	}
}
EXPORT_SYMBOL(probe_logging_init);

const struct log_backend *log_backend_probe_get(void)
{
	return &log_backend_adsp_probe;
}

bool probe_is_backend_configured(void)
{
	return probe_hook != NULL;
}

#if CONFIG_LOG_BACKEND_SOF_PROBE_BOOT_HOOK_INIT
/*
 * Boot-time dummy hook: accepts no DMA yet, but probe_char_out() will
 * call pre_buffer() for us as long as probe_hook == NULL.
 * We set probe_hook to a sentinel that is NOT NULL so that
 * probe_is_backend_configured() returns true early, but we still gate
 * actual DMA writes on ext_dma being valid inside probe_logging_hook().
 *
 * Strategy: keep probe_hook NULL at boot; the pre_buffer path in
 * probe_char_out() is taken automatically when probe_hook is NULL.
 * Once the real DMA is set up, probe_logging_init(probe_logging_hook)
 * is called which drains the pre-buffer and starts live DMA writes.
 *
 * All we need to do at SYS_INIT time is to make sure the log backend
 * is active (it is, it's registered at link time with LOG_BACKEND_DEFINE).
 * The pre-buffer is allocated lazily on first write.
 */
static int probe_log_boot_init(void)
{
	/* Nothing to do - LOG_BACKEND_DEFINE already registers the backend.
	 * Pre-buffering starts automatically on the first log message because
	 * probe_hook is NULL and probe_char_out() calls pre_buffer().
	 * This function exists as a hook for platforms that need explicit
	 * initialization (e.g. enabling clocks for the DMA controller).
	 */
	return 0;
}
SYS_INIT(probe_log_boot_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
#endif /* CONFIG_LOG_BACKEND_SOF_PROBE_BOOT_HOOK_INIT */
