/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2026 Intel Corporation.
 */

/**
 * \file
 * \brief System call wrappers for privileged data accessed by the kernel
 *        (RTOS/infrastructure) SOF shell commands.
 *
 * The kernel shell commands in zephyr/shell/kernel.c read privileged global
 * state (IPC counters, scheduler info, ...). When the shell command thread
 * runs as a Zephyr user-mode thread (CONFIG_SOF_SHELL_USERSPACE) these
 * accessors must be reached through system calls. On supervisor-only builds
 * the wrappers compile to direct calls to the z_impl_* implementations with no
 * overhead.
 */

#ifndef __SOF_SOF_SHELL_SYSCALL_H__
#define __SOF_SOF_SHELL_SYSCALL_H__

#include <sof/ipc/common.h>
#include <stdint.h>

#if defined(__ZEPHYR__) && defined(CONFIG_SOF_FULL_ZEPHYR_APPLICATION)

/** \brief Snapshot of per-core enabled state for "sof core_status". */
struct sof_shell_core_status {
	uint32_t core_count;			/* number of valid entries in enabled[] */
	uint32_t current;			/* id of the core servicing the request */
	uint8_t enabled[CONFIG_CORE_COUNT];	/* 1 if core is enabled, 0 otherwise */
};

/** \brief Snapshot of current clock frequencies for "sof clock_status". */
struct sof_shell_clock_status {
	uint32_t valid;				/* 0 if clock info is unavailable */
	uint32_t num_clocks;			/* number of valid entries in freq_hz[] */
	uint32_t freq_hz[CONFIG_CORE_COUNT];	/* current frequency of each clock */
};

/** \brief Maximum number of scheduler tasks captured in one snapshot. */
#define SOF_SHELL_SCHED_MAX_TASKS 48

/** \brief Per-task snapshot entry for "sof sched_tasks"/"sof sched_load". */
struct sof_shell_sched_task {
	uint32_t sch_type;	/* SOF_SCHEDULE_ type of the owning scheduler */
	uint32_t core;
	uint32_t priority;
	uint32_t state;		/* enum task_state */
	uint32_t flags;
	uint32_t cycles_cnt;
	uint32_t cycles_sum;
	uint32_t cycles_max;
	uintptr_t uid;		/* opaque uuid pointer, printed as a value */
	uintptr_t data;		/* opaque task data pointer, printed as a value */
};

/** \brief Snapshot of the scheduler task list for the sched shell commands. */
struct sof_shell_sched_snapshot {
	uint32_t no_schedulers;			/* 1 if no schedulers are registered */
	uint32_t count;				/* number of valid entries in tasks[] */
	struct sof_shell_sched_task tasks[SOF_SHELL_SCHED_MAX_TASKS];
};

/** \brief Maximum number of log backends captured in one snapshot. */
#define SOF_SHELL_LOG_MAX_BACKENDS 8
/** \brief Maximum stored length (including NUL) of a log backend name. */
#define SOF_SHELL_LOG_NAME_MAX 48

/** \brief Per-backend snapshot entry for "sof log_status". */
struct sof_shell_log_backend {
	uint32_t id;
	uint32_t active;			/* 1 if the backend is active */
	char name[SOF_SHELL_LOG_NAME_MAX];
};

/** \brief Snapshot of the logging subsystem state for "sof log_status". */
struct sof_shell_log_status {
	uint32_t backend_count;			/* total backends reported by the core */
	uint32_t source_count;			/* number of log sources */
	uint32_t filled;			/* number of valid entries in backends[] */
	struct sof_shell_log_backend backends[SOF_SHELL_LOG_MAX_BACKENDS];
};

/** \brief Maximum number of mapped memory regions captured in a TLB snapshot. */
#define SOF_SHELL_TLB_MAX_REGIONS 16

/** \brief One mapped memory region entry for "sof mmu_status". */
struct sof_shell_mm_region {
	uint32_t addr;
	uint32_t size;
	uint32_t attr;
};

/**
 * \brief Snapshot of TLB / virtual-memory metadata for the MMU shell commands.
 *
 * All values are read from privileged TLB MMIO and the memory-management
 * driver; the decoding (per-entry flags, paddr) is done by the shell using the
 * bit indices and base addresses reported here.
 */
struct sof_shell_tlb_meta {
	uint32_t vm_base;		/* virtual memory base address */
	uint32_t page_size;		/* page size in bytes */
	uint32_t total_entries;		/* number of TLB entries */
	uint32_t enabled_entries;	/* number of active (mapped) TLB entries */
	uint32_t tlb_mmio_base;		/* TLB MMIO table base address */
	uint32_t phys_base;		/* base physical address for the region */
	uint32_t paddr_size;		/* paddr field width; enable bit = BIT(paddr_size) */
	uint32_t exec_bit_idx;		/* index of the exec permission bit */
	uint32_t write_bit_idx;		/* index of the write permission bit */
	uint32_t region_count;		/* number of valid entries in regions[] */
	uint32_t regions_truncated;	/* 1 if more regions exist than fit */
	struct sof_shell_mm_region regions[SOF_SHELL_TLB_MAX_REGIONS];
};

/** \brief Copy the current IPC statistics snapshot (wraps ipc_stats_get()). */
__syscall void sof_shell_ipc_stats_get(struct ipc_stats *out);

/** \brief Reset all IPC statistics counters (wraps ipc_stats_reset()). */
__syscall void sof_shell_ipc_stats_reset(void);

/** \brief Copy a snapshot of per-core enabled/current state. */
__syscall void sof_shell_core_status_get(struct sof_shell_core_status *out);

/** \brief Copy a snapshot of the current per-clock frequencies. */
__syscall void sof_shell_clock_status_get(struct sof_shell_clock_status *out);

/** \brief Copy a snapshot of the scheduler task list. */
__syscall void sof_shell_sched_snapshot_get(struct sof_shell_sched_snapshot *out);

/** \brief Copy a snapshot of the logging subsystem backends. */
__syscall void sof_shell_log_status_get(struct sof_shell_log_status *out);

/** \brief Copy TLB/VM metadata and the mapped memory region list. */
__syscall void sof_shell_tlb_meta_get(struct sof_shell_tlb_meta *out);

/**
 * \brief Copy a range of raw 16-bit TLB entries into a user buffer.
 * \param start index of the first entry to copy.
 * \param count number of entries requested.
 * \param out   buffer receiving up to \p count entries.
 * \return number of entries actually copied (clamped to the valid range).
 */
__syscall uint32_t sof_shell_tlb_entries_get(uint32_t start, uint32_t count,
					     uint16_t *out);

#else /* !__ZEPHYR__ || !CONFIG_SOF_FULL_ZEPHYR_APPLICATION */

struct sof_shell_core_status;
struct sof_shell_clock_status;
struct sof_shell_sched_snapshot;
struct sof_shell_log_status;
struct sof_shell_tlb_meta;

void z_impl_sof_shell_ipc_stats_get(struct ipc_stats *out);
void z_impl_sof_shell_ipc_stats_reset(void);
void z_impl_sof_shell_core_status_get(struct sof_shell_core_status *out);
void z_impl_sof_shell_clock_status_get(struct sof_shell_clock_status *out);
void z_impl_sof_shell_sched_snapshot_get(struct sof_shell_sched_snapshot *out);
void z_impl_sof_shell_log_status_get(struct sof_shell_log_status *out);
void z_impl_sof_shell_tlb_meta_get(struct sof_shell_tlb_meta *out);
uint32_t z_impl_sof_shell_tlb_entries_get(uint32_t start, uint32_t count,
					  uint16_t *out);
#define sof_shell_ipc_stats_get z_impl_sof_shell_ipc_stats_get
#define sof_shell_ipc_stats_reset z_impl_sof_shell_ipc_stats_reset
#define sof_shell_core_status_get z_impl_sof_shell_core_status_get
#define sof_shell_clock_status_get z_impl_sof_shell_clock_status_get
#define sof_shell_sched_snapshot_get z_impl_sof_shell_sched_snapshot_get
#define sof_shell_log_status_get z_impl_sof_shell_log_status_get
#define sof_shell_tlb_meta_get z_impl_sof_shell_tlb_meta_get
#define sof_shell_tlb_entries_get z_impl_sof_shell_tlb_entries_get

#endif

#if defined(__ZEPHYR__) && defined(CONFIG_SOF_FULL_ZEPHYR_APPLICATION)
#include <zephyr/syscalls/sof_shell_syscall.h>
#endif

#endif /* __SOF_SOF_SHELL_SYSCALL_H__ */
