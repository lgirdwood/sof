// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2026 Intel Corporation.

/*
 * System call implementations for the SOF kernel (RTOS/infrastructure) shell
 * commands. The z_impl_* functions run in supervisor context and forward to
 * the underlying privileged accessors. The z_vrfy_* handlers validate
 * user-supplied pointers before dispatching when the shell thread runs in
 * user mode (CONFIG_USERSPACE).
 */

#include <sof/ipc/common.h>
#include <sof/lib/cpu.h>
#include <rtos/clk.h>
#include <sof/sof_shell_syscall.h>

#if CONFIG_SOF_SHELL_SCHED_INFO
#include <sof/schedule/schedule.h>
#include <rtos/task.h>
#include <sof/list.h>
#endif

#if CONFIG_SOF_SHELL_LOG_INFO
#include <zephyr/logging/log_backend.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/logging/log_core.h>
#include <string.h>
#endif

#if CONFIG_SOF_SHELL_MMU_DBG && CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB
#include <zephyr/devicetree.h>
#include <zephyr/kernel/mm.h>
#include <zephyr/drivers/mm/system_mm.h>
#include <zephyr/sys/util.h>

/*
 * Lightweight wrappers around the Intel ADSP MTL TLB MMIO table. Mirrors
 * mm_drv_intel_adsp.h without pulling in the driver-internal header. These
 * accesses are privileged (MMIO and the memory-management driver) and so live
 * here behind system calls; the shell only consumes the decoded snapshot.
 */
#define _SHELL_TLB_NODE       DT_NODELABEL(tlb)
#define _SHELL_TLB_BASE       ((volatile uint16_t *)(uintptr_t)DT_REG_ADDR(_SHELL_TLB_NODE))
#define _SHELL_PADDR_SIZE     DT_PROP(_SHELL_TLB_NODE, paddr_size)
#define _SHELL_TLB_ENTRY_NUM  BIT(_SHELL_PADDR_SIZE)
#define _SHELL_PADDR_MASK     (_SHELL_TLB_ENTRY_NUM - 1)
#define _SHELL_ENABLE_BIT     ((uint16_t)BIT(_SHELL_PADDR_SIZE))

/*
 * Base physical address for the HPSRAM region (mirrors TLB_PHYS_BASE in the
 * driver).
 */
#define _SHELL_PHYS_BASE \
	(((CONFIG_KERNEL_VM_BASE / CONFIG_MM_DRV_PAGE_SIZE) & ~_SHELL_PADDR_MASK) * \
	 CONFIG_MM_DRV_PAGE_SIZE)
#endif /* CONFIG_SOF_SHELL_MMU_DBG && CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB */

void z_impl_sof_shell_ipc_stats_get(struct ipc_stats *out)
{
	ipc_stats_get(out);
}

void z_impl_sof_shell_ipc_stats_reset(void)
{
	ipc_stats_reset();
}

void z_impl_sof_shell_core_status_get(struct sof_shell_core_status *out)
{
	unsigned int i;

	out->core_count = CONFIG_CORE_COUNT;
	out->current = cpu_get_id();
	for (i = 0; i < CONFIG_CORE_COUNT; i++)
		out->enabled[i] = cpu_is_core_enabled(i) ? 1 : 0;
}

void z_impl_sof_shell_clock_status_get(struct sof_shell_clock_status *out)
{
	struct clock_info *clocks = clocks_get();
	unsigned int i;

	if (!clocks) {
		out->valid = 0;
		out->num_clocks = 0;
		return;
	}

	out->valid = 1;
	out->num_clocks = NUM_CLOCKS;
	for (i = 0; i < NUM_CLOCKS && i < CONFIG_CORE_COUNT; i++)
		out->freq_hz[i] = clocks[i].freqs[clocks[i].current_freq_idx].freq;
}

#if CONFIG_SOF_SHELL_SCHED_INFO
static void sof_shell_sched_cb(struct task *task, void *_ctx)
{
	struct sof_shell_sched_snapshot *out = _ctx;
	struct sof_shell_sched_task *e;

	if (out->count >= SOF_SHELL_SCHED_MAX_TASKS)
		return;

	e = &out->tasks[out->count++];
	e->sch_type = task->sch ? task->sch->type : 0;
	e->core = task->core;
	e->priority = task->priority;
	e->state = task->state;
	e->flags = task->flags;
	e->cycles_cnt = task->cycles_cnt;
	e->cycles_sum = task->cycles_sum;
	e->cycles_max = task->cycles_max;
	e->uid = (uintptr_t)task->uid;
	e->data = (uintptr_t)task->data;
}

void z_impl_sof_shell_sched_snapshot_get(struct sof_shell_sched_snapshot *out)
{
	struct schedulers *schedulers = *arch_schedulers_get();
	struct schedule_data *sch;
	struct list_item *slist;

	out->count = 0;
	out->no_schedulers = 0;

	if (!schedulers) {
		out->no_schedulers = 1;
		return;
	}

	list_for_item(slist, &schedulers->list) {
		sch = container_of(slist, struct schedule_data, list);
		if (!sch->ops->scheduler_dump_tasks)
			continue;
		sch->ops->scheduler_dump_tasks(sch->data, sof_shell_sched_cb, out);
	}
}
#endif /* CONFIG_SOF_SHELL_SCHED_INFO */

#if CONFIG_SOF_SHELL_LOG_INFO
void z_impl_sof_shell_log_status_get(struct sof_shell_log_status *out)
{
	int n = log_backend_count_get();
	int i;

	out->backend_count = n;
	out->source_count = log_src_cnt_get(Z_LOG_LOCAL_DOMAIN_ID);
	out->filled = 0;

	for (i = 0; i < n && out->filled < SOF_SHELL_LOG_MAX_BACKENDS; i++) {
		const struct log_backend *be = log_backend_get(i);
		struct sof_shell_log_backend *e;
		const char *name;

		if (!be)
			continue;

		e = &out->backends[out->filled++];
		e->id = log_backend_id_get(be);
		e->active = log_backend_is_active(be) ? 1 : 0;
		name = be->name ? be->name : "?";
		strncpy(e->name, name, SOF_SHELL_LOG_NAME_MAX - 1);
		e->name[SOF_SHELL_LOG_NAME_MAX - 1] = '\0';
	}
}
#endif /* CONFIG_SOF_SHELL_LOG_INFO */

#if CONFIG_SOF_SHELL_MMU_DBG && CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB
void z_impl_sof_shell_tlb_meta_get(struct sof_shell_tlb_meta *out)
{
	volatile uint16_t *tlb = _SHELL_TLB_BASE;
	uint32_t total = _SHELL_TLB_ENTRY_NUM;
	const struct sys_mm_drv_region *regions, *r;
	uint32_t enabled = 0;
	uint32_t i;

	for (i = 0; i < total; i++) {
		if (tlb[i] & _SHELL_ENABLE_BIT)
			enabled++;
	}

	out->vm_base = CONFIG_KERNEL_VM_BASE;
	out->page_size = CONFIG_MM_DRV_PAGE_SIZE;
	out->total_entries = total;
	out->enabled_entries = enabled;
	out->tlb_mmio_base = (uint32_t)(uintptr_t)_SHELL_TLB_BASE;
	out->phys_base = (uint32_t)_SHELL_PHYS_BASE;
	out->paddr_size = _SHELL_PADDR_SIZE;
	out->exec_bit_idx = DT_PROP(_SHELL_TLB_NODE, exec_bit_idx);
	out->write_bit_idx = DT_PROP(_SHELL_TLB_NODE, write_bit_idx);
	out->region_count = 0;
	out->regions_truncated = 0;

	regions = sys_mm_drv_query_memory_regions();
	if (regions) {
		SYS_MM_DRV_MEMORY_REGION_FOREACH(regions, r) {
			struct sof_shell_mm_region *e;

			if (out->region_count >= SOF_SHELL_TLB_MAX_REGIONS) {
				out->regions_truncated = 1;
				break;
			}
			e = &out->regions[out->region_count++];
			e->addr = (uint32_t)(uintptr_t)r->addr;
			e->size = (uint32_t)r->size;
			e->attr = (uint32_t)r->attr;
		}
		sys_mm_drv_query_memory_regions_free(regions);
	}
}

uint32_t z_impl_sof_shell_tlb_entries_get(uint32_t start, uint32_t count,
					  uint16_t *out)
{
	volatile uint16_t *tlb = _SHELL_TLB_BASE;
	uint32_t total = _SHELL_TLB_ENTRY_NUM;
	uint32_t i;

	if (start >= total)
		return 0;
	if (count > total - start)
		count = total - start;

	for (i = 0; i < count; i++)
		out[i] = tlb[start + i];

	return count;
}
#endif /* CONFIG_SOF_SHELL_MMU_DBG && CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB */

#ifdef CONFIG_USERSPACE
#include <zephyr/internal/syscall_handler.h>

void z_vrfy_sof_shell_ipc_stats_get(struct ipc_stats *out)
{
	K_OOPS(K_SYSCALL_MEMORY_WRITE(out, sizeof(*out)));
	z_impl_sof_shell_ipc_stats_get(out);
}
#include <zephyr/syscalls/sof_shell_ipc_stats_get_mrsh.c>

void z_vrfy_sof_shell_ipc_stats_reset(void)
{
	z_impl_sof_shell_ipc_stats_reset();
}
#include <zephyr/syscalls/sof_shell_ipc_stats_reset_mrsh.c>

void z_vrfy_sof_shell_core_status_get(struct sof_shell_core_status *out)
{
	K_OOPS(K_SYSCALL_MEMORY_WRITE(out, sizeof(*out)));
	z_impl_sof_shell_core_status_get(out);
}
#include <zephyr/syscalls/sof_shell_core_status_get_mrsh.c>

void z_vrfy_sof_shell_clock_status_get(struct sof_shell_clock_status *out)
{
	K_OOPS(K_SYSCALL_MEMORY_WRITE(out, sizeof(*out)));
	z_impl_sof_shell_clock_status_get(out);
}
#include <zephyr/syscalls/sof_shell_clock_status_get_mrsh.c>

#if CONFIG_SOF_SHELL_SCHED_INFO
void z_vrfy_sof_shell_sched_snapshot_get(struct sof_shell_sched_snapshot *out)
{
	K_OOPS(K_SYSCALL_MEMORY_WRITE(out, sizeof(*out)));
	z_impl_sof_shell_sched_snapshot_get(out);
}
#include <zephyr/syscalls/sof_shell_sched_snapshot_get_mrsh.c>
#endif /* CONFIG_SOF_SHELL_SCHED_INFO */

#if CONFIG_SOF_SHELL_LOG_INFO
void z_vrfy_sof_shell_log_status_get(struct sof_shell_log_status *out)
{
	K_OOPS(K_SYSCALL_MEMORY_WRITE(out, sizeof(*out)));
	z_impl_sof_shell_log_status_get(out);
}
#include <zephyr/syscalls/sof_shell_log_status_get_mrsh.c>
#endif /* CONFIG_SOF_SHELL_LOG_INFO */

#if CONFIG_SOF_SHELL_MMU_DBG && CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB
void z_vrfy_sof_shell_tlb_meta_get(struct sof_shell_tlb_meta *out)
{
	K_OOPS(K_SYSCALL_MEMORY_WRITE(out, sizeof(*out)));
	z_impl_sof_shell_tlb_meta_get(out);
}
#include <zephyr/syscalls/sof_shell_tlb_meta_get_mrsh.c>

uint32_t z_vrfy_sof_shell_tlb_entries_get(uint32_t start, uint32_t count,
					  uint16_t *out)
{
	K_OOPS(K_SYSCALL_MEMORY_ARRAY_WRITE(out, count, sizeof(uint16_t)));
	return z_impl_sof_shell_tlb_entries_get(start, count, out);
}
#include <zephyr/syscalls/sof_shell_tlb_entries_get_mrsh.c>
#endif /* CONFIG_SOF_SHELL_MMU_DBG && CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB */
#endif /* CONFIG_USERSPACE */
