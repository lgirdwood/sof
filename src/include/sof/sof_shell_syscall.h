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

/** \brief Copy the current IPC statistics snapshot (wraps ipc_stats_get()). */
__syscall void sof_shell_ipc_stats_get(struct ipc_stats *out);

/** \brief Reset all IPC statistics counters (wraps ipc_stats_reset()). */
__syscall void sof_shell_ipc_stats_reset(void);

/** \brief Copy a snapshot of per-core enabled/current state. */
__syscall void sof_shell_core_status_get(struct sof_shell_core_status *out);

/** \brief Copy a snapshot of the current per-clock frequencies. */
__syscall void sof_shell_clock_status_get(struct sof_shell_clock_status *out);

#else /* !__ZEPHYR__ || !CONFIG_SOF_FULL_ZEPHYR_APPLICATION */

struct sof_shell_core_status;
struct sof_shell_clock_status;

void z_impl_sof_shell_ipc_stats_get(struct ipc_stats *out);
void z_impl_sof_shell_ipc_stats_reset(void);
void z_impl_sof_shell_core_status_get(struct sof_shell_core_status *out);
void z_impl_sof_shell_clock_status_get(struct sof_shell_clock_status *out);
#define sof_shell_ipc_stats_get z_impl_sof_shell_ipc_stats_get
#define sof_shell_ipc_stats_reset z_impl_sof_shell_ipc_stats_reset
#define sof_shell_core_status_get z_impl_sof_shell_core_status_get
#define sof_shell_clock_status_get z_impl_sof_shell_clock_status_get

#endif

#if defined(__ZEPHYR__) && defined(CONFIG_SOF_FULL_ZEPHYR_APPLICATION)
#include <zephyr/syscalls/sof_shell_syscall.h>
#endif

#endif /* __SOF_SOF_SHELL_SYSCALL_H__ */
