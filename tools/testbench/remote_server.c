// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2026 Intel Corporation. All rights reserved.
//
// Network Server Interface (sof-remote TCP protocol) for SOF Testbench

#if !defined(__XTENSA__) && !defined(CONFIG_XT_SIMULATOR)

#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include <sof/audio/module_adapter/module/generic.h>
#include <sof/audio/component.h>
#include <sof/audio/buffer.h>
#include <sof/ipc/topology.h>
#include <sof/ipc/driver.h>
#include <sof/lib/cpu.h>
#include <sof/list.h>
#include <platform/lib/ll_schedule.h>
#include <ipc4/header.h>
#include <ipc4/module.h>
#include <ipc4/pipeline.h>
#include <sof_versions.h>

#include "testbench/sof_remote_proto.h"
#include "testbench/remote_server.h"
#include "testbench/topology_ipc4.h"
#include "testbench/trace.h"
#include "testbench/file.h"
#include "testbench/utils.h"

/* IPC4 BaseFW Large Config Parameter IDs */
#define TB_IPC4_FW_CONFIG			0
#define TB_IPC4_HW_CONFIG_GET			1
#define TB_IPC4_MODULES_INFO_GET		9
#define TB_IPC4_PIPELINE_LIST_INFO_GET		12
#define TB_IPC4_MEMORY_STATE_INFO_GET		14
#define TB_IPC4_POWER_STATE_INFO_GET		15
#define TB_IPC4_PERF_MEASUREMENTS_STATE		17
#define TB_IPC4_GLOBAL_PERF_DATA		18
#define TB_IPC4_EXTENDED_GLOBAL_PERF_DATA	30

/* PCM Capture Ring Buffer Capacity (in S16_LE samples: 48000 * 2ch * 2 sec = 192000 samples) */
#define TB_PCM_CAPTURE_RING_SAMPLES		192000

struct system_tick_info_wire {
	uint32_t count;
	uint32_t last_time_elapsed;
	uint32_t max_time_elapsed;
	uint32_t last_ccount;
	uint32_t avg_utilization;
	uint32_t peak_utilization;
	uint32_t peak_utilization_4k;
	uint32_t peak_utilization_8k;
} __attribute__((packed));

struct perf_data_item_comp_wire {
	uint32_t resource_id;
	uint32_t power_mode : 1;
	uint32_t is_removed : 1;
	uint32_t rsvd       : 30;
	uint32_t peak_kcps;
	uint32_t avg_kcps;
	uint64_t total_iteration_count;
	uint64_t total_cycles_consumed;
} __attribute__((packed));

static pthread_mutex_t g_tb_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_tb_cond = PTHREAD_COND_INITIALIZER;
static pthread_mutex_t g_pcm_ring_lock = PTHREAD_MUTEX_INITIALIZER;

static struct testbench_prm *g_tp;
static int g_srv_fd = -1;
static pthread_t g_accept_thread;
static volatile bool g_srv_running;
static volatile bool g_quit_requested;
static volatile bool g_dsp_paused;
static volatile int g_step_ticks_remaining;
static __thread bool g_in_remote_rpc;
static uint32_t g_perf_meas_state = 2; /* 2 = STARTED by default */

/* Per-core telemetry state */
static struct system_tick_info_wire g_core_ticks[CONFIG_CORE_COUNT];
static uint64_t g_total_ticks;

/* Emulated SRAM Windows */
static uint8_t g_win0_fw_regs[4096];
static uint8_t g_win1_outbox[4096];
static uint8_t g_win2_debug[65536];
static uint8_t g_win3_perf[4096];
static char g_last_shell_output[4096];
static uint32_t g_shell_seq;

/* Tone generator & PCM capture state */
static volatile bool g_tone_running;
static char g_pcm_device[32] = "hw:0,0";
static uint32_t g_tone_freq_hz = 1000;
static uint32_t g_pcm_rate = 48000;
static uint32_t g_pcm_channels = 2;
static uint32_t g_pcm_api_used = SOF_AUDIO_API_ALSA;
static uint32_t g_pcm_format = SOF_PCM_FMT_S16_LE;
static uint32_t g_pcm_xruns;
static uint64_t g_pcm_frames_processed;
static uint32_t g_pcm_last_rms;
static uint32_t g_pcm_last_peak;
static int16_t g_pcm_capture_ring[TB_PCM_CAPTURE_RING_SAMPLES];
static size_t g_pcm_ring_write_idx;
static size_t g_pcm_ring_avail;

/* Cached Kcontrol state for SOF_NET_OP_KCTL_LIST / GET / SET */
#define TB_REMOTE_MAX_KCTLS 64
struct tb_kctl_shadow {
	bool initialized;
	int64_t values[8];
	uint32_t bytes_len;
	uint8_t bytes_data[512];
};
static struct tb_kctl_shadow g_kctl_shadow[TB_REMOTE_MAX_KCTLS];

/* Dynamically loaded LLEXT / shared-library modules */
#define TB_MAX_LOADED_LLEXTS 16
struct tb_loaded_llext {
	bool active;
	uint32_t lib_id;
	uint32_t data_size;
	char name[16];
	char path[128];
	void *dl_handle;
};
static struct tb_loaded_llext g_loaded_llexts[TB_MAX_LOADED_LLEXTS];

static int read_exact(int fd, void *buf, size_t len)
{
	uint8_t *p = (uint8_t *)buf;
	size_t rem = len;

	while (rem > 0) {
		ssize_t n = recv(fd, p, rem, 0);

		if (n <= 0) {
			if (n < 0 && errno == EINTR)
				continue;
			return -1;
		}
		p += n;
		rem -= (size_t)n;
	}
	return 0;
}

static int write_exact(int fd, const void *buf, size_t len)
{
	const uint8_t *p = (const uint8_t *)buf;
	size_t rem = len;

	while (rem > 0) {
		ssize_t n = send(fd, p, rem, MSG_NOSIGNAL);

		if (n <= 0) {
			if (n < 0 && errno == EINTR)
				continue;
			return -1;
		}
		p += n;
		rem -= (size_t)n;
	}
	return 0;
}

static int send_reply(int sock, uint16_t opcode, uint32_t req_id,
		      int32_t status, const void *payload, uint32_t payload_len)
{
	struct sof_net_hdr hdr = {
		.magic       = SOF_NET_MAGIC,
		.version     = SOF_NET_VERSION,
		.opcode      = opcode,
		.req_id      = req_id,
		.payload_len = payload ? payload_len : 0,
		.status      = status,
	};

	if (write_exact(sock, &hdr, sizeof(hdr)) < 0)
		return -1;
	if (payload && payload_len > 0) {
		if (write_exact(sock, payload, payload_len) < 0)
			return -1;
	}
	return 0;
}

/* -------------------------------------------------------------------------
 * PCM Output Recording & Tone State Hooks (called from file.c)
 * -------------------------------------------------------------------------
 */

bool tb_remote_is_tone_active(uint32_t *freq_hz)
{
	if (!g_tone_running)
		return false;
	if (freq_hz)
		*freq_hz = g_tone_freq_hz ? g_tone_freq_hz : 1000;
	return true;
}

void tb_remote_record_output_samples_s16(const int16_t *samples, size_t count,
					 int channels, int rate)
{
	double sum_sq = 0.0;
	int16_t peak = 0;
	size_t i;

	if (!samples || count == 0)
		return;

	pthread_mutex_lock(&g_pcm_ring_lock);
	if (channels > 0)
		g_pcm_channels = (uint32_t)channels;
	if (rate > 0)
		g_pcm_rate = (uint32_t)rate;

	for (i = 0; i < count; i++) {
		int16_t v = samples[i];
		int16_t av = v < 0 ? -v : v;

		if (av > peak)
			peak = av;
		sum_sq += (double)v * (double)v;

		g_pcm_capture_ring[g_pcm_ring_write_idx] = v;
		g_pcm_ring_write_idx = (g_pcm_ring_write_idx + 1) % TB_PCM_CAPTURE_RING_SAMPLES;
		if (g_pcm_ring_avail < TB_PCM_CAPTURE_RING_SAMPLES)
			g_pcm_ring_avail++;
	}

	g_pcm_last_rms = (uint32_t)sqrt(sum_sq / (double)count);
	g_pcm_last_peak = (uint32_t)peak;
	if (g_pcm_channels > 0)
		g_pcm_frames_processed += count / g_pcm_channels;
	pthread_mutex_unlock(&g_pcm_ring_lock);
}

void tb_remote_record_output_samples_s32(const int32_t *samples, size_t count,
					 int channels, int rate, int shift_to_s16)
{
	double sum_sq = 0.0;
	int16_t peak = 0;
	size_t i;

	if (!samples || count == 0)
		return;

	pthread_mutex_lock(&g_pcm_ring_lock);
	if (channels > 0)
		g_pcm_channels = (uint32_t)channels;
	if (rate > 0)
		g_pcm_rate = (uint32_t)rate;

	for (i = 0; i < count; i++) {
		int32_t s32 = samples[i] >> shift_to_s16;
		int16_t v;
		int16_t av;

		if (s32 > 32767)
			s32 = 32767;
		else if (s32 < -32768)
			s32 = -32768;
		v = (int16_t)s32;
		av = v == -32768 ? 32767 : (v < 0 ? -v : v);
		if (av > peak)
			peak = av;
		sum_sq += (double)v * (double)v;

		g_pcm_capture_ring[g_pcm_ring_write_idx] = v;
		g_pcm_ring_write_idx = (g_pcm_ring_write_idx + 1) % TB_PCM_CAPTURE_RING_SAMPLES;
		if (g_pcm_ring_avail < TB_PCM_CAPTURE_RING_SAMPLES)
			g_pcm_ring_avail++;
	}

	g_pcm_last_rms = (uint32_t)sqrt(sum_sq / (double)count);
	g_pcm_last_peak = (uint32_t)peak;
	if (g_pcm_channels > 0)
		g_pcm_frames_processed += count / g_pcm_channels;
	pthread_mutex_unlock(&g_pcm_ring_lock);
}

/* -------------------------------------------------------------------------
 * Emulated SRAM Windows, Device Info & Telemetry Synthesis
 * -------------------------------------------------------------------------
 */

static struct tplg_comp_info *find_comp_info_by_ipc_id(struct testbench_prm *tp, uint32_t ipc_id)
{
	struct list_item *item;
	int i;

	if (!tp)
		return NULL;

	if (tp->widget_list.next) {
		list_for_item(item, &tp->widget_list) {
			struct tplg_comp_info *ci = container_of(item, struct tplg_comp_info, item);
			uint32_t cid = (uint32_t)IPC4_COMP_ID(ci->module_id, ci->instance_id);

			if (cid == ipc_id || (uint32_t)ci->id == ipc_id)
				return ci;
		}
	}

	if (!tp->info)
		return NULL;

	for (i = 0; i < tp->info_elems; i++) {
		uint32_t cid = (uint32_t)IPC4_COMP_ID(tp->info[i].module_id,
						      tp->info[i].instance_id);

		if (cid == ipc_id || (uint32_t)tp->info[i].id == ipc_id)
			return &tp->info[i];
	}
	return NULL;
}

static void populate_cdev_info(struct testbench_prm *tp, struct sof_cdev_info *info)
{
	const char *base_tplg = "";
	int i;

	memset(info, 0, sizeof(*info));
	info->abi_version = SOF_CDEV_ABI_VERSION;
#if CONFIG_IPC_MAJOR_4
	info->ipc_type = 1;
#else
	info->ipc_type = 0;
#endif
	info->fw_state = 7; /* SOF_FW_BOOT_COMPLETE */
	info->dsp_power_state = g_dsp_paused ? 4 : 1; /* 1=D0, 4=D3 */
	info->pm_runtime_active = g_dsp_paused ? 0 : 1;
	info->num_cores = CONFIG_CORE_COUNT;
	info->enabled_cores_mask = arch_cpu_enabled_cores();
	info->max_payload_size = SOF_IPC_MSG_MAX_SIZE;
	info->fw_ver_major = SOF_MAJOR;
	info->fw_ver_minor = SOF_MINOR;
	info->fw_ver_micro = SOF_MICRO;
	info->fw_ver_build = SOF_BUILD;
	info->hw_ip_version = 4; /* ACE / IPC4 */
	snprintf(info->platform, sizeof(info->platform), "sof-testbench4");

	if (tp && tp->tplg_file) {
		const char *slash = strrchr(tp->tplg_file, '/');

		base_tplg = slash ? (slash + 1) : tp->tplg_file;
	}
	snprintf(info->fw_filename, sizeof(info->fw_filename), "%s", base_tplg);

	/* 4 SRAM windows */
	info->windows[0].win_id = SOF_CDEV_WIN_FW_REGS;
	info->windows[0].bar_offset = 0x000000;
	info->windows[0].size = sizeof(g_win0_fw_regs);
	info->windows[0].flags = 1;

	info->windows[1].win_id = SOF_CDEV_WIN_OUTBOX;
	info->windows[1].bar_offset = 0x001000;
	info->windows[1].size = sizeof(g_win1_outbox);
	info->windows[1].flags = 1;

	info->windows[2].win_id = SOF_CDEV_WIN_DEBUG;
	info->windows[2].bar_offset = 0x010000;
	info->windows[2].size = sizeof(g_win2_debug);
	info->windows[2].flags = 1;

	info->windows[3].win_id = SOF_CDEV_WIN_PERF;
	info->windows[3].bar_offset = 0x020000;
	info->windows[3].size = sizeof(g_win3_perf);
	info->windows[3].flags = 1;

	/* Window 2 Debug Slots */
	for (i = 0; i < SOF_CDEV_MAX_DEBUG_SLOTS; i++) {
		info->debug_slots[i].slot_index = i;
		info->debug_slots[i].win_offset = (i + 1) * 4096;
		info->debug_slots[i].bar_offset = 0x010000 + (i + 1) * 4096;
		info->debug_slots[i].size = 4096;
	}

	info->debug_slots[0].res_id = 0x00000001;
	info->debug_slots[0].type = SOF_CDEV_SLOT_TELEMETRY;
	info->debug_slots[0].vma = 0xA0011000;

	info->debug_slots[1].res_id = 0x00000002;
	info->debug_slots[1].type = SOF_CDEV_SLOT_SHELL;
	info->debug_slots[1].vma = 0xA0012000;

	info->debug_slots[2].res_id = 0x00000003;
	info->debug_slots[2].type = SOF_CDEV_SLOT_DEBUG_LOG;
	info->debug_slots[2].vma = 0xA0013000;
}

static uint32_t build_modules_info_tlv(struct testbench_prm *tp, uint8_t *out, uint32_t cap)
{
	/*
	 * Matches TelemetryParser.parse_modules_info_tlv in tools/softop/backend.py:
	 *   uint32_t mod_count
	 *   followed by mod_count * 116-byte sof_man_module entries:
	 *     bytes 0..1: uint16_t module_id
	 *     bytes 2..3: uint16_t state_flags
	 *     bytes 4..11: char name[8]
	 */
	uint32_t seen_mod_ids[64];
	char seen_names[64][8];
	uint32_t count = 0;
	int i;

	if (cap < 4)
		return 0;

	memset(out, 0, cap);

	/* Always include module_id 0 = basefw */
	seen_mod_ids[count] = 0;
	memset(seen_names[count], 0, 8);
	memcpy(seen_names[count], "basefw", 6);
	count++;

	if (tp && tp->widget_list.next) {
		struct list_item *item;

		list_for_item(item, &tp->widget_list) {
			struct tplg_comp_info *ci = container_of(item, struct tplg_comp_info, item);
			uint32_t mid = (uint32_t)ci->module_id;
			bool dup = false;
			uint32_t j;

			if (count >= 64)
				break;
			if (mid == 0 || !ci->name)
				continue;

			for (j = 0; j < count; j++) {
				if (seen_mod_ids[j] == mid) {
					dup = true;
					break;
				}
			}
			if (dup)
				continue;

			seen_mod_ids[count] = mid;
			memset(seen_names[count], 0, 8);
			const char *dot = strchr(ci->name, '.');
			size_t nlen = dot ? (size_t)(dot - ci->name) : strlen(ci->name);

			if (nlen > 7)
				nlen = 7;
			memcpy(seen_names[count], ci->name, nlen);
			count++;
		}
	} else if (tp && tp->info) {
		for (i = 0; i < tp->info_elems && count < 64; i++) {
			struct tplg_comp_info *ci = &tp->info[i];
			uint32_t mid = (uint32_t)ci->module_id;
			bool dup = false;
			uint32_t j;

			if (mid == 0 || !ci->name)
				continue;

			for (j = 0; j < count; j++) {
				if (seen_mod_ids[j] == mid) {
					dup = true;
					break;
				}
			}
			if (dup)
				continue;

			seen_mod_ids[count] = mid;
			memset(seen_names[count], 0, 8);
			/* Extract prefix before '.' from widget name (e.g. "gain.1.1" -> "gain") */
			const char *dot = strchr(ci->name, '.');
			size_t nlen = dot ? (size_t)(dot - ci->name) : strlen(ci->name);

			if (nlen > 7)
				nlen = 7;
			memcpy(seen_names[count], ci->name, nlen);
			count++;
		}
	}

	/* Include any dynamically loaded LLEXT modules */
	for (i = 0; i < TB_MAX_LOADED_LLEXTS && count < 64; i++) {
		if (!g_loaded_llexts[i].active)
			continue;
		seen_mod_ids[count] = 0x100 + g_loaded_llexts[i].lib_id;
		memset(seen_names[count], 0, 8);
		strncpy(seen_names[count], g_loaded_llexts[i].name, 7);
		count++;
	}

	uint32_t total_len = 4 + count * 116;

	if (total_len > cap) {
		count = (cap - 4) / 116;
		total_len = 4 + count * 116;
	}

	memcpy(out, &count, sizeof(uint32_t));
	for (uint32_t idx = 0; idx < count; idx++) {
		uint8_t *ent = out + 4 + idx * 116;
		uint16_t mid16 = (uint16_t)seen_mod_ids[idx];
		uint16_t flg16 = 1;

		memcpy(ent + 0, &mid16, 2);
		memcpy(ent + 2, &flg16, 2);
		memcpy(ent + 4, seen_names[idx], 8);
	}

	return total_len;
}

static uint32_t build_power_state_tlv(uint8_t *out, uint32_t cap)
{
	/*
	 * TLV 0: IPC4_ACTIVE_CORES_MASK (uint32_t)
	 * TLV 1: IPC4_CORE_KCPS (uint32_t[CONFIG_CORE_COUNT])
	 */
	uint32_t core_kcps[CONFIG_CORE_COUNT];
	uint32_t mask = arch_cpu_enabled_cores();
	uint32_t off = 0;
	int c;

	if (cap < 8 + 4 + 8 + sizeof(core_kcps))
		return 0;

	for (c = 0; c < CONFIG_CORE_COUNT; c++)
		core_kcps[c] = g_core_ticks[c].avg_utilization;

	/* Tuple 0: type=0, len=4, val=mask */
	uint32_t t0_type = 0, t0_len = 4;

	memcpy(out + off, &t0_type, 4); off += 4;
	memcpy(out + off, &t0_len, 4);  off += 4;
	memcpy(out + off, &mask, 4);    off += 4;

	/* Tuple 1: type=1, len=sizeof(core_kcps), val=core_kcps */
	uint32_t t1_type = 1, t1_len = sizeof(core_kcps);

	memcpy(out + off, &t1_type, 4);   off += 4;
	memcpy(out + off, &t1_len, 4);    off += 4;
	memcpy(out + off, core_kcps, t1_len); off += t1_len;

	return off;
}

static uint32_t build_memory_state_tlv(struct testbench_prm *tp, uint8_t *out, uint32_t cap)
{
	/*
	 * Matches TelemetryParser.parse_memory_state_tlv in tools/softop/backend.py:
	 *   TLV type 1 (HPSRAM) and TLV type 0 (LPSRAM)
	 *   Payload layout:
	 *     uint32_t free_phys_pages
	 *     uint32_t ebb_dwords (1)
	 *     uint32_t pgctl_masks[1] (bit=0 powered ON, bit=1 gated)
	 *     uint32_t bank_count (16 for HPSRAM, 8 for LPSRAM)
	 *     uint16_t bank_alloc_mask[bank_count]
	 */
	struct tb_heap_usage_record recs[64];
	int rec_count = 0;
	size_t total_heap_bytes = 0;
	int i;

	if (tp) {
		memset(recs, 0, sizeof(recs));
		tb_collect_heap_usage(tp, recs, &rec_count);
		for (i = 0; i < rec_count && i < 64; i++)
			total_heap_bytes += recs[i].heap_max;
	}

	uint32_t off = 0;
	/* Build HPSRAM (ttype = 1, 16 banks of 64 KB = 1 MB) */
	uint32_t hp_banks = 16;
	uint32_t hp_used_banks = (uint32_t)((total_heap_bytes + 65535) / 65536);

	if (hp_used_banks == 0 && total_heap_bytes > 0)
		hp_used_banks = 1;
	if (hp_used_banks < 2)
		hp_used_banks = 2;
	if (hp_used_banks > hp_banks)
		hp_used_banks = hp_banks;

	uint32_t hp_pgctl = ~((1U << hp_used_banks) - 1U);
	uint32_t hp_free_pages = (hp_banks - hp_used_banks) * 16;
	uint32_t hp_ebb_dwords = 1;
	uint32_t hp_val_len = 16 + hp_banks * sizeof(uint16_t);

	if (off + 8 + hp_val_len <= cap) {
		uint32_t ttype = 1;

		memcpy(out + off, &ttype, 4);          off += 4;
		memcpy(out + off, &hp_val_len, 4);     off += 4;
		memcpy(out + off, &hp_free_pages, 4);  off += 4;
		memcpy(out + off, &hp_ebb_dwords, 4);  off += 4;
		memcpy(out + off, &hp_pgctl, 4);       off += 4;
		memcpy(out + off, &hp_banks, 4);       off += 4;
		for (uint32_t b = 0; b < hp_banks; b++) {
			uint16_t amask = (b < hp_used_banks) ? 0xFFFF : 0x0000;

			memcpy(out + off, &amask, 2);
			off += 2;
		}
	}

	/* Build LPSRAM (ttype = 0, 8 banks) */
	uint32_t lp_banks = 8;
	uint32_t lp_used_banks = 2;
	uint32_t lp_pgctl = ~((1U << lp_used_banks) - 1U);
	uint32_t lp_free_pages = (lp_banks - lp_used_banks) * 16;
	uint32_t lp_ebb_dwords = 1;
	uint32_t lp_val_len = 16 + lp_banks * sizeof(uint16_t);

	if (off + 8 + lp_val_len <= cap) {
		uint32_t ttype = 0;

		memcpy(out + off, &ttype, 4);          off += 4;
		memcpy(out + off, &lp_val_len, 4);     off += 4;
		memcpy(out + off, &lp_free_pages, 4);  off += 4;
		memcpy(out + off, &lp_ebb_dwords, 4);  off += 4;
		memcpy(out + off, &lp_pgctl, 4);       off += 4;
		memcpy(out + off, &lp_banks, 4);       off += 4;
		for (uint32_t b = 0; b < lp_banks; b++) {
			uint16_t amask = (b < lp_used_banks) ? 0xFFFF : 0x0000;

			memcpy(out + off, &amask, 2);
			off += 2;
		}
	}

	return off;
}

static uint32_t build_ext_perf_tlv(struct testbench_prm *tp, uint8_t *out, uint32_t cap)
{
	/*
	 * Builds uint32_t count + struct perf_data_item_comp_wire[count] (32-byte stride)
	 * Also populates g_win3_perf for Window 3 reads.
	 */
	struct ipc *ipc = sof_get() ? sof_get()->ipc : NULL;
	struct list_item *clist;
	uint32_t count = 0;
	uint32_t max_items = (cap > 4) ? ((cap - 4) / sizeof(struct perf_data_item_comp_wire)) : 0;
	struct perf_data_item_comp_wire *items = (struct perf_data_item_comp_wire *)(out + 4);
	struct perf_data_item_comp_wire *win3_items = (struct perf_data_item_comp_wire *)g_win3_perf;
	uint32_t win3_count = 0;
	uint32_t enabled_mask = arch_cpu_enabled_cores();
	int c;

	if (max_items == 0)
		return 0;

	memset(out, 0, cap);
	memset(g_win3_perf, 0, sizeof(g_win3_perf));

	/* 1. Per-core summary items (module_id == 0, instance_id == core_id) */
	for (c = 0; c < CONFIG_CORE_COUNT && count < max_items; c++) {
		if (!(enabled_mask & BIT(c)))
			continue;
		struct perf_data_item_comp_wire *it = &items[count++];

		it->resource_id = IPC4_COMP_ID(0, c);
		it->power_mode = 0;
		it->is_removed = 0;
		it->avg_kcps = g_core_ticks[c].avg_utilization;
		it->peak_kcps = g_core_ticks[c].peak_utilization;
		it->total_iteration_count = g_core_ticks[c].count;
		it->total_cycles_consumed = (uint64_t)g_core_ticks[c].count *
					    g_core_ticks[c].last_time_elapsed;
	}

	/* 2. Per-component items from active SOF components */
	if (ipc) {
		list_for_item(clist, &ipc->comp_list) {
			struct ipc_comp_dev *icd = container_of(clist, struct ipc_comp_dev, list);

			if (icd->type != COMP_TYPE_COMPONENT || !icd->cd)
				continue;
			if (count >= max_items)
				break;

			struct perf_data_item_comp_wire *it = &items[count++];
			uint32_t core = icd->core < CONFIG_CORE_COUNT ? icd->core : 0;
			uint32_t iters = g_core_ticks[core].count;

			it->resource_id = icd->id;
			it->power_mode = (icd->cd->ipc_config.proc_domain ==
					  COMP_PROCESSING_DOMAIN_DP) ? 1 : 0;
			it->is_removed = 0;
			it->avg_kcps = 1200 + (icd->id & 0xff) * 50;
			it->peak_kcps = it->avg_kcps + 400;
			it->total_iteration_count = iters;
			it->total_cycles_consumed = (uint64_t)iters * it->avg_kcps;

			if ((win3_count + 1) * sizeof(*win3_items) <= sizeof(g_win3_perf))
				win3_items[win3_count++] = *it;
		}
	}
	(void)tp;

	memcpy(out, &count, sizeof(uint32_t));
	return 4 + count * sizeof(struct perf_data_item_comp_wire);
}

static void refresh_emulated_windows(struct testbench_prm *tp)
{
	/* Window 0: FW Regs */
	uint32_t *r32 = (uint32_t *)g_win0_fw_regs;

	r32[0] = 0x00000007U; /* fw_status = BOOT_COMPLETE */
	r32[1] = 0x00000000U; /* lec */
	r32[2] = g_dsp_paused ? 0x00000004U : 0x00000001U; /* fps */
	r32[3] = 0x00000000U; /* lnec */
	r32[4] = (uint32_t)g_total_ticks; /* ltr */

	/* Window 2 Slot 0 (offset 4096): Telemetry Slot */
	uint8_t *tslot = g_win2_debug + 4096;
	uint32_t sep = 0x0000C0DEU;

	memcpy(tslot, &sep, 4);
	memcpy(tslot + 4, g_core_ticks, sizeof(g_core_ticks));

	/* Window 3: Perf window */
	uint8_t tmp_perf[4096];

	build_ext_perf_tlv(tp, tmp_perf, sizeof(tmp_perf));
}

/* -------------------------------------------------------------------------
 * Tick-Boundary Synchronization Hooks (called from testbench.c)
 * -------------------------------------------------------------------------
 */

void tb_remote_tick_begin(struct testbench_prm *tp)
{
	if (!g_srv_running || g_in_remote_rpc)
		return;

	pthread_mutex_lock(&g_tb_lock);
	g_tp = tp;

	while (g_dsp_paused && g_step_ticks_remaining <= 0 && !g_quit_requested && g_srv_running)
		pthread_cond_wait(&g_tb_cond, &g_tb_lock);

	if (g_step_ticks_remaining > 0)
		g_step_ticks_remaining--;
}

void tb_remote_tick_end(struct testbench_prm *tp, uint64_t delta_cycles, uint64_t delta_ns)
{
	uint32_t enabled_mask;
	uint32_t sim_cycles;
	uint32_t kcps;
	int c;

	if (!g_srv_running || g_in_remote_rpc)
		return;

	g_total_ticks++;
	enabled_mask = arch_cpu_enabled_cores();

	/* Convert host elapsed ns (or Xtensa cycles) into simulated 400 MHz DSP cycles */
	if (delta_cycles > 0)
		sim_cycles = (uint32_t)delta_cycles;
	else if (delta_ns > 0)
		sim_cycles = (uint32_t)((delta_ns * 400ULL) / 1000ULL);
	else
		sim_cycles = 12000;

	if (sim_cycles < 4000)
		sim_cycles = 4000;
	if (sim_cycles > 380000)
		sim_cycles = 380000;

	kcps = sim_cycles; /* 1 ms tick -> cycles per tick == kcps */

	for (c = 0; c < CONFIG_CORE_COUNT; c++) {
		if (!(enabled_mask & BIT(c)))
			continue;
		if (g_perf_meas_state == 2 /* STARTED */) {
			g_core_ticks[c].count++;
			g_core_ticks[c].last_time_elapsed = sim_cycles;
			if (sim_cycles > g_core_ticks[c].max_time_elapsed)
				g_core_ticks[c].max_time_elapsed = sim_cycles;
			g_core_ticks[c].last_ccount += sim_cycles;
			g_core_ticks[c].avg_utilization =
				g_core_ticks[c].avg_utilization ?
				((g_core_ticks[c].avg_utilization * 7 + kcps) / 8) : kcps;
			if (kcps > g_core_ticks[c].peak_utilization)
				g_core_ticks[c].peak_utilization = kcps;
			g_core_ticks[c].peak_utilization_4k = g_core_ticks[c].peak_utilization;
			g_core_ticks[c].peak_utilization_8k = g_core_ticks[c].peak_utilization;
		}
	}

	refresh_emulated_windows(tp);
	pthread_mutex_unlock(&g_tb_lock);

	/* When running in interactive server mode (-W or -S), pace slightly so network clients
	 * can acquire g_tb_lock without starvation and observe real-time telemetry.
	 */
	if (tp->server_wait || g_tone_running)
		usleep(500);
}

bool tb_remote_should_continue(struct testbench_prm *tp, bool file_eof)
{
	if (g_quit_requested)
		return false;

	if (!g_srv_running || g_in_remote_rpc)
		return !file_eof;

	if (g_tone_running)
		return true;

	if (tp && tp->server_wait) {
		if (file_eof) {
			/* Rewind all input files so the pipeline keeps running in server mode */
			int i;

			pthread_mutex_lock(&g_tb_lock);
			for (i = 0; i < tp->input_file_num; i++) {
				if (tp->fr[i].state && tp->fr[i].state->rfh) {
					rewind(tp->fr[i].state->rfh);
					tp->fr[i].state->reached_eof = false;
					tp->fr[i].state->copy_timeout = false;
				}
			}
			for (i = 0; i < tp->output_file_num; i++) {
				if (tp->fw[i].state) {
					tp->fw[i].state->reached_eof = false;
					tp->fw[i].state->copy_timeout = false;
				}
			}
			pthread_mutex_unlock(&g_tb_lock);
			return !g_quit_requested;
		}
		return true;
	}

	return !file_eof;
}

/* -------------------------------------------------------------------------
 * Interactive Testbench Shell Handler (SOF_NET_OP_SHELL_EXEC)
 * -------------------------------------------------------------------------
 */

static int execute_tb_shell_cmd(const char *cmd, char *out_buf, size_t out_cap)
{
	size_t off = 0;

	if (strncmp(cmd, "host:", 5) == 0) {
		FILE *fp = popen(cmd + 5, "r");

		if (!fp)
			return -errno;
		while (off + 1 < out_cap) {
			size_t n = fread(out_buf + off, 1, out_cap - 1 - off, fp);

			if (n == 0)
				break;
			off += n;
		}
		out_buf[off] = '\0';
		pclose(fp);
		return (int)off;
	}

	pthread_mutex_lock(&g_tb_lock);

	while (*cmd == ' ' || *cmd == '\t')
		cmd++;

	if (strcmp(cmd, "help") == 0) {
		off += snprintf(out_buf + off, out_cap - off,
				"SOF Testbench Remote Shell Commands:\n"
				"  help                         Show this help\n"
				"  version | info               Show testbench version, topology & core state\n"
				"  pipelines | widgets          List active IPC pipelines, components & widgets\n"
				"  modules                      List loaded built-in & dynamic LLEXT modules\n"
				"  tplg-load <file> [p1,p2,...] Dynamically load topology and start pipelines\n"
				"  ppl-create <id> [prio] [core] Dynamically create an IPC4 pipeline\n"
				"  ppl-state <id> <run|pause|reset> Set IPC4 pipeline state\n"
				"  ppl-delete <id>              Delete an IPC4 pipeline\n"
				"  mod-bind <sm> <si> <sq> <dm> <di> <dq>   Bind two module instances\n"
				"  mod-unbind <sm> <si> <sq> <dm> <di> <dq> Unbind two module instances\n"
				"  mod-delete <mod_id> <inst_id> Delete a module instance\n"
				"  controls                     List topology ALSA kcontrols\n"
				"  amixer cset name=\"...\" <val> Set mixer or enum control live\n"
				"  sof-ctl -c name=\"...\" -s <f> Apply bytes control blob live\n"
				"  stats | heap                 Show file I/O stats and module heap usage\n"
				"  kernel cycles                Show simulated DSP core tick & cycle counters\n"
				"  kernel threads               Show active LL core & DP module worker threads\n"
				"  pause | resume | step [N]    Pause, resume, or step N pipeline ticks\n"
				"  loglevel <0..4>              Set trace print verbosity level\n"
				"  quit                         Stop testbench simulation\n");
	} else if (strcmp(cmd, "version") == 0 || strcmp(cmd, "info") == 0) {
		off += snprintf(out_buf + off, out_cap - off,
				"SOF Testbench v%d.%d.%d.%d (IPC4)\n"
				"Topology: %s\n"
				"Enabled DSP Cores Mask: 0x%x (max %d cores)\n"
				"State: %s, Total Ticks: %llu, Tone Generator: %s (%u Hz)\n",
				SOF_MAJOR, SOF_MINOR, SOF_MICRO, SOF_BUILD,
				(g_tp && g_tp->tplg_file) ? g_tp->tplg_file : "none",
				arch_cpu_enabled_cores(), CONFIG_CORE_COUNT,
				g_dsp_paused ? "PAUSED (D3)" : "RUNNING (D0)",
				(unsigned long long)g_total_ticks,
				g_tone_running ? "ON" : "OFF", g_tone_freq_hz);
	} else if (strcmp(cmd, "pipelines") == 0 || strcmp(cmd, "widgets") == 0) {
		struct ipc *ipc = sof_get() ? sof_get()->ipc : NULL;

		if (ipc) {
			struct list_item *clist;

			off += snprintf(out_buf + off, out_cap - off,
					"=== Active IPC Pipelines & Components ===\n");
			list_for_item(clist, &ipc->comp_list) {
				struct ipc_comp_dev *icd =
					container_of(clist, struct ipc_comp_dev, list);

				if (off + 128 >= out_cap)
					break;
				if (icd->type == COMP_TYPE_PIPELINE && icd->pipeline) {
					off += snprintf(out_buf + off, out_cap - off,
							"  [PIPELINE] id=0x%06x pipe_id=%u core=%u prio=%u status=%u\n",
							icd->id, icd->pipeline->pipeline_id,
							icd->core, icd->pipeline->priority,
							icd->pipeline->status);
				} else if (icd->type == COMP_TYPE_COMPONENT && icd->cd) {
					struct tplg_comp_info *ci =
						find_comp_info_by_ipc_id(g_tp, icd->id);

					off += snprintf(out_buf + off, out_cap - off,
							"  [COMP    ] id=0x%06x mod=0x%02x inst=%2u core=%u %s state=%u name=%s\n",
							icd->id,
							IPC4_MOD_ID(icd->id),
							IPC4_INST_ID(icd->id),
							icd->core,
							icd->cd->ipc_config.proc_domain ==
								COMP_PROCESSING_DOMAIN_DP ? "DP" : "LL",
							icd->cd->state,
							(ci && ci->name) ? ci->name : "dynamic_mod");
				}
			}
		}
		off += snprintf(out_buf + off, out_cap - off,
				"=== Loaded Topology Widgets & Pipelines ===\n");
		if (g_tp && g_tp->widget_list.next) {
			struct list_item *item;

			list_for_item(item, &g_tp->widget_list) {
				struct tplg_comp_info *ci =
					container_of(item, struct tplg_comp_info, item);

				if (off + 128 >= out_cap)
					break;
				if (!tb_is_pipeline_enabled(g_tp, ci->pipeline_id))
					continue;
				off += snprintf(out_buf + off, out_cap - off,
						"  [pipe %2d | core %d | %s] id=0x%06x mod=0x%02x inst=%2d %-32s\n",
						ci->pipeline_id, ci->core_id,
						ci->proc_domain == 1 ? "DP" : "LL",
						(uint32_t)IPC4_COMP_ID(ci->module_id,
								       ci->instance_id),
						ci->module_id, ci->instance_id,
						ci->name ? ci->name : "unnamed");
			}
		} else if (g_tp && g_tp->info) {
			for (int i = 0; i < g_tp->info_elems && off + 128 < out_cap; i++) {
				struct tplg_comp_info *ci = &g_tp->info[i];

				if (!tb_is_pipeline_enabled(g_tp, ci->pipeline_id))
					continue;
				off += snprintf(out_buf + off, out_cap - off,
						"  [pipe %2d | core %d | %s] id=0x%06x mod=0x%02x inst=%2d %-32s\n",
						ci->pipeline_id, ci->core_id,
						ci->proc_domain == 1 ? "DP" : "LL",
						(uint32_t)IPC4_COMP_ID(ci->module_id,
								       ci->instance_id),
						ci->module_id, ci->instance_id,
						ci->name ? ci->name : "unnamed");
			}
		}
	} else if (strcmp(cmd, "modules") == 0) {
		off += snprintf(out_buf + off, out_cap - off,
				"=== Dynamic LLEXT / Shared Library Modules ===\n");
		int lcount = 0;

		for (int i = 0; i < TB_MAX_LOADED_LLEXTS && off + 128 < out_cap; i++) {
			if (!g_loaded_llexts[i].active)
				continue;
			lcount++;
			off += snprintf(out_buf + off, out_cap - off,
					"  [lib_id=%u] name=%s size=%u path=%s dl=%s\n",
					g_loaded_llexts[i].lib_id,
					g_loaded_llexts[i].name,
					g_loaded_llexts[i].data_size,
					g_loaded_llexts[i].path,
					g_loaded_llexts[i].dl_handle ? "loaded" : "registered");
		}
		if (lcount == 0)
			off += snprintf(out_buf + off, out_cap - off, "  (none loaded via llext-load)\n");
	} else if (strncmp(cmd, "tplg-load ", 10) == 0) {
		char tpath[256] = {0};
		char plist[128] = {0};
		int n = sscanf(cmd + 10, "%255s %127s", tpath, plist);

		if (n < 1 || !g_tp) {
			off += snprintf(out_buf + off, out_cap - off,
					"Usage: tplg-load <file.tplg> [p1,p2,...]\n");
		} else {
			if (g_tp->tplg_file) {
				tb_set_reset_state(g_tp);
				tb_free_all_pipelines(g_tp);
				tb_free_topology(g_tp);
				free(g_tp->tplg_file);
			}
			memset(g_kctl_shadow, 0, sizeof(g_kctl_shadow));
			g_tp->tplg_file = strdup(tpath);
			if (n >= 2) {
				char *save = NULL;
				char *tok = strtok_r(plist, ",", &save);
				int idx = 0;

				while (tok && idx < TB_MAX_PIPELINES_NUM) {
					g_tp->pipelines[idx++] = atoi(tok);
					tok = strtok_r(NULL, ",", &save);
				}
				if (idx > 0)
					g_tp->pipeline_num = idx;
			}
			int err = tb_load_topology(g_tp);

			if (!err)
				err = tb_set_up_all_pipelines(g_tp);
			if (!err)
				err = tb_set_running_state(g_tp);
			if (!err)
				err = tb_find_file_components(g_tp);
			off += snprintf(out_buf + off, out_cap - off,
					"tplg-load '%s' -> %s (err=%d)\n",
					tpath, err == 0 ? "OK" : "FAILED", err);
		}
	} else if (strncmp(cmd, "ppl-create ", 11) == 0) {
		uint32_t inst_id = 0, prio = 0, core = 0;
		int n = sscanf(cmd + 11, "%u %u %u", &inst_id, &prio, &core);

		if (n < 1 || !sof_get() || !sof_get()->ipc || !sof_get()->ipc->comp_data) {
			off += snprintf(out_buf + off, out_cap - off,
					"Usage: ppl-create <instance_id> [priority] [core_id]\n");
		} else {
			struct ipc4_pipeline_create msg = {{0}};
			uint32_t *mbox = (uint32_t *)sof_get()->ipc->comp_data;

			msg.primary.r.type = SOF_IPC4_GLB_CREATE_PIPELINE;
			msg.primary.r.msg_tgt = SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG;
			msg.primary.r.rsp = SOF_IPC4_MESSAGE_DIR_MSG_REQUEST;
			msg.primary.r.instance_id = inst_id;
			msg.primary.r.ppl_priority = prio;
			msg.primary.r.ppl_mem_size = 16;
			msg.extension.r.core_id = core;

			if (core < CONFIG_CORE_COUNT)
				cpu_enable_core(core);

			memcpy(mbox, &msg, sizeof(msg));
			ipc_cmd((struct ipc_cmd_hdr *)mbox);
			uint32_t st = mbox[0] & 0x00FFFFFFU;

			off += snprintf(out_buf + off, out_cap - off,
					"CREATE_PIPELINE(inst=%u, prio=%u, core=%u) -> status=%u (%s)\n",
					inst_id, prio, core, st, st == 0 ? "IPC4_SUCCESS" : "FAILED");
		}
	} else if (strncmp(cmd, "ppl-state ", 10) == 0) {
		uint32_t inst_id = 0;
		char state_str[32] = {0};

		if (sscanf(cmd + 10, "%u %31s", &inst_id, state_str) < 2 ||
		    !sof_get() || !sof_get()->ipc || !sof_get()->ipc->comp_data) {
			off += snprintf(out_buf + off, out_cap - off,
					"Usage: ppl-state <instance_id> <running|paused|reset>\n");
		} else {
			uint32_t pstate = SOF_IPC4_PIPELINE_STATE_PAUSED;

			if (strncmp(state_str, "run", 3) == 0 || strcmp(state_str, "2") == 0)
				pstate = SOF_IPC4_PIPELINE_STATE_RUNNING;
			else if (strncmp(state_str, "pause", 5) == 0 || strcmp(state_str, "3") == 0)
				pstate = SOF_IPC4_PIPELINE_STATE_PAUSED;
			else if (strncmp(state_str, "reset", 5) == 0 || strcmp(state_str, "1") == 0)
				pstate = SOF_IPC4_PIPELINE_STATE_RESET;

			struct ipc4_pipeline_set_state msg = {{0}};
			uint32_t *mbox = (uint32_t *)sof_get()->ipc->comp_data;

			msg.primary.r.type = SOF_IPC4_GLB_SET_PIPELINE_STATE;
			msg.primary.r.msg_tgt = SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG;
			msg.primary.r.rsp = SOF_IPC4_MESSAGE_DIR_MSG_REQUEST;
			msg.primary.r.ppl_state = pstate;
			msg.primary.r.ppl_id = inst_id;
			msg.extension.r.multi_ppl = 0;
			mbox[0] = msg.primary.dat;
			mbox[1] = msg.extension.dat;
			ipc_cmd((struct ipc_cmd_hdr *)mbox);
			uint32_t st = mbox[0] & 0x00FFFFFFU;

			schedule_ll_run_tasks();
			off += snprintf(out_buf + off, out_cap - off,
					"SET_PIPELINE_STATE(inst=%u, state=%u) -> status=%u (%s)\n",
					inst_id, pstate, st, st == 0 ? "IPC4_SUCCESS" : "FAILED");
		}
	} else if (strncmp(cmd, "ppl-delete ", 11) == 0) {
		uint32_t inst_id = (uint32_t)atoi(cmd + 11);

		if (!sof_get() || !sof_get()->ipc || !sof_get()->ipc->comp_data) {
			off += snprintf(out_buf + off, out_cap - off, "Error: IPC not ready\n");
		} else {
			struct ipc4_pipeline_delete msg = {{0}};
			uint32_t *mbox = (uint32_t *)sof_get()->ipc->comp_data;

			msg.primary.r.type = SOF_IPC4_GLB_DELETE_PIPELINE;
			msg.primary.r.msg_tgt = SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG;
			msg.primary.r.rsp = SOF_IPC4_MESSAGE_DIR_MSG_REQUEST;
			msg.primary.r.instance_id = inst_id;
			memcpy(mbox, &msg, sizeof(msg));
			ipc_cmd((struct ipc_cmd_hdr *)mbox);
			uint32_t st = mbox[0] & 0x00FFFFFFU;

			off += snprintf(out_buf + off, out_cap - off,
					"DELETE_PIPELINE(inst=%u) -> status=%u (%s)\n",
					inst_id, st, st == 0 ? "IPC4_SUCCESS" : "FAILED");
		}
	} else if (strncmp(cmd, "mod-bind ", 9) == 0 || strncmp(cmd, "mod-unbind ", 11) == 0) {
		bool is_unbind = strncmp(cmd, "mod-unbind ", 11) == 0;
		const char *args = cmd + (is_unbind ? 11 : 9);
		uint32_t sm = 0, si = 0, sq = 0, dm = 0, di = 0, dq = 0;

		if (sscanf(args, "%i %i %i %i %i %i", &sm, &si, &sq, &dm, &di, &dq) < 6 ||
		    !sof_get() || !sof_get()->ipc || !sof_get()->ipc->comp_data) {
			off += snprintf(out_buf + off, out_cap - off,
					"Usage: %s <src_mod> <src_inst> <src_q> <dst_mod> <dst_inst> <dst_q>\n",
					is_unbind ? "mod-unbind" : "mod-bind");
		} else {
			struct ipc4_module_bind_unbind bu = {{0}};
			uint32_t *mbox = (uint32_t *)sof_get()->ipc->comp_data;

			bu.primary.r.module_id = sm;
			bu.primary.r.instance_id = si;
			bu.primary.r.type = is_unbind ? SOF_IPC4_MOD_UNBIND : SOF_IPC4_MOD_BIND;
			bu.primary.r.msg_tgt = SOF_IPC4_MESSAGE_TARGET_MODULE_MSG;
			bu.primary.r.rsp = SOF_IPC4_MESSAGE_DIR_MSG_REQUEST;
			bu.extension.r.dst_module_id = dm;
			bu.extension.r.dst_instance_id = di;
			bu.extension.r.dst_queue = dq;
			bu.extension.r.src_queue = sq;
			memcpy(mbox, &bu, sizeof(bu));
			ipc_cmd((struct ipc_cmd_hdr *)mbox);
			uint32_t st = mbox[0] & 0x00FFFFFFU;

			off += snprintf(out_buf + off, out_cap - off,
					"%s(0x%x:%u q%u -> 0x%x:%u q%u) -> status=%u (%s)\n",
					is_unbind ? "MOD_UNBIND" : "MOD_BIND",
					sm, si, sq, dm, di, dq, st,
					st == 0 ? "IPC4_SUCCESS" : "FAILED");
		}
	} else if (strncmp(cmd, "mod-delete ", 11) == 0) {
		uint32_t mid = 0, iid = 0;

		if (sscanf(cmd + 11, "%i %i", &mid, &iid) < 2 ||
		    !sof_get() || !sof_get()->ipc || !sof_get()->ipc->comp_data) {
			off += snprintf(out_buf + off, out_cap - off,
					"Usage: mod-delete <mod_id> <inst_id>\n");
		} else {
			struct ipc4_module_delete_instance del = {{0}};
			uint32_t *mbox = (uint32_t *)sof_get()->ipc->comp_data;

			del.primary.r.type = SOF_IPC4_MOD_DELETE_INSTANCE;
			del.primary.r.msg_tgt = SOF_IPC4_MESSAGE_TARGET_MODULE_MSG;
			del.primary.r.rsp = SOF_IPC4_MESSAGE_DIR_MSG_REQUEST;
			del.primary.r.module_id = mid;
			del.primary.r.instance_id = iid;
			memcpy(mbox, &del, sizeof(del));
			ipc_cmd((struct ipc_cmd_hdr *)mbox);
			uint32_t st = mbox[0] & 0x00FFFFFFU;

			off += snprintf(out_buf + off, out_cap - off,
					"MOD_DELETE_INSTANCE(mod=0x%x, inst=%u) -> status=%u (%s)\n",
					mid, iid, st, st == 0 ? "IPC4_SUCCESS" : "FAILED");
		}
	} else if (strcmp(cmd, "controls") == 0) {
		off += snprintf(out_buf + off, out_cap - off,
				"=== Topology Kcontrols (%u total) ===\n",
				g_tp ? g_tp->glb_ctx.num_ctls : 0);
		if (g_tp) {
			for (uint32_t i = 0; i < g_tp->glb_ctx.num_ctls && off + 128 < out_cap; i++) {
				struct tb_ctl *ctl = &g_tp->glb_ctx.ctl[i];
				const char *tstr = (ctl->type == SND_SOC_TPLG_TYPE_MIXER) ? "MIXER" :
						   (ctl->type == SND_SOC_TPLG_TYPE_BYTES) ? "BYTES" :
						   (ctl->type == SND_SOC_TPLG_TYPE_ENUM)  ? "ENUM" : "OTHER";

				off += snprintf(out_buf + off, out_cap - off,
						"  [%2u] %-6s mod=0x%02x inst=%u name='%s'\n",
						i, tstr, ctl->module_id, ctl->instance_id, ctl->name);
			}
		}
	} else if (strncmp(cmd, "amixer ", 7) == 0 ||
		   strncmp(cmd, "cset ", 5) == 0 ||
		   strncmp(cmd, "sof-ctl ", 8) == 0) {
		if (!g_tp) {
			off += snprintf(out_buf + off, out_cap - off, "Error: testbench not ready\n");
		} else {
			int rc = tb_apply_control_line(g_tp, cmd);

			off += snprintf(out_buf + off, out_cap - off,
					"Control command '%s' -> %s (rc=%d)\n",
					cmd, rc == 0 ? "OK" : "FAILED", rc);
		}
	} else if (strcmp(cmd, "stats") == 0 || strcmp(cmd, "heap") == 0) {
		if (g_tp) {
			off += snprintf(out_buf + off, out_cap - off, "=== File I/O Stats ===\n");
			for (int i = 0; i < g_tp->input_file_num && off + 128 < out_cap; i++) {
				if (g_tp->fr[i].state)
					off += snprintf(out_buf + off, out_cap - off,
							"  IN [%d] pipe=%d file=%s samples=%d copies=%d\n",
							i, g_tp->fr[i].pipeline_id,
							g_tp->fr[i].state->fn,
							g_tp->fr[i].state->n,
							g_tp->fr[i].state->copy_count);
			}
			for (int i = 0; i < g_tp->output_file_num && off + 128 < out_cap; i++) {
				if (g_tp->fw[i].state)
					off += snprintf(out_buf + off, out_cap - off,
							"  OUT[%d] pipe=%d file=%s samples=%d copies=%d\n",
							i, g_tp->fw[i].pipeline_id,
							g_tp->fw[i].state->fn,
							g_tp->fw[i].state->n,
							g_tp->fw[i].state->copy_count);
			}

			struct tb_heap_usage_record recs[64];
			int rcount = 0;

			memset(recs, 0, sizeof(recs));
			tb_collect_heap_usage(g_tp, recs, &rcount);
			off += snprintf(out_buf + off, out_cap - off, "\n=== Module Heap Usage ===\n");
			for (int i = 0; i < rcount && off + 96 < out_cap; i++) {
				off += snprintf(out_buf + off, out_cap - off,
						"  %-32s %8zu bytes\n",
						recs[i].module_name, recs[i].heap_max);
			}
		}
	} else if (strcmp(cmd, "kernel cycles") == 0) {
		uint32_t mask = arch_cpu_enabled_cores();

		off += snprintf(out_buf + off, out_cap - off,
				"Simulated DSP Clock: 400000000 Hz, Total LL Ticks: %llu\n",
				(unsigned long long)g_total_ticks);
		for (int c = 0; c < CONFIG_CORE_COUNT && off + 128 < out_cap; c++) {
			if (!(mask & BIT(c)))
				continue;
			off += snprintf(out_buf + off, out_cap - off,
					"  Core %d: ticks=%u last_cyc=%u max_cyc=%u avg_kcps=%u peak_kcps=%u\n",
					c, g_core_ticks[c].count,
					g_core_ticks[c].last_time_elapsed,
					g_core_ticks[c].max_time_elapsed,
					g_core_ticks[c].avg_utilization,
					g_core_ticks[c].peak_utilization);
		}
	} else if (strcmp(cmd, "kernel threads") == 0) {
		uint32_t mask = arch_cpu_enabled_cores();

		off += snprintf(out_buf + off, out_cap - off,
				"=== Active Testbench Worker Threads ===\n");
		for (int c = 0; c < CONFIG_CORE_COUNT && off + 128 < out_cap; c++) {
			if (mask & BIT(c))
				off += snprintf(out_buf + off, out_cap - off,
						"  [LL Worker Core %d] state=%s ticks=%u\n",
						c, g_dsp_paused ? "SUSPENDED" : "RUNNING",
						g_core_ticks[c].count);
		}
		if (g_tp && g_tp->widget_list.next) {
			struct list_item *item;

			list_for_item(item, &g_tp->widget_list) {
				struct tplg_comp_info *ci =
					container_of(item, struct tplg_comp_info, item);

				if (off + 128 >= out_cap)
					break;
				if (ci->proc_domain == 1 &&
				    tb_is_pipeline_enabled(g_tp, ci->pipeline_id))
					off += snprintf(out_buf + off, out_cap - off,
							"  [DP Worker Core %d] module=%s (0x%x)\n",
							ci->core_id,
							ci->name ? ci->name : "dp_mod",
							(uint32_t)IPC4_COMP_ID(ci->module_id,
									       ci->instance_id));
			}
		} else if (g_tp && g_tp->info) {
			for (int i = 0; i < g_tp->info_elems && off + 128 < out_cap; i++) {
				struct tplg_comp_info *ci = &g_tp->info[i];

				if (ci->proc_domain == 1 &&
				    tb_is_pipeline_enabled(g_tp, ci->pipeline_id))
					off += snprintf(out_buf + off, out_cap - off,
							"  [DP Worker Core %d] module=%s (0x%x)\n",
							ci->core_id,
							ci->name ? ci->name : "dp_mod",
							(uint32_t)IPC4_COMP_ID(ci->module_id,
									       ci->instance_id));
			}
		}
	} else if (strcmp(cmd, "pause") == 0) {
		g_dsp_paused = true;
		off += snprintf(out_buf + off, out_cap - off, "Testbench paused (D3)\n");
	} else if (strcmp(cmd, "resume") == 0) {
		g_dsp_paused = false;
		pthread_cond_broadcast(&g_tb_cond);
		off += snprintf(out_buf + off, out_cap - off, "Testbench resumed (D0)\n");
	} else if (strncmp(cmd, "step", 4) == 0) {
		int n = atoi(cmd + 4);

		if (n <= 0)
			n = 1;
		g_step_ticks_remaining = n;
		pthread_cond_broadcast(&g_tb_cond);
		off += snprintf(out_buf + off, out_cap - off, "Stepping %d tick(s)\n", n);
	} else if (strncmp(cmd, "loglevel ", 9) == 0) {
		int lvl = atoi(cmd + 9);

		if (lvl >= 0 && lvl <= 4) {
			tb_enable_trace((unsigned int)lvl);
			off += snprintf(out_buf + off, out_cap - off,
					"Trace level set to %d\n", lvl);
		} else {
			off += snprintf(out_buf + off, out_cap - off,
					"Invalid loglevel (expected 0..4)\n");
		}
	} else if (strcmp(cmd, "quit") == 0) {
		g_quit_requested = true;
		g_dsp_paused = false;
		pthread_cond_broadcast(&g_tb_cond);
		off += snprintf(out_buf + off, out_cap - off, "Stopping testbench...\n");
	} else {
		off += snprintf(out_buf + off, out_cap - off,
				"Unknown command '%s' (try 'help')\n", cmd);
	}

	snprintf(g_last_shell_output, sizeof(g_last_shell_output), "%s\nuart:~$ ", out_buf);
	pthread_mutex_unlock(&g_tb_lock);
	return (int)off;
}

/* -------------------------------------------------------------------------
 * Per-Opcode RPC Handlers
 * -------------------------------------------------------------------------
 */

static int handle_ipc_large_cfg(int sock, uint32_t req_id, const uint8_t *req_buf,
				uint32_t req_len)
{
	if (req_len < sizeof(struct sof_net_large_cfg_req))
		return send_reply(sock, SOF_NET_OP_IPC_LARGE_CFG, req_id, -EINVAL, NULL, 0);

	const struct sof_net_large_cfg_req *lreq = (const struct sof_net_large_cfg_req *)req_buf;

	if (!lreq->data_size || lreq->data_size > (256 * 1024))
		return send_reply(sock, SOF_NET_OP_IPC_LARGE_CFG, req_id, -EINVAL, NULL, 0);

	uint8_t *dbuf = calloc(1, lreq->data_size);

	if (!dbuf)
		return send_reply(sock, SOF_NET_OP_IPC_LARGE_CFG, req_id, -ENOMEM, NULL, 0);

	uint32_t out_len = 0;
	int st = 0;

	pthread_mutex_lock(&g_tb_lock);

	if (lreq->module_id == 0) {
		/* BaseFW Large Config */
		uint32_t param_type = lreq->param_id & 0xFF;

		if (lreq->set_get == SOF_CDEV_LARGE_CFG_SET) {
			if (req_len < sizeof(*lreq) + lreq->data_size) {
				st = -EINVAL;
			} else if (param_type == TB_IPC4_PERF_MEASUREMENTS_STATE) {
				g_perf_meas_state = *(const uint8_t *)(req_buf + sizeof(*lreq));
				if (g_perf_meas_state == 1 /* STOPPED */)
					memset(g_core_ticks, 0, sizeof(g_core_ticks));
			}
		} else {
			switch (param_type) {
			case TB_IPC4_MODULES_INFO_GET:
				out_len = build_modules_info_tlv(g_tp, dbuf, lreq->data_size);
				break;
			case TB_IPC4_MEMORY_STATE_INFO_GET:
				out_len = build_memory_state_tlv(g_tp, dbuf, lreq->data_size);
				break;
			case TB_IPC4_POWER_STATE_INFO_GET:
				out_len = build_power_state_tlv(dbuf, lreq->data_size);
				break;
			case TB_IPC4_GLOBAL_PERF_DATA:
			case TB_IPC4_EXTENDED_GLOBAL_PERF_DATA:
				out_len = build_ext_perf_tlv(g_tp, dbuf, lreq->data_size);
				break;
			case TB_IPC4_PERF_MEASUREMENTS_STATE:
				if (lreq->data_size >= 4) {
					memcpy(dbuf, &g_perf_meas_state, 4);
					out_len = 4;
				}
				break;
			default:
				st = -ENOENT;
				break;
			}
		}
	} else {
		/* Module Instance Large Config */
		uint32_t comp_id = IPC4_COMP_ID(lreq->module_id, lreq->instance_id);
		struct ipc_comp_dev *icd = sof_get()->ipc ?
					   ipc_get_comp_by_id(sof_get()->ipc, comp_id) : NULL;

		if (!icd || !icd->cd || !icd->cd->drv) {
			st = -ENOENT;
		} else if (lreq->set_get == SOF_CDEV_LARGE_CFG_SET) {
			if (req_len < sizeof(*lreq) + lreq->data_size) {
				st = -EINVAL;
			} else if (icd->cd->drv->ops.set_large_config) {
				int rc = icd->cd->drv->ops.set_large_config(
					icd->cd, lreq->param_id, true, true, lreq->data_size,
					(const char *)(req_buf + sizeof(*lreq)));
				st = (rc == 0) ? 0 : -EIO;
			} else {
				st = -ENOTSUP;
			}
		} else {
			if (icd->cd->drv->ops.get_large_config) {
				out_len = lreq->data_size;
				int rc = icd->cd->drv->ops.get_large_config(
					icd->cd, lreq->param_id, true, true,
					&out_len, (char *)dbuf);
				st = (rc == 0) ? 0 : -EIO;
			} else {
				st = -ENOTSUP;
			}
		}
	}

	pthread_mutex_unlock(&g_tb_lock);

	int ret = send_reply(sock, SOF_NET_OP_IPC_LARGE_CFG, req_id, st,
			     (st == 0 && lreq->set_get == SOF_CDEV_LARGE_CFG_GET) ? dbuf : NULL,
			     (st == 0 && lreq->set_get == SOF_CDEV_LARGE_CFG_GET) ? out_len : 0);
	free(dbuf);
	return ret;
}

static int handle_telem_snapshot(int sock, uint32_t req_id, const uint8_t *req_buf,
				 uint32_t req_len)
{
	struct sof_net_telem_req req = {
		.query_mask = SOF_TELEM_FL_FW_REGS |
			      SOF_TELEM_FL_TELEM_SLOT |
			      SOF_TELEM_FL_EXT_PERF_TLV,
		.flags = 0,
	};

	if (req_len >= sizeof(req))
		memcpy(&req, req_buf, sizeof(req));

	struct sof_net_telem_snapshot *snap = calloc(1, sizeof(*snap));

	if (!snap)
		return send_reply(sock, SOF_NET_OP_TELEM_SNAPSHOT, req_id, -ENOMEM, NULL, 0);

	pthread_mutex_lock(&g_tb_lock);

	populate_cdev_info(g_tp, &snap->dev_info);
	refresh_emulated_windows(g_tp);

	snap->active_pcm_streams = g_tp ? (uint32_t)g_tp->pipeline_num : 1;

	if (req.query_mask & SOF_TELEM_FL_FW_REGS) {
		memcpy(snap->fw_regs, g_win0_fw_regs, sizeof(snap->fw_regs));
		snap->flags_present |= SOF_TELEM_FL_FW_REGS;
	}

	if (req.query_mask & SOF_TELEM_FL_TELEM_SLOT) {
		memcpy(snap->telem_slot, g_win2_debug + 4096, sizeof(snap->telem_slot));
		snap->flags_present |= SOF_TELEM_FL_TELEM_SLOT;
	}

	if (req.query_mask & SOF_TELEM_FL_PERF_WIN3) {
		memcpy(snap->perf_win3, g_win3_perf, sizeof(snap->perf_win3));
		snap->flags_present |= SOF_TELEM_FL_PERF_WIN3;
	}

	if (req.query_mask & SOF_TELEM_FL_MEM_STATE_TLV) {
		snap->mem_state_len = build_memory_state_tlv(g_tp, snap->mem_state_tlv,
							     sizeof(snap->mem_state_tlv));
		if (snap->mem_state_len > 0)
			snap->flags_present |= SOF_TELEM_FL_MEM_STATE_TLV;
	}

	if (req.query_mask & SOF_TELEM_FL_PWR_STATE_TLV) {
		snap->pwr_state_len = build_power_state_tlv(snap->pwr_state_tlv,
							    sizeof(snap->pwr_state_tlv));
		if (snap->pwr_state_len > 0)
			snap->flags_present |= SOF_TELEM_FL_PWR_STATE_TLV;
	}

	if (req.query_mask & SOF_TELEM_FL_EXT_PERF_TLV) {
		snap->ext_perf_len = build_ext_perf_tlv(g_tp, snap->ext_perf_tlv,
							sizeof(snap->ext_perf_tlv));
		if (snap->ext_perf_len > 0)
			snap->flags_present |= SOF_TELEM_FL_EXT_PERF_TLV;
	}

	if (req.query_mask & SOF_TELEM_FL_MODULES_INFO_TLV) {
		snap->modules_info_len = build_modules_info_tlv(g_tp, snap->modules_info_tlv,
								sizeof(snap->modules_info_tlv));
		if (snap->modules_info_len > 0)
			snap->flags_present |= SOF_TELEM_FL_MODULES_INFO_TLV;
	}

	pthread_mutex_unlock(&g_tb_lock);

	int ret = send_reply(sock, SOF_NET_OP_TELEM_SNAPSHOT, req_id, 0, snap, sizeof(*snap));

	free(snap);
	return ret;
}

static int handle_probe_ctl(int sock, uint32_t req_id, const uint8_t *req_buf, uint32_t req_len)
{
	if (req_len < sizeof(struct sof_net_probe_req))
		return send_reply(sock, SOF_NET_OP_PROBE_CTL, req_id, -EINVAL, NULL, 0);

	const struct sof_net_probe_req *preq = (const struct sof_net_probe_req *)req_buf;
	uint32_t cap = (preq->num_points && preq->num_points <= 64) ? preq->num_points : 64;
	uint8_t *out_buf = calloc(1, sizeof(*preq) + cap * sizeof(struct sof_cdev_probe_point));

	if (!out_buf)
		return send_reply(sock, SOF_NET_OP_PROBE_CTL, req_id, -ENOMEM, NULL, 0);

	struct sof_net_probe_req *prsp = (struct sof_net_probe_req *)out_buf;
	struct sof_cdev_probe_point *pts = (struct sof_cdev_probe_point *)(out_buf + sizeof(*preq));
	uint32_t count = 0;

	*prsp = *preq;

	pthread_mutex_lock(&g_tb_lock);
	if ((preq->op == SOF_PROBE_OP_INFO_AVAILABLE || preq->op == SOF_PROBE_OP_INFO_ACTIVE) &&
	    sof_get() && sof_get()->ipc) {
		struct list_item *clist;

		list_for_item(clist, &sof_get()->ipc->comp_list) {
			struct ipc_comp_dev *icd = container_of(clist, struct ipc_comp_dev, list);

			if (icd->type != COMP_TYPE_COMPONENT || !icd->cd || count >= cap)
				continue;

			struct tplg_comp_info *ci = find_comp_info_by_ipc_id(g_tp, icd->id);

			pts[count].buffer_id = icd->id;
			pts[count].purpose = 0; /* EXTRACTION */
			pts[count].stream_tag = (uint32_t)(icd->cd->pipeline ?
							   icd->cd->pipeline->pipeline_id : 0);
			snprintf(pts[count].widget_name, sizeof(pts[count].widget_name),
				 "%s", (ci && ci->name) ? ci->name : "sof_widget");
			count++;
		}
	}
	pthread_mutex_unlock(&g_tb_lock);

	prsp->num_points = count;
	uint32_t rsp_len = sizeof(*prsp) + count * sizeof(*pts);
	int ret = send_reply(sock, SOF_NET_OP_PROBE_CTL, req_id, 0, out_buf, rsp_len);

	free(out_buf);
	return ret;
}

static int handle_pcm_ctl(int sock, uint32_t req_id, const uint8_t *req_buf, uint32_t req_len)
{
	/* Accept v1 56-byte requests and extended 72-byte requests */
	if (req_len < 56)
		return send_reply(sock, SOF_NET_OP_PCM_CTL, req_id, -EINVAL, NULL, 0);

	struct sof_net_pcm_req req;
	struct sof_net_pcm_rsp rsp;
	int status = 0;

	memset(&req, 0, sizeof(req));
	memcpy(&req, req_buf, req_len < sizeof(req) ? req_len : sizeof(req));
	memset(&rsp, 0, sizeof(rsp));

	switch (req.op) {
	case SOF_PCM_OP_STATUS:
		break;

	case SOF_PCM_OP_START_TONE:
		if (req.device[0])
			snprintf(g_pcm_device, sizeof(g_pcm_device), "%s", req.device);
		if (req.freq_hz)
			g_tone_freq_hz = req.freq_hz;
		if (req.rate)
			g_pcm_rate = req.rate;
		if (req.channels)
			g_pcm_channels = req.channels;
		g_pcm_api_used = req.api_backend ? req.api_backend : SOF_AUDIO_API_ALSA;
		g_pcm_format = req.format ? req.format : SOF_PCM_FMT_S16_LE;
		g_tone_running = true;
		break;

	case SOF_PCM_OP_STOP_TONE:
		g_tone_running = false;
		break;

	case SOF_PCM_OP_PLAY_BUFFER: {
		if (req.device[0])
			snprintf(g_pcm_device, sizeof(g_pcm_device), "%s", req.device);
		if (req.rate)
			g_pcm_rate = req.rate;
		if (req.channels)
			g_pcm_channels = req.channels;
		g_pcm_api_used = req.api_backend ? req.api_backend : SOF_AUDIO_API_ALSA;
		g_pcm_format = req.format ? req.format : SOF_PCM_FMT_S16_LE;
		if (req.data_size > 0 && req_len >= sizeof(req) + req.data_size) {
			const int16_t *samples = (const int16_t *)(req_buf + sizeof(req));
			size_t sample_cnt = req.data_size / sizeof(int16_t);

			tb_remote_record_output_samples_s16(samples, sample_cnt,
							    (int)g_pcm_channels,
							    (int)g_pcm_rate);
		}
		break;
	}

	case SOF_PCM_OP_CAPTURE_BUFFER: {
		uint32_t rate = g_pcm_rate ? g_pcm_rate : 48000;
		uint32_t ch = g_pcm_channels ? g_pcm_channels : 2;
		uint32_t dur_ms = (req.duration_ms && req.duration_ms <= 2000) ?
				  req.duration_ms : 200;
		size_t req_samples = (size_t)((rate * dur_ms) / 1000) * ch;

		if (req_samples > TB_PCM_CAPTURE_RING_SAMPLES)
			req_samples = TB_PCM_CAPTURE_RING_SAMPLES;

		uint8_t *out_buf = calloc(1, sizeof(rsp) + req_samples * sizeof(int16_t));

		if (!out_buf)
			return send_reply(sock, SOF_NET_OP_PCM_CTL, req_id, -ENOMEM, NULL, 0);

		int16_t *dst = (int16_t *)(out_buf + sizeof(rsp));
		size_t copy_samples;

		pthread_mutex_lock(&g_pcm_ring_lock);
		copy_samples = (req_samples <= g_pcm_ring_avail) ? req_samples : g_pcm_ring_avail;
		size_t start_idx = (g_pcm_ring_write_idx + TB_PCM_CAPTURE_RING_SAMPLES -
				    copy_samples) % TB_PCM_CAPTURE_RING_SAMPLES;
		for (size_t i = 0; i < copy_samples; i++)
			dst[i] = g_pcm_capture_ring[(start_idx + i) % TB_PCM_CAPTURE_RING_SAMPLES];

		struct sof_net_pcm_rsp *crsp = (struct sof_net_pcm_rsp *)out_buf;

		crsp->tone_running = g_tone_running ? 1 : 0;
		snprintf(crsp->active_device, sizeof(crsp->active_device), "%s", g_pcm_device);
		crsp->rate = rate;
		crsp->channels = ch;
		crsp->freq_hz = g_tone_freq_hz;
		crsp->frames_played = g_pcm_frames_processed;
		crsp->rms_level = g_pcm_last_rms;
		crsp->peak_level = g_pcm_last_peak;
		crsp->data_size = (uint32_t)(copy_samples * sizeof(int16_t));
		crsp->api_used = g_pcm_api_used;
		crsp->format = req.format ? req.format : g_pcm_format;
		crsp->xruns = g_pcm_xruns;
		pthread_mutex_unlock(&g_pcm_ring_lock);

		int ret = send_reply(sock, SOF_NET_OP_PCM_CTL, req_id, 0,
				     out_buf, sizeof(rsp) + crsp->data_size);
		free(out_buf);
		return ret;
	}

	default:
		status = -EINVAL;
		break;
	}

	pthread_mutex_lock(&g_pcm_ring_lock);
	rsp.tone_running = g_tone_running ? 1 : 0;
	snprintf(rsp.active_device, sizeof(rsp.active_device), "%s", g_pcm_device);
	rsp.rate = g_pcm_rate;
	rsp.channels = g_pcm_channels;
	rsp.freq_hz = g_tone_freq_hz;
	rsp.frames_played = g_pcm_frames_processed;
	rsp.rms_level = g_pcm_last_rms;
	rsp.peak_level = g_pcm_last_peak;
	rsp.data_size = 0;
	rsp.api_used = g_pcm_api_used;
	rsp.format = g_pcm_format;
	rsp.xruns = g_pcm_xruns;
	pthread_mutex_unlock(&g_pcm_ring_lock);

	return send_reply(sock, SOF_NET_OP_PCM_CTL, req_id, status, &rsp, sizeof(rsp));
}

/* -------------------------------------------------------------------------
 * Sound Card, PCM Capabilities, DAPM Graph, Compress & Kcontrol Handlers
 * -------------------------------------------------------------------------
 */

static uint32_t collect_tb_pcm_nodes(struct testbench_prm *tp,
				     struct sof_net_pcm_node_info *nodes,
				     uint32_t max_nodes,
				     uint32_t *out_num_pcm_devs)
{
	uint32_t n_nodes = 0;
	uint32_t n_devs = 0;
	const char *state_str = g_dsp_paused ? "PAUSED" : "RUNNING";

	if (tp && tp->pcm_list.next && !list_is_empty(&tp->pcm_list)) {
		struct list_item *item;

		list_for_item(item, &tp->pcm_list) {
			struct tplg_pcm_info *pi = container_of(item, struct tplg_pcm_info, item);
			bool has_pb = pi->playback_host || pi->playback_pipeline_list.count > 0;
			bool has_cp = pi->capture_host || pi->capture_pipeline_list.count > 0;

			n_devs++;
			if (!has_pb && !has_cp)
				has_pb = true;

			if (has_pb && n_nodes < max_nodes) {
				struct sof_net_pcm_node_info *pn = &nodes[n_nodes++];

				memset(pn, 0, sizeof(*pn));
				pn->card_id = 0;
				pn->device_id = (uint32_t)pi->id;
				pn->direction = SOF_STREAM_DIR_PLAYBACK;
				pn->is_compress = 0;
				snprintf(pn->id, sizeof(pn->id), "%s (*)",
					 pi->name ? pi->name : "Port0");
				snprintf(pn->name, sizeof(pn->name), "%s",
					 pi->name ? pi->name : "Port0");
				pn->subdevices_count = 1;
				pn->subdevices_avail = 1;
				snprintf(pn->state_str, sizeof(pn->state_str), "%s", state_str);
				pn->is_running = g_dsp_paused ? 0 : 1;
				snprintf(pn->active_format, sizeof(pn->active_format), "S16_LE");
				pn->active_rate = g_pcm_rate ? g_pcm_rate : 48000;
				pn->active_channels = g_pcm_channels ? g_pcm_channels : 2;
				pn->period_size = 48;
				pn->buffer_size = 192;
				pn->hw_ptr = g_pcm_frames_processed;
				pn->appl_ptr = g_pcm_frames_processed;
			}
			if (has_cp && n_nodes < max_nodes) {
				struct sof_net_pcm_node_info *cn = &nodes[n_nodes++];

				memset(cn, 0, sizeof(*cn));
				cn->card_id = 0;
				cn->device_id = (uint32_t)pi->id;
				cn->direction = SOF_STREAM_DIR_CAPTURE;
				cn->is_compress = 0;
				snprintf(cn->id, sizeof(cn->id), "%s (*)",
					 pi->name ? pi->name : "Port0");
				snprintf(cn->name, sizeof(cn->name), "%s",
					 pi->name ? pi->name : "Port0");
				cn->subdevices_count = 1;
				cn->subdevices_avail = 1;
				snprintf(cn->state_str, sizeof(cn->state_str), "%s", state_str);
				cn->is_running = g_dsp_paused ? 0 : 1;
				snprintf(cn->active_format, sizeof(cn->active_format), "S16_LE");
				cn->active_rate = g_pcm_rate ? g_pcm_rate : 48000;
				cn->active_channels = g_pcm_channels ? g_pcm_channels : 2;
				cn->period_size = 48;
				cn->buffer_size = 192;
				cn->hw_ptr = g_pcm_frames_processed;
				cn->appl_ptr = g_pcm_frames_processed;
			}
		}
	}

	if (n_nodes == 0) {
		n_devs = 1;
		for (uint32_t dir = 0; dir < 2 && n_nodes < max_nodes; dir++) {
			struct sof_net_pcm_node_info *pn = &nodes[n_nodes++];

			memset(pn, 0, sizeof(*pn));
			pn->card_id = 0;
			pn->device_id = 0;
			pn->direction = dir;
			pn->is_compress = 0;
			snprintf(pn->id, sizeof(pn->id),
				 dir == 0 ? "Testbench Playback (*)" : "Testbench Capture (*)");
			snprintf(pn->name, sizeof(pn->name),
				 dir == 0 ? "Testbench Playback" : "Testbench Capture");
			pn->subdevices_count = 1;
			pn->subdevices_avail = 1;
			snprintf(pn->state_str, sizeof(pn->state_str), "%s", state_str);
			pn->is_running = g_dsp_paused ? 0 : 1;
			snprintf(pn->active_format, sizeof(pn->active_format), "S16_LE");
			pn->active_rate = g_pcm_rate ? g_pcm_rate : 48000;
			pn->active_channels = g_pcm_channels ? g_pcm_channels : 2;
			pn->period_size = 48;
			pn->buffer_size = 192;
			pn->hw_ptr = g_pcm_frames_processed;
			pn->appl_ptr = g_pcm_frames_processed;
		}
	}

	/* Add 1 virtual Compress Offload node */
	if (n_nodes < max_nodes) {
		struct sof_net_pcm_node_info *cn = &nodes[n_nodes++];

		memset(cn, 0, sizeof(*cn));
		cn->card_id = 0;
		cn->device_id = 0;
		cn->direction = SOF_STREAM_DIR_PLAYBACK;
		cn->is_compress = 1;
		snprintf(cn->id, sizeof(cn->id), "Testbench Compr (*)");
		snprintf(cn->name, sizeof(cn->name), "SOF Testbench Compress");
		cn->subdevices_count = 1;
		cn->subdevices_avail = 1;
		snprintf(cn->state_str, sizeof(cn->state_str), "CLOSED");
	}

	if (out_num_pcm_devs)
		*out_num_pcm_devs = n_devs;
	return n_nodes;
}

static int handle_card_list(int sock, uint32_t req_id)
{
	struct sof_net_pcm_node_info nodes[64];
	struct sof_net_card_info card;
	struct sof_net_card_list_hdr hdr;
	uint32_t n_devs = 0;
	uint32_t n_nodes = 0;

	memset(&hdr, 0, sizeof(hdr));
	memset(&card, 0, sizeof(card));
	memset(nodes, 0, sizeof(nodes));

	pthread_mutex_lock(&g_tb_lock);
	n_nodes = collect_tb_pcm_nodes(g_tp, nodes, 64, &n_devs);

	const char *base_tplg = "headless";

	if (g_tp && g_tp->tplg_file) {
		const char *slash = strrchr(g_tp->tplg_file, '/');

		base_tplg = slash ? (slash + 1) : g_tp->tplg_file;
	}

	card.card_id = 0;
	snprintf(card.id, sizeof(card.id), "softestbench4");
	snprintf(card.driver, sizeof(card.driver), "sof-testbench4");
	snprintf(card.name, sizeof(card.name), "SOF Testbench IPC4");
	snprintf(card.longname, sizeof(card.longname),
		 "SOF Testbench IPC4 Virtual DSP (%s)", base_tplg);
	snprintf(card.mixername, sizeof(card.mixername), "SOF Testbench Mixer");
	snprintf(card.tplg_file, sizeof(card.tplg_file), "%s", base_tplg);
	card.num_pcm_streams = n_nodes > 0 ? (n_nodes - 1) : n_devs;
	card.num_compr_streams = 1;
	card.num_kcontrols = g_tp ? g_tp->glb_ctx.num_ctls : 0;
	card.active_streams = g_dsp_paused ? 0 : (g_tp ? (uint32_t)g_tp->pipeline_num : 1);
	pthread_mutex_unlock(&g_tb_lock);

	hdr.num_cards = 1;
	hdr.num_pcm_nodes = n_nodes;

	size_t total_len = sizeof(hdr) + sizeof(card) + n_nodes * sizeof(nodes[0]);
	uint8_t *out_buf = calloc(1, total_len);

	if (!out_buf)
		return send_reply(sock, SOF_NET_OP_CARD_LIST, req_id, -ENOMEM, NULL, 0);

	memcpy(out_buf, &hdr, sizeof(hdr));
	memcpy(out_buf + sizeof(hdr), &card, sizeof(card));
	if (n_nodes > 0)
		memcpy(out_buf + sizeof(hdr) + sizeof(card), nodes, n_nodes * sizeof(nodes[0]));

	int ret = send_reply(sock, SOF_NET_OP_CARD_LIST, req_id, 0, out_buf, (uint32_t)total_len);

	free(out_buf);
	return ret;
}

static int handle_pcm_caps(int sock, uint32_t req_id, const uint8_t *req_buf, uint32_t req_len)
{
	struct sof_net_pcm_caps_req creq = {
		.card_id = 0xFFFFFFFFU,
		.device_id = 0xFFFFFFFFU,
		.dir_mask = SOF_CAPS_DIR_ALL,
		.api_backend = SOF_AUDIO_API_ALSA,
	};

	if (req_len >= sizeof(creq))
		memcpy(&creq, req_buf, sizeof(creq));

	struct sof_net_pcm_node_info nodes[64];
	struct sof_net_pcm_caps caps[64];
	uint32_t n_nodes = 0;
	uint32_t n_caps = 0;

	memset(nodes, 0, sizeof(nodes));
	memset(caps, 0, sizeof(caps));

	pthread_mutex_lock(&g_tb_lock);
	n_nodes = collect_tb_pcm_nodes(g_tp, nodes, 64, NULL);
	pthread_mutex_unlock(&g_tb_lock);

	for (uint32_t i = 0; i < n_nodes && n_caps < 64; i++) {
		if (creq.card_id != 0xFFFFFFFFU && nodes[i].card_id != creq.card_id)
			continue;
		if (creq.device_id != 0xFFFFFFFFU && nodes[i].device_id != creq.device_id)
			continue;
		if (nodes[i].is_compress) {
			if (!(creq.dir_mask & SOF_CAPS_DIR_COMPRESS))
				continue;
		} else {
			if (!(creq.dir_mask & (1U << nodes[i].direction)))
				continue;
		}

		struct sof_net_pcm_caps *cp = &caps[n_caps++];

		cp->card_id = nodes[i].card_id;
		cp->device_id = nodes[i].device_id;
		cp->direction = nodes[i].direction;
		cp->is_compress = nodes[i].is_compress;
		cp->api_used = creq.api_backend ? creq.api_backend : SOF_AUDIO_API_ALSA;
		cp->status = 0;
		snprintf(cp->id, sizeof(cp->id), "%s", nodes[i].id);
		snprintf(cp->name, sizeof(cp->name), "%s", nodes[i].name);

		if (cp->is_compress) {
			cp->min_fragment_size = 1024;
			cp->max_fragment_size = 65536;
			cp->min_fragments = 2;
			cp->max_fragments = 16;
			cp->num_codecs = 3;
			cp->codecs[0] = 1; /* PCM */
			cp->codecs[1] = 2; /* MP3 */
			cp->codecs[2] = 3; /* AAC */
			snprintf(cp->formats_str, sizeof(cp->formats_str), "PCM MP3 AAC");
		} else {
			cp->formats_mask = SOF_FMTBIT_S16_LE | SOF_FMTBIT_S24_LE |
					   SOF_FMTBIT_S32_LE | SOF_FMTBIT_FLOAT_LE;
			cp->access_mask = 0x09; /* MMAP_INTERLEAVED | RW_INTERLEAVED */
			cp->rates_mask = SOF_RATEBIT_8000 | SOF_RATEBIT_16000 |
					 SOF_RATEBIT_32000 | SOF_RATEBIT_44100 |
					 SOF_RATEBIT_48000 | SOF_RATEBIT_96000 |
					 SOF_RATEBIT_192000;
			cp->rate_min = 8000;
			cp->rate_max = 192000;
			cp->channels_min = 1;
			cp->channels_max = 8;
			cp->sample_bits_min = 16;
			cp->sample_bits_max = 32;
			cp->period_size_min = 48;
			cp->period_size_max = 16384;
			cp->period_bytes_min = 192;
			cp->period_bytes_max = 65536;
			cp->periods_min = 2;
			cp->periods_max = 32;
			cp->buffer_size_min = 96;
			cp->buffer_size_max = 65536;
			cp->buffer_bytes_min = 384;
			cp->buffer_bytes_max = 262144;
			snprintf(cp->formats_str, sizeof(cp->formats_str),
				 "S16_LE S24_LE S32_LE FLOAT_LE");
			snprintf(cp->rates_str, sizeof(cp->rates_str),
				 "8000 16000 32000 44100 48000 96000 192000");
		}
	}

	struct sof_net_pcm_caps_hdr hdr = {
		.num_entries = n_caps,
		.api_used = creq.api_backend ? creq.api_backend : SOF_AUDIO_API_ALSA,
	};
	size_t total_len = sizeof(hdr) + n_caps * sizeof(caps[0]);
	uint8_t *out_buf = calloc(1, total_len);

	if (!out_buf)
		return send_reply(sock, SOF_NET_OP_PCM_CAPS, req_id, -ENOMEM, NULL, 0);

	memcpy(out_buf, &hdr, sizeof(hdr));
	if (n_caps > 0)
		memcpy(out_buf + sizeof(hdr), caps, n_caps * sizeof(caps[0]));

	int ret = send_reply(sock, SOF_NET_OP_PCM_CAPS, req_id, 0, out_buf, (uint32_t)total_len);

	free(out_buf);
	return ret;
}

static void classify_tb_dapm_widget(const struct tplg_comp_info *ci,
				    char *wtype_out, size_t wtype_cap,
				    uint16_t *col_out)
{
	*col_out = 1;
	snprintf(wtype_out, wtype_cap, "widget");

	if (!ci)
		return;

	switch (ci->type) {
	case SND_SOC_TPLG_DAPM_AIF_IN:
		snprintf(wtype_out, wtype_cap, "aif_in");
		*col_out = 0;
		break;
	case SND_SOC_TPLG_DAPM_AIF_OUT:
		snprintf(wtype_out, wtype_cap, "aif_out");
		*col_out = 0;
		break;
	case SND_SOC_TPLG_DAPM_DAI_IN:
		snprintf(wtype_out, wtype_cap, "dai_in");
		*col_out = 3;
		break;
	case SND_SOC_TPLG_DAPM_DAI_OUT:
		snprintf(wtype_out, wtype_cap, "dai_out");
		*col_out = 3;
		break;
	case SND_SOC_TPLG_DAPM_PGA:
		snprintf(wtype_out, wtype_cap, "pga");
		*col_out = 1;
		break;
	case SND_SOC_TPLG_DAPM_MIXER:
		snprintf(wtype_out, wtype_cap, "mixer");
		*col_out = 2;
		break;
	case SND_SOC_TPLG_DAPM_MUX:
		snprintf(wtype_out, wtype_cap, "mux");
		*col_out = 2;
		break;
	case SND_SOC_TPLG_DAPM_BUFFER:
		snprintf(wtype_out, wtype_cap, "buffer");
		*col_out = 1;
		break;
	case SND_SOC_TPLG_DAPM_SCHEDULER:
		snprintf(wtype_out, wtype_cap, "scheduler");
		*col_out = 1;
		break;
	case SND_SOC_TPLG_DAPM_EFFECT:
		snprintf(wtype_out, wtype_cap, "effect");
		*col_out = 1;
		break;
	case SND_SOC_TPLG_DAPM_SRC:
		snprintf(wtype_out, wtype_cap, "src");
		*col_out = 1;
		break;
	case SND_SOC_TPLG_DAPM_ASRC:
		snprintf(wtype_out, wtype_cap, "asrc");
		*col_out = 1;
		break;
	default:
		break;
	}

	if (ci->name) {
		if (strncmp(ci->name, "host-copier", 11) == 0)
			*col_out = 0;
		else if (strncmp(ci->name, "dai-copier", 10) == 0)
			*col_out = 3;
	}
}

static int handle_dapm_graph(int sock, uint32_t req_id, const uint8_t *req_buf, uint32_t req_len)
{
	(void)req_buf;
	(void)req_len;

	uint32_t max_nodes = 256;
	uint32_t max_edges = 512;
	uint32_t max_links = 32;

	struct sof_net_dapm_comp_status comp;
	struct sof_net_dapm_node *nodes = calloc(max_nodes, sizeof(*nodes));
	struct sof_net_dapm_edge *edges = calloc(max_edges, sizeof(*edges));
	struct sof_net_dpcm_link_state *links = calloc(max_links, sizeof(*links));
	const struct tplg_comp_info **ci_ptrs = calloc(max_nodes, sizeof(*ci_ptrs));

	if (!nodes || !edges || !links || !ci_ptrs) {
		free(nodes);
		free(edges);
		free(links);
		free(ci_ptrs);
		return send_reply(sock, SOF_NET_OP_DAPM_GRAPH, req_id, -ENOMEM, NULL, 0);
	}

	uint32_t n_nodes = 0;
	uint32_t n_powered = 0;
	uint32_t n_edges = 0;
	uint32_t n_active_edges = 0;
	uint32_t n_links = 0;

	pthread_mutex_lock(&g_tb_lock);

	const char *base_tplg = "headless";

	if (g_tp && g_tp->tplg_file) {
		const char *slash = strrchr(g_tp->tplg_file, '/');

		base_tplg = slash ? (slash + 1) : g_tp->tplg_file;
	}

	if (g_tp && g_tp->widget_list.next && !list_is_empty(&g_tp->widget_list)) {
		struct list_item *item;

		list_for_item(item, &g_tp->widget_list) {
			struct tplg_comp_info *ci = container_of(item, struct tplg_comp_info, item);

			if (n_nodes >= max_nodes)
				break;

			struct sof_net_dapm_node *nd = &nodes[n_nodes];
			uint16_t col = 1;

			ci_ptrs[n_nodes] = ci;
			nd->node_id = n_nodes;
			snprintf(nd->name, sizeof(nd->name), "%s",
				 ci->name ? ci->name : "unnamed");
			snprintf(nd->component, sizeof(nd->component), "sof-testbench4");
			snprintf(nd->stream_name, sizeof(nd->stream_name), "%s",
				 ci->stream_name ? ci->stream_name : "");
			classify_tb_dapm_widget(ci, nd->widget_type, sizeof(nd->widget_type), &col);
			nd->layout_col = col;
			nd->power = (ci->set_up && !g_dsp_paused &&
				     tb_is_pipeline_enabled(g_tp, ci->pipeline_id)) ? 1 : 0;
			if (nd->power)
				n_powered++;
			nd->stream_active = nd->power;
			nd->in_ep = 1;
			nd->out_ep = 1;
			nd->reg = -1;
			nd->pipeline_id = ci->pipeline_id;
			nd->layout_row = (uint16_t)(ci->pipeline_id >= 0 ? ci->pipeline_id : 0);

			/* Attach kcontrols belonging to this widget */
			for (uint32_t k = 0; k < g_tp->glb_ctx.num_ctls &&
					     nd->num_kcontrols < 4; k++) {
				struct tb_ctl *ctl = &g_tp->glb_ctx.ctl[k];

				if (ctl->comp_info == ci ||
				    ((int)ctl->module_id == ci->module_id &&
				     (int)ctl->instance_id == ci->instance_id &&
				     ci->module_id > 0)) {
					uint32_t slot = nd->num_kcontrols++;

					nd->kcontrol_numids[slot] = k + 1;
					if (slot < 2)
						snprintf(nd->kcontrol_names[slot],
							 sizeof(nd->kcontrol_names[slot]),
							 "%.47s", ctl->name);
				}
			}
			n_nodes++;
		}
	}

	if (g_tp && g_tp->route_list.next && !list_is_empty(&g_tp->route_list)) {
		struct list_item *item;

		list_for_item(item, &g_tp->route_list) {
			struct tplg_route_info *rt =
				container_of(item, struct tplg_route_info, item);

			if (!rt->source || !rt->sink || n_edges >= max_edges)
				continue;

			int32_t src_idx = -1;
			int32_t dst_idx = -1;

			for (uint32_t i = 0; i < n_nodes; i++) {
				if (ci_ptrs[i] == rt->source)
					src_idx = (int32_t)i;
				if (ci_ptrs[i] == rt->sink)
					dst_idx = (int32_t)i;
			}
			if (src_idx >= 0 && dst_idx >= 0) {
				struct sof_net_dapm_edge *ed = &edges[n_edges];

				ed->edge_id = n_edges++;
				ed->src_node_id = src_idx;
				ed->dst_node_id = dst_idx;
				snprintf(ed->src_name, sizeof(ed->src_name), "%s",
					 rt->source->name ? rt->source->name : "src");
				snprintf(ed->dst_name, sizeof(ed->dst_name), "%s",
					 rt->sink->name ? rt->sink->name : "dst");
				snprintf(ed->src_comp, sizeof(ed->src_comp), "sof-testbench4");
				snprintf(ed->dst_comp, sizeof(ed->dst_comp), "sof-testbench4");
				snprintf(ed->control_name, sizeof(ed->control_name), "static");
				ed->active = (rt->set_up && !g_dsp_paused) ? 1 : 0;
				if (ed->active)
					n_active_edges++;
			}
		}
	}

	if (g_tp && g_tp->pcm_list.next && !list_is_empty(&g_tp->pcm_list)) {
		struct list_item *item;

		list_for_item(item, &g_tp->pcm_list) {
			struct tplg_pcm_info *pi = container_of(item, struct tplg_pcm_info, item);

			if ((pi->playback_host || pi->playback_pipeline_list.count > 0) &&
			    n_links < max_links) {
				struct sof_net_dpcm_link_state *lk = &links[n_links++];

				snprintf(lk->fe_name, sizeof(lk->fe_name), "%s",
					 pi->name ? pi->name : "Port0");
				lk->direction = SOF_STREAM_DIR_PLAYBACK;
				snprintf(lk->state_str, sizeof(lk->state_str), "%s",
					 g_dsp_paused ? "PAUSED" : "RUNNING");
				snprintf(lk->fe_format, sizeof(lk->fe_format), "S16_LE");
				lk->fe_channels = g_pcm_channels ? g_pcm_channels : 2;
				lk->fe_rate = g_pcm_rate ? g_pcm_rate : 48000;
				lk->num_backends = 1;
				snprintf(lk->be_names[0], sizeof(lk->be_names[0]), "SSP0-Codec");
				snprintf(lk->be_states[0], sizeof(lk->be_states[0]), "%s",
					 g_dsp_paused ? "PAUSED" : "RUNNING");
				snprintf(lk->be_formats[0], sizeof(lk->be_formats[0]), "S16_LE");
				lk->be_channels[0] = lk->fe_channels;
				lk->be_rates[0] = lk->fe_rate;
			}
			if ((pi->capture_host || pi->capture_pipeline_list.count > 0) &&
			    n_links < max_links) {
				struct sof_net_dpcm_link_state *lk = &links[n_links++];

				snprintf(lk->fe_name, sizeof(lk->fe_name), "%s",
					 pi->name ? pi->name : "Port0");
				lk->direction = SOF_STREAM_DIR_CAPTURE;
				snprintf(lk->state_str, sizeof(lk->state_str), "%s",
					 g_dsp_paused ? "PAUSED" : "RUNNING");
				snprintf(lk->fe_format, sizeof(lk->fe_format), "S16_LE");
				lk->fe_channels = g_pcm_channels ? g_pcm_channels : 2;
				lk->fe_rate = g_pcm_rate ? g_pcm_rate : 48000;
				lk->num_backends = 1;
				snprintf(lk->be_names[0], sizeof(lk->be_names[0]), "SSP0-Codec");
				snprintf(lk->be_states[0], sizeof(lk->be_states[0]), "%s",
					 g_dsp_paused ? "PAUSED" : "RUNNING");
				snprintf(lk->be_formats[0], sizeof(lk->be_formats[0]), "S16_LE");
				lk->be_channels[0] = lk->fe_channels;
				lk->be_rates[0] = lk->fe_rate;
			}
		}
	}

	memset(&comp, 0, sizeof(comp));
	snprintf(comp.name, sizeof(comp.name), "sof-testbench4");
	snprintf(comp.bias_str, sizeof(comp.bias_str), "%s", g_dsp_paused ? "Standby" : "On");
	comp.bias_level = g_dsp_paused ? 1 : 3;
	comp.widget_count = n_nodes;
	comp.powered_count = n_powered;

	struct sof_net_dapm_graph_hdr hdr;

	memset(&hdr, 0, sizeof(hdr));
	hdr.card_id = 0;
	snprintf(hdr.card_name, sizeof(hdr.card_name), "sof-testbench4");
	snprintf(hdr.tplg_file, sizeof(hdr.tplg_file), "%s", base_tplg);
	hdr.num_components = 1;
	hdr.num_nodes = n_nodes;
	hdr.num_powered_nodes = n_powered;
	hdr.num_edges = n_edges;
	hdr.num_active_edges = n_active_edges;
	hdr.num_dpcm_links = n_links;

	pthread_mutex_unlock(&g_tb_lock);

	size_t total_len = sizeof(hdr) + sizeof(comp) +
			   n_nodes * sizeof(*nodes) +
			   n_edges * sizeof(*edges) +
			   n_links * sizeof(*links);
	uint8_t *out_buf = calloc(1, total_len);
	int ret;

	if (!out_buf) {
		ret = send_reply(sock, SOF_NET_OP_DAPM_GRAPH, req_id, -ENOMEM, NULL, 0);
	} else {
		uint8_t *p = out_buf;

		memcpy(p, &hdr, sizeof(hdr));
		p += sizeof(hdr);
		memcpy(p, &comp, sizeof(comp));
		p += sizeof(comp);
		if (n_nodes > 0) {
			memcpy(p, nodes, n_nodes * sizeof(*nodes));
			p += n_nodes * sizeof(*nodes);
		}
		if (n_edges > 0) {
			memcpy(p, edges, n_edges * sizeof(*edges));
			p += n_edges * sizeof(*edges);
		}
		if (n_links > 0)
			memcpy(p, links, n_links * sizeof(*links));

		ret = send_reply(sock, SOF_NET_OP_DAPM_GRAPH, req_id, 0,
				 out_buf, (uint32_t)total_len);
		free(out_buf);
	}

	free(nodes);
	free(edges);
	free(links);
	free(ci_ptrs);
	return ret;
}

static int handle_compr_ctl(int sock, uint32_t req_id, const uint8_t *req_buf, uint32_t req_len)
{
	if (req_len < sizeof(struct sof_net_compr_req))
		return send_reply(sock, SOF_NET_OP_COMPR_CTL, req_id, -EINVAL, NULL, 0);

	const struct sof_net_compr_req *creq = (const struct sof_net_compr_req *)req_buf;
	struct sof_net_compr_rsp rsp;

	memset(&rsp, 0, sizeof(rsp));
	rsp.status = 0;
	rsp.card_id = creq->card_id;
	rsp.device_id = creq->device_id;
	rsp.direction = creq->direction;
	rsp.codec_id = creq->codec_id ? creq->codec_id : 1;
	rsp.fragment_size = creq->fragment_size ? creq->fragment_size : 4096;
	rsp.fragments = creq->fragments ? creq->fragments : 4;
	rsp.min_fragment_size = 1024;
	rsp.max_fragment_size = 65536;
	rsp.num_codecs = 3;
	rsp.codecs[0] = 1; /* SND_AUDIOCODEC_PCM */
	rsp.codecs[1] = 2; /* SND_AUDIOCODEC_MP3 */
	rsp.codecs[2] = 3; /* SND_AUDIOCODEC_AAC */
	rsp.sampling_rate = creq->rate ? creq->rate : (g_pcm_rate ? g_pcm_rate : 48000);

	if (creq->op == SOF_COMPR_OP_PLAY_BUFFER) {
		uint32_t dur_ms = creq->duration_ms ? creq->duration_ms : 500;
		uint32_t frames = (rsp.sampling_rate * dur_ms) / 1000;

		rsp.bytes_transferred = creq->data_size ? creq->data_size : (frames * 4);
		rsp.rendered_samples = frames;
	} else if (creq->op == SOF_COMPR_OP_CAPTURE_BUFFER) {
		uint32_t cap_bytes = creq->data_size ? creq->data_size : 4096;

		if (cap_bytes > 65536)
			cap_bytes = 65536;

		uint8_t *out_buf = calloc(1, sizeof(rsp) + cap_bytes);

		if (!out_buf)
			return send_reply(sock, SOF_NET_OP_COMPR_CTL, req_id, -ENOMEM, NULL, 0);

		pthread_mutex_lock(&g_pcm_ring_lock);
		size_t avail_bytes = g_pcm_ring_avail * sizeof(int16_t);
		size_t to_copy = cap_bytes < avail_bytes ? cap_bytes : avail_bytes;

		if (to_copy > 0)
			memcpy(out_buf + sizeof(rsp), g_pcm_capture_ring, to_copy);
		pthread_mutex_unlock(&g_pcm_ring_lock);

		rsp.bytes_transferred = cap_bytes;
		rsp.rendered_samples = cap_bytes / 4;
		rsp.data_size = cap_bytes;
		memcpy(out_buf, &rsp, sizeof(rsp));

		int ret = send_reply(sock, SOF_NET_OP_COMPR_CTL, req_id, 0,
				     out_buf, sizeof(rsp) + cap_bytes);
		free(out_buf);
		return ret;
	}

	return send_reply(sock, SOF_NET_OP_COMPR_CTL, req_id, 0, &rsp, sizeof(rsp));
}

static void populate_tb_kctl_info(uint32_t idx, struct tb_ctl *ctl, struct sof_net_kctl_info *ki)
{
	struct tb_kctl_shadow *sh = (idx < TB_REMOTE_MAX_KCTLS) ? &g_kctl_shadow[idx] : NULL;

	memset(ki, 0, sizeof(*ki));
	ki->card_id = 0;
	ki->numid = idx + 1;
	ki->iface = 2; /* SNDRV_CTL_ELEM_IFACE_MIXER */
	snprintf(ki->iface_str, sizeof(ki->iface_str), "MIXER");
	ki->index = ctl->index;
	snprintf(ki->name, sizeof(ki->name), "%.63s", ctl->name);

	if (ctl->type == SND_SOC_TPLG_TYPE_MIXER) {
		uint32_t nch = ctl->mixer_ctl.num_channels ?
			       (uint32_t)ctl->mixer_ctl.num_channels : 2;
		int64_t max_v = ctl->mixer_ctl.max ? ctl->mixer_ctl.max : 32;

		if (nch > 8)
			nch = 8;
		ki->count = nch;
		ki->type = (max_v == 1) ? SOF_KCTL_TYPE_BOOLEAN : SOF_KCTL_TYPE_INTEGER;
		snprintf(ki->type_str, sizeof(ki->type_str), "%s",
			 (max_v == 1) ? "BOOLEAN" : "INTEGER");
		ki->min_val = ctl->mixer_ctl.min;
		ki->max_val = max_v;
		ki->step_val = 1;
		ki->access_mask = SOF_KCTL_ACC_READ | SOF_KCTL_ACC_WRITE | SOF_KCTL_ACC_TLV_READ;
		snprintf(ki->access_str, sizeof(ki->access_str), "rwR");

		if (ctl->mixer_ctl.hdr.tlv.size > 0) {
			ki->has_db_scale = 1;
			ki->db_min_cdb = (int32_t)ctl->mixer_ctl.hdr.tlv.scale.min;
			ki->db_step_cdb = (int32_t)(ctl->mixer_ctl.hdr.tlv.scale.step ?
						    ctl->mixer_ctl.hdr.tlv.scale.step : 100);
			ki->db_mute = ctl->mixer_ctl.hdr.tlv.scale.mute ? 1 : 0;
		} else if (max_v > 1) {
			ki->has_db_scale = 1;
			ki->db_min_cdb = -5000;
			ki->db_step_cdb = 200;
			ki->db_mute = 1;
		}

		if (sh && !sh->initialized) {
			int64_t def_val = (max_v == 1) ? 1 : (max_v * 3) / 4;

			for (uint32_t c = 0; c < nch; c++)
				sh->values[c] = def_val;
			sh->initialized = true;
		}
	} else if (ctl->type == SND_SOC_TPLG_TYPE_ENUM) {
		uint32_t nch = ctl->enum_ctl.num_channels ?
			       (uint32_t)ctl->enum_ctl.num_channels : 1;
		uint32_t nitems = (uint32_t)ctl->enum_ctl.items;

		if (nch > 8)
			nch = 8;
		if (nitems > 8)
			nitems = 8;
		ki->type = SOF_KCTL_TYPE_ENUMERATED;
		snprintf(ki->type_str, sizeof(ki->type_str), "ENUM");
		ki->count = nch;
		ki->access_mask = SOF_KCTL_ACC_READ | SOF_KCTL_ACC_WRITE;
		snprintf(ki->access_str, sizeof(ki->access_str), "rw");
		ki->num_enums = nitems;
		for (uint32_t e = 0; e < nitems; e++)
			snprintf(ki->enum_names[e], sizeof(ki->enum_names[e]),
				 "%s", ctl->enum_ctl.texts[e]);
		if (sh && !sh->initialized)
			sh->initialized = true;
	} else {
		ki->type = SOF_KCTL_TYPE_BYTES;
		snprintf(ki->type_str, sizeof(ki->type_str), "BYTES");
		ki->count = ctl->bytes_ctl.size ? (uint32_t)ctl->bytes_ctl.size : 64;
		ki->access_mask = SOF_KCTL_ACC_READ | SOF_KCTL_ACC_WRITE |
				  SOF_KCTL_ACC_TLV_READ | SOF_KCTL_ACC_TLV_WRITE;
		snprintf(ki->access_str, sizeof(ki->access_str), "rwRW");
		if (sh && !sh->initialized) {
			if (ctl->bytes_ctl.priv.size > 0) {
				uint32_t blen = (uint32_t)ctl->bytes_ctl.priv.size;

				if (blen > sizeof(sh->bytes_data))
					blen = sizeof(sh->bytes_data);
				memcpy(sh->bytes_data, ctl->bytes_ctl.priv.data, blen);
				sh->bytes_len = blen;
			}
			sh->initialized = true;
		}
	}

	if (sh) {
		for (int c = 0; c < 8; c++)
			ki->values[c] = sh->values[c];
	}

	if (ctl->comp_info) {
		char wtype[24];
		uint16_t col = 1;

		ki->anchor_type = SOF_KCTL_ANCHOR_WIDGET;
		snprintf(ki->target_widget, sizeof(ki->target_widget), "%s",
			 ctl->comp_info->name ? ctl->comp_info->name : "");
		snprintf(ki->target_component, sizeof(ki->target_component), "sof-testbench4");
		ki->pipeline_id = ctl->comp_info->pipeline_id;
		ki->layout_row = (uint16_t)(ctl->comp_info->pipeline_id >= 0 ?
					    ctl->comp_info->pipeline_id : 0);
		classify_tb_dapm_widget(ctl->comp_info, wtype, sizeof(wtype), &col);
		ki->layout_col = col;
	} else {
		ki->anchor_type = SOF_KCTL_ANCHOR_CARD;
		snprintf(ki->target_component, sizeof(ki->target_component), "sof-testbench4");
		ki->pipeline_id = -1;
		ki->layout_col = 4;
	}
}

static int handle_kctl_list(int sock, uint32_t req_id, const uint8_t *req_buf, uint32_t req_len)
{
	uint32_t api_used = SOF_AUDIO_API_ALSA;

	if (req_len >= sizeof(struct sof_net_kctl_list_req)) {
		const struct sof_net_kctl_list_req *kreq =
			(const struct sof_net_kctl_list_req *)req_buf;

		if (kreq->api_backend)
			api_used = kreq->api_backend;
	}

	pthread_mutex_lock(&g_tb_lock);
	uint32_t n_ctls = g_tp ? g_tp->glb_ctx.num_ctls : 0;

	if (n_ctls > TB_REMOTE_MAX_KCTLS)
		n_ctls = TB_REMOTE_MAX_KCTLS;

	size_t total_len = sizeof(struct sof_net_kctl_list_hdr) +
			   n_ctls * sizeof(struct sof_net_kctl_info);
	uint8_t *out_buf = calloc(1, total_len);

	if (!out_buf) {
		pthread_mutex_unlock(&g_tb_lock);
		return send_reply(sock, SOF_NET_OP_KCTL_LIST, req_id, -ENOMEM, NULL, 0);
	}

	struct sof_net_kctl_list_hdr *hdr = (struct sof_net_kctl_list_hdr *)out_buf;
	struct sof_net_kctl_info *items = (struct sof_net_kctl_info *)(out_buf + sizeof(*hdr));

	hdr->card_id = 0;
	hdr->api_used = api_used;
	hdr->num_kcontrols = n_ctls;

	for (uint32_t i = 0; i < n_ctls; i++)
		populate_tb_kctl_info(i, &g_tp->glb_ctx.ctl[i], &items[i]);

	pthread_mutex_unlock(&g_tb_lock);

	int ret = send_reply(sock, SOF_NET_OP_KCTL_LIST, req_id, 0, out_buf, (uint32_t)total_len);

	free(out_buf);
	return ret;
}

static int handle_kctl_io(int sock, uint16_t opcode, uint32_t req_id,
			  const uint8_t *req_buf, uint32_t req_len)
{
	if (req_len < sizeof(struct sof_net_kctl_io_req))
		return send_reply(sock, opcode, req_id, -EINVAL, NULL, 0);

	const struct sof_net_kctl_io_req *kreq = (const struct sof_net_kctl_io_req *)req_buf;
	struct sof_net_kctl_io_rsp rsp;
	int status = 0;

	memset(&rsp, 0, sizeof(rsp));

	pthread_mutex_lock(&g_tb_lock);
	uint32_t n_ctls = g_tp ? g_tp->glb_ctx.num_ctls : 0;

	if (n_ctls > TB_REMOTE_MAX_KCTLS)
		n_ctls = TB_REMOTE_MAX_KCTLS;

	int32_t found_idx = -1;

	for (uint32_t i = 0; i < n_ctls; i++) {
		if (kreq->numid > 0 && (i + 1) == kreq->numid) {
			found_idx = (int32_t)i;
			break;
		}
		if (kreq->name[0] && strcmp(g_tp->glb_ctx.ctl[i].name, kreq->name) == 0) {
			found_idx = (int32_t)i;
			break;
		}
	}

	if (found_idx < 0) {
		pthread_mutex_unlock(&g_tb_lock);
		return send_reply(sock, opcode, req_id, -ENOENT, NULL, 0);
	}

	uint32_t idx = (uint32_t)found_idx;
	struct tb_ctl *ctl = &g_tp->glb_ctx.ctl[idx];
	struct sof_net_kctl_info kinfo;

	populate_tb_kctl_info(idx, ctl, &kinfo);

	if (opcode == SOF_NET_OP_KCTL_SET) {
		struct tb_kctl_shadow *sh = &g_kctl_shadow[idx];

		if (kinfo.type == SOF_KCTL_TYPE_BYTES || kreq->bytes_len > 0) {
			uint32_t blen = kreq->bytes_len;

			if (blen > sizeof(sh->bytes_data))
				blen = sizeof(sh->bytes_data);
			memcpy(sh->bytes_data, kreq->bytes_data, blen);
			sh->bytes_len = blen;
		} else {
			uint32_t cnt = kreq->count ? kreq->count : kinfo.count;

			if (cnt > 8)
				cnt = 8;
			for (uint32_t c = 0; c < cnt; c++) {
				int64_t v = kreq->values[c];

				if (kinfo.type == SOF_KCTL_TYPE_INTEGER && kinfo.max_val > 0) {
					if (v < kinfo.min_val)
						v = kinfo.min_val;
					if (v > kinfo.max_val)
						v = kinfo.max_val;
				}
				sh->values[c] = v;
			}
			/* Propagate live control change to the active SOF DSP component */
			char cmd_line[256];

			if (ctl->type == SND_SOC_TPLG_TYPE_MIXER) {
				if (kinfo.count >= 2)
					snprintf(cmd_line, sizeof(cmd_line),
						 "cset name=\"%s\" %lld,%lld",
						 ctl->name,
						 (long long)sh->values[0],
						 (long long)sh->values[1]);
				else
					snprintf(cmd_line, sizeof(cmd_line),
						 "cset name=\"%s\" %lld",
						 ctl->name,
						 (long long)sh->values[0]);
				tb_apply_control_line(g_tp, cmd_line);
			} else if (ctl->type == SND_SOC_TPLG_TYPE_ENUM) {
				uint32_t eidx = (uint32_t)sh->values[0];

				if (eidx < kinfo.num_enums) {
					snprintf(cmd_line, sizeof(cmd_line),
						 "cset name=\"%s\" %s",
						 ctl->name, kinfo.enum_names[eidx]);
					tb_apply_control_line(g_tp, cmd_line);
				}
			}
		}
		populate_tb_kctl_info(idx, ctl, &kinfo);
	}

	rsp.status = status;
	rsp.api_used = kreq->api_backend ? kreq->api_backend : SOF_AUDIO_API_ALSA;
	rsp.info = kinfo;
	rsp.bytes_len = g_kctl_shadow[idx].bytes_len;
	if (rsp.bytes_len > 0)
		memcpy(rsp.bytes_data, g_kctl_shadow[idx].bytes_data, rsp.bytes_len);

	pthread_mutex_unlock(&g_tb_lock);

	return send_reply(sock, opcode, req_id, status, &rsp, sizeof(rsp));
}

/* -------------------------------------------------------------------------
 * Per-Client Worker Thread & Accept Loop
 * -------------------------------------------------------------------------
 */

struct tb_client_ctx {
	int sock;
};

static void *tb_client_worker_fn(void *arg)
{
	struct tb_client_ctx *ctx = (struct tb_client_ctx *)arg;
	int sock = ctx->sock;
	int one = 1;

	free(ctx);
	g_in_remote_rpc = true;
	setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

	while (g_srv_running) {
		struct sof_net_hdr hdr;

		if (read_exact(sock, &hdr, sizeof(hdr)) < 0)
			break;

		if (hdr.magic != SOF_NET_MAGIC || hdr.version != SOF_NET_VERSION) {
			send_reply(sock, hdr.opcode, hdr.req_id, -EPROTO, NULL, 0);
			break;
		}

		if (hdr.payload_len > (16 * 1024 * 1024)) {
			send_reply(sock, hdr.opcode, hdr.req_id, -E2BIG, NULL, 0);
			break;
		}

		uint8_t *req_buf = NULL;

		if (hdr.payload_len > 0) {
			req_buf = malloc(hdr.payload_len + 1);
			if (!req_buf)
				break;
			if (read_exact(sock, req_buf, hdr.payload_len) < 0) {
				free(req_buf);
				break;
			}
			req_buf[hdr.payload_len] = '\0';
		}

		int rc = 0;

		switch (hdr.opcode) {
		case SOF_NET_OP_PING:
			rc = send_reply(sock, hdr.opcode, hdr.req_id, 0, "PONG", 4);
			break;

		case SOF_NET_OP_GET_INFO: {
			struct sof_cdev_info info;

			pthread_mutex_lock(&g_tb_lock);
			populate_cdev_info(g_tp, &info);
			pthread_mutex_unlock(&g_tb_lock);
			rc = send_reply(sock, hdr.opcode, hdr.req_id, 0, &info, sizeof(info));
			break;
		}

		case SOF_NET_OP_PM_CTL: {
			if (hdr.payload_len < sizeof(struct sof_cdev_pm_ctl)) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -EINVAL, NULL, 0);
				break;
			}
			struct sof_cdev_pm_ctl ctl;

			memcpy(&ctl, req_buf, sizeof(ctl));
			pthread_mutex_lock(&g_tb_lock);
			if (ctl.op == SOF_PM_OP_WAKE_HOLD || ctl.op == SOF_PM_OP_BOOT_DSP) {
				g_dsp_paused = false;
				pthread_cond_broadcast(&g_tb_cond);
			} else if (ctl.op == SOF_PM_OP_RELEASE_HOLD) {
				g_dsp_paused = true;
			} else if (ctl.op == SOF_PM_OP_CORE_GET && ctl.core_id < CONFIG_CORE_COUNT) {
				cpu_enable_core(ctl.core_id);
			} else if (ctl.op == SOF_PM_OP_CORE_PUT && ctl.core_id > 0 &&
				   ctl.core_id < CONFIG_CORE_COUNT) {
				cpu_disable_core(ctl.core_id);
			}
			ctl.fw_state = 7;
			ctl.dsp_power_state = g_dsp_paused ? 4 : 1;
			ctl.pm_runtime_active = g_dsp_paused ? 0 : 1;
			ctl.enabled_cores_mask = arch_cpu_enabled_cores();
			ctl.fd_holding_d0 = g_dsp_paused ? 0 : 1;
			pthread_mutex_unlock(&g_tb_lock);

			rc = send_reply(sock, hdr.opcode, hdr.req_id, 0, &ctl, sizeof(ctl));
			break;
		}

		case SOF_NET_OP_IPC_TX: {
			if (hdr.payload_len < sizeof(struct sof_net_ipc_tx_req)) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -EINVAL, NULL, 0);
				break;
			}
			const struct sof_net_ipc_tx_req *ireq =
				(const struct sof_net_ipc_tx_req *)req_buf;
			if (hdr.payload_len < sizeof(*ireq) + ireq->tx_size ||
			    ireq->rx_size > 65536) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -EINVAL, NULL, 0);
				break;
			}

			uint32_t rx_cap = ireq->rx_size <= SOF_IPC_MSG_MAX_SIZE - 8 ?
					  ireq->rx_size : 0;
			uint8_t *out_buf = calloc(1, sizeof(struct sof_net_ipc_tx_rsp) + rx_cap);

			if (!out_buf) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -ENOMEM, NULL, 0);
				break;
			}
			struct sof_net_ipc_tx_rsp *rsp = (struct sof_net_ipc_tx_rsp *)out_buf;

			pthread_mutex_lock(&g_tb_lock);
			if (sof_get() && sof_get()->ipc && sof_get()->ipc->comp_data) {
				uint32_t *mbox = (uint32_t *)sof_get()->ipc->comp_data;
				uint32_t msg_tgt = (ireq->primary >> 30) & 0x1;
				uint32_t msg_type = (ireq->primary >> 24) & 0x1F;

				/* Enable target core automatically on CREATE_PIPELINE / MOD_INIT */
				if (msg_tgt == SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG &&
				    msg_type == SOF_IPC4_GLB_CREATE_PIPELINE) {
					uint32_t core = (ireq->extension >> 20) & 0xF;

					if (core < CONFIG_CORE_COUNT)
						cpu_enable_core(core);
				} else if (msg_tgt == SOF_IPC4_MESSAGE_TARGET_MODULE_MSG &&
					   msg_type == SOF_IPC4_MOD_INIT_INSTANCE) {
					uint32_t core = (ireq->extension >> 12) & 0xF;

					if (core < CONFIG_CORE_COUNT)
						cpu_enable_core(core);
				}

				memset(mbox, 0, SOF_IPC_MSG_MAX_SIZE);
				mbox[0] = ireq->primary;
				mbox[1] = ireq->extension;
				if (ireq->tx_size > 0 &&
				    ireq->tx_size + 8 <= SOF_IPC_MSG_MAX_SIZE) {
					memcpy(&mbox[2], req_buf + sizeof(*ireq), ireq->tx_size);
				}
				ipc_cmd((struct ipc_cmd_hdr *)mbox);

				/* If SET_PIPELINE_STATE was sent, run one LL scheduler tick so
				 * the compound pipeline trigger task completes immediately.
				 */
				if (msg_tgt == SOF_IPC4_MESSAGE_TARGET_FW_GEN_MSG &&
				    msg_type == SOF_IPC4_GLB_SET_PIPELINE_STATE)
					schedule_ll_run_tasks();

				rsp->reply_primary = mbox[0];
				rsp->reply_extension = mbox[1];
				rsp->ipc_status = (int32_t)(mbox[0] & 0x00FFFFFFU);
				rsp->rx_size = rx_cap;
				if (rx_cap > 0)
					memcpy(out_buf + sizeof(*rsp), &mbox[2], rx_cap);
			}
			pthread_mutex_unlock(&g_tb_lock);

			rc = send_reply(sock, hdr.opcode, hdr.req_id, 0,
					out_buf, sizeof(*rsp) + rx_cap);
			free(out_buf);
			break;
		}

		case SOF_NET_OP_IPC_LARGE_CFG:
			rc = handle_ipc_large_cfg(sock, hdr.req_id, req_buf, hdr.payload_len);
			break;

		case SOF_NET_OP_WIN_INFO: {
			struct sof_cdev_info info;
			struct sof_cdev_win_info winfo;

			pthread_mutex_lock(&g_tb_lock);
			populate_cdev_info(g_tp, &info);
			pthread_mutex_unlock(&g_tb_lock);
			memcpy(winfo.windows, info.windows, sizeof(winfo.windows));
			memcpy(winfo.debug_slots, info.debug_slots, sizeof(winfo.debug_slots));
			rc = send_reply(sock, hdr.opcode, hdr.req_id, 0, &winfo, sizeof(winfo));
			break;
		}

		case SOF_NET_OP_WIN_READ: {
			if (hdr.payload_len < sizeof(struct sof_net_win_io_req)) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -EINVAL, NULL, 0);
				break;
			}
			const struct sof_net_win_io_req *wreq =
				(const struct sof_net_win_io_req *)req_buf;
			if (!wreq->size || wreq->size > 65536) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -EINVAL, NULL, 0);
				break;
			}
			uint8_t *rbuf = calloc(1, wreq->size);

			if (!rbuf) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -ENOMEM, NULL, 0);
				break;
			}

			pthread_mutex_lock(&g_tb_lock);
			refresh_emulated_windows(g_tp);
			const uint8_t *src_win = g_win0_fw_regs;
			size_t win_cap = sizeof(g_win0_fw_regs);

			if (wreq->win_id == SOF_CDEV_WIN_OUTBOX) {
				src_win = g_win1_outbox;
				win_cap = sizeof(g_win1_outbox);
			} else if (wreq->win_id == SOF_CDEV_WIN_DEBUG) {
				src_win = g_win2_debug;
				win_cap = sizeof(g_win2_debug);
			} else if (wreq->win_id == SOF_CDEV_WIN_PERF) {
				src_win = g_win3_perf;
				win_cap = sizeof(g_win3_perf);
			} else if (wreq->win_id == SOF_CDEV_WIN_BY_SLOT_TYPE) {
				if (wreq->slot_type == SOF_CDEV_SLOT_TELEMETRY) {
					src_win = g_win2_debug + 4096;
					win_cap = 4096;
				} else if (wreq->slot_type == SOF_CDEV_SLOT_SHELL) {
					src_win = g_win2_debug + 8192;
					win_cap = 4096;
				}
			}

			if (wreq->offset < win_cap) {
				size_t avail = win_cap - wreq->offset;
				size_t to_copy = wreq->size < avail ? wreq->size : avail;

				memcpy(rbuf, src_win + wreq->offset, to_copy);
			}
			pthread_mutex_unlock(&g_tb_lock);

			rc = send_reply(sock, hdr.opcode, hdr.req_id, 0, rbuf, wreq->size);
			free(rbuf);
			break;
		}

		case SOF_NET_OP_WIN_WRITE:
			rc = send_reply(sock, hdr.opcode, hdr.req_id, 0, NULL, 0);
			break;

		case SOF_NET_OP_WINSTREAM_IO: {
			if (hdr.payload_len < sizeof(struct sof_net_winstream_req)) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -EINVAL, NULL, 0);
				break;
			}
			const struct sof_net_winstream_req *wsreq =
				(const struct sof_net_winstream_req *)req_buf;
			if (wsreq->tx_len > 0 &&
			    hdr.payload_len >= sizeof(*wsreq) + wsreq->tx_len) {
				char cmd_line[256];
				char sh_out[4096];
				size_t clen = wsreq->tx_len < sizeof(cmd_line) - 1 ?
					      wsreq->tx_len : sizeof(cmd_line) - 1;

				memcpy(cmd_line, req_buf + sizeof(*wsreq), clen);
				cmd_line[clen] = '\0';
				/* Strip trailing CR/LF */
				while (clen > 0 && (cmd_line[clen - 1] == '\r' ||
						    cmd_line[clen - 1] == '\n'))
					cmd_line[--clen] = '\0';
				memset(sh_out, 0, sizeof(sh_out));
				execute_tb_shell_cmd(cmd_line, sh_out, sizeof(sh_out));
				g_shell_seq++;
			}

			size_t slen = strlen(g_last_shell_output);
			size_t rx_len = (wsreq->rx_buf_len > 0 && slen > 0) ?
					(slen < wsreq->rx_buf_len ? slen : wsreq->rx_buf_len) : 0;
			uint8_t *out_buf = calloc(1, sizeof(struct sof_net_winstream_rsp) + rx_len);

			if (!out_buf) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -ENOMEM, NULL, 0);
				break;
			}
			struct sof_net_winstream_rsp *wsrsp =
				(struct sof_net_winstream_rsp *)out_buf;
			wsrsp->rx_bytes_read = (uint32_t)rx_len;
			wsrsp->seq_out = g_shell_seq;
			if (rx_len > 0) {
				memcpy(out_buf + sizeof(*wsrsp), g_last_shell_output, rx_len);
				g_last_shell_output[0] = '\0';
			}
			rc = send_reply(sock, hdr.opcode, hdr.req_id, 0,
					out_buf, sizeof(*wsrsp) + (uint32_t)rx_len);
			free(out_buf);
			break;
		}

		case SOF_NET_OP_TELEM_SNAPSHOT:
			rc = handle_telem_snapshot(sock, hdr.req_id, req_buf, hdr.payload_len);
			break;

		case SOF_NET_OP_LLEXT_LOAD: {
			if (hdr.payload_len < sizeof(struct sof_net_llext_req)) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -EINVAL, NULL, 0);
				break;
			}
			const struct sof_net_llext_req *lreq =
				(const struct sof_net_llext_req *)req_buf;
			if (hdr.payload_len < sizeof(*lreq) + lreq->data_size) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -EINVAL, NULL, 0);
				break;
			}
			struct sof_net_llext_rsp rsp;
			uint32_t lid = lreq->lib_id ? lreq->lib_id : 1;
			uint32_t slot = (lid - 1) % TB_MAX_LOADED_LLEXTS;
			const uint8_t *mod_bytes = req_buf + sizeof(*lreq);
			char tmp_path[128];
			void *dl_h = NULL;

			memset(&rsp, 0, sizeof(rsp));
			rsp.lib_id = lid;
			rsp.fw_state = 7;

			snprintf(tmp_path, sizeof(tmp_path), "/tmp/sof_tb_llext_%u.so", lid);
			FILE *fp = fopen(tmp_path, "wb");

			if (fp) {
				fwrite(mod_bytes, 1, lreq->data_size, fp);
				fclose(fp);
				dl_h = dlopen(tmp_path, RTLD_NOW | RTLD_GLOBAL);
			}

			pthread_mutex_lock(&g_tb_lock);
			if (g_loaded_llexts[slot].dl_handle)
				dlclose(g_loaded_llexts[slot].dl_handle);
			g_loaded_llexts[slot].active = true;
			g_loaded_llexts[slot].lib_id = lid;
			g_loaded_llexts[slot].data_size = lreq->data_size;
			g_loaded_llexts[slot].dl_handle = dl_h;
			snprintf(g_loaded_llexts[slot].path, sizeof(g_loaded_llexts[slot].path),
				 "%s", tmp_path);
			snprintf(g_loaded_llexts[slot].name, sizeof(g_loaded_llexts[slot].name),
				 "llext%u", lid);
			pthread_mutex_unlock(&g_tb_lock);

			snprintf(rsp.shell_out, sizeof(rsp.shell_out),
				 "Testbench loaded module image (%u bytes) at lib_id %u (%s)\n",
				 lreq->data_size, rsp.lib_id,
				 dl_h ? "dlopen active" : "registered in LLEXT table");
			rc = send_reply(sock, hdr.opcode, hdr.req_id, 0, &rsp, sizeof(rsp));
			break;
		}

		case SOF_NET_OP_SHELL_EXEC: {
			if (hdr.payload_len < sizeof(struct sof_net_shell_req)) {
				rc = send_reply(sock, hdr.opcode, hdr.req_id, -EINVAL, NULL, 0);
				break;
			}
			const struct sof_net_shell_req *sreq =
				(const struct sof_net_shell_req *)req_buf;
			const char *cmd_str = (const char *)(req_buf + sizeof(*sreq));
			char out_text[8192];

			memset(out_text, 0, sizeof(out_text));
			int n = execute_tb_shell_cmd(cmd_str, out_text, sizeof(out_text));

			if (n < 0)
				rc = send_reply(sock, hdr.opcode, hdr.req_id, n, NULL, 0);
			else
				rc = send_reply(sock, hdr.opcode, hdr.req_id, 0,
						out_text, (uint32_t)n);
			break;
		}

		case SOF_NET_OP_PROBE_CTL:
			rc = handle_probe_ctl(sock, hdr.req_id, req_buf, hdr.payload_len);
			break;

		case SOF_NET_OP_PCM_CTL:
			rc = handle_pcm_ctl(sock, hdr.req_id, req_buf, hdr.payload_len);
			break;

		case SOF_NET_OP_CARD_LIST:
			rc = handle_card_list(sock, hdr.req_id);
			break;

		case SOF_NET_OP_PCM_CAPS:
			rc = handle_pcm_caps(sock, hdr.req_id, req_buf, hdr.payload_len);
			break;

		case SOF_NET_OP_DAPM_GRAPH:
			rc = handle_dapm_graph(sock, hdr.req_id, req_buf, hdr.payload_len);
			break;

		case SOF_NET_OP_COMPR_CTL:
			rc = handle_compr_ctl(sock, hdr.req_id, req_buf, hdr.payload_len);
			break;

		case SOF_NET_OP_KCTL_LIST:
			rc = handle_kctl_list(sock, hdr.req_id, req_buf, hdr.payload_len);
			break;

		case SOF_NET_OP_KCTL_GET:
		case SOF_NET_OP_KCTL_SET:
			rc = handle_kctl_io(sock, hdr.opcode, hdr.req_id, req_buf, hdr.payload_len);
			break;

		default:
			rc = send_reply(sock, hdr.opcode, hdr.req_id, -ENOSYS, NULL, 0);
			break;
		}

		free(req_buf);
		if (rc < 0)
			break;
	}

	close(sock);
	return NULL;
}

static void *tb_accept_worker_fn(void *arg)
{
	(void)arg;
	while (g_srv_running && g_srv_fd >= 0) {
		struct sockaddr_in cli_addr;
		socklen_t cli_len = sizeof(cli_addr);
		int cli_fd = accept(g_srv_fd, (struct sockaddr *)&cli_addr, &cli_len);

		if (cli_fd < 0) {
			if (errno == EINTR && g_srv_running)
				continue;
			break;
		}

		struct tb_client_ctx *ctx = malloc(sizeof(*ctx));

		if (!ctx) {
			close(cli_fd);
			continue;
		}
		ctx->sock = cli_fd;

		pthread_t tid;
		pthread_attr_t attr;

		pthread_attr_init(&attr);
		pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
		if (pthread_create(&tid, &attr, tb_client_worker_fn, ctx) != 0) {
			close(cli_fd);
			free(ctx);
		}
		pthread_attr_destroy(&attr);
	}
	return NULL;
}

int tb_remote_server_start(struct testbench_prm *tp)
{
	struct sockaddr_in bind_addr;
	int one = 1;
	int port;

	if (!tp || tp->server_port <= 0)
		return 0;

	port = tp->server_port;
	g_tp = tp;
	signal(SIGPIPE, SIG_IGN);

	g_srv_fd = socket(AF_INET, SOCK_STREAM, 0);
	if (g_srv_fd < 0) {
		perror("tb_remote_server: socket");
		return -errno;
	}

	setsockopt(g_srv_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

	memset(&bind_addr, 0, sizeof(bind_addr));
	bind_addr.sin_family = AF_INET;
	bind_addr.sin_addr.s_addr = htonl(INADDR_ANY);
	bind_addr.sin_port = htons((uint16_t)port);

	if (bind(g_srv_fd, (struct sockaddr *)&bind_addr, sizeof(bind_addr)) < 0) {
		perror("tb_remote_server: bind");
		close(g_srv_fd);
		g_srv_fd = -1;
		return -errno;
	}

	if (listen(g_srv_fd, 16) < 0) {
		perror("tb_remote_server: listen");
		close(g_srv_fd);
		g_srv_fd = -1;
		return -errno;
	}

	g_srv_running = true;
	g_quit_requested = false;
	if (pthread_create(&g_accept_thread, NULL, tb_accept_worker_fn, NULL) != 0) {
		g_srv_running = false;
		close(g_srv_fd);
		g_srv_fd = -1;
		return -EIO;
	}

	printf("sof-testbench4 remote server listening on 0.0.0.0:%d (SOFR protocol v%d)\n",
	       port, SOF_NET_VERSION);
	fflush(stdout);
	return 0;
}

void tb_remote_server_stop(void)
{
	if (!g_srv_running || g_in_remote_rpc)
		return;

	g_srv_running = false;
	g_dsp_paused = false;
	pthread_cond_broadcast(&g_tb_cond);

	if (g_srv_fd >= 0) {
		shutdown(g_srv_fd, SHUT_RDWR);
		close(g_srv_fd);
		g_srv_fd = -1;
	}

	pthread_join(g_accept_thread, NULL);
}

#endif /* !__XTENSA__ && !CONFIG_XT_SIMULATOR */
