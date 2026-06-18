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

#if defined(__ZEPHYR__) && defined(CONFIG_SOF_FULL_ZEPHYR_APPLICATION)

/** \brief Copy the current IPC statistics snapshot (wraps ipc_stats_get()). */
__syscall void sof_shell_ipc_stats_get(struct ipc_stats *out);

/** \brief Reset all IPC statistics counters (wraps ipc_stats_reset()). */
__syscall void sof_shell_ipc_stats_reset(void);

#else /* !__ZEPHYR__ || !CONFIG_SOF_FULL_ZEPHYR_APPLICATION */

void z_impl_sof_shell_ipc_stats_get(struct ipc_stats *out);
void z_impl_sof_shell_ipc_stats_reset(void);
#define sof_shell_ipc_stats_get z_impl_sof_shell_ipc_stats_get
#define sof_shell_ipc_stats_reset z_impl_sof_shell_ipc_stats_reset

#endif

#if defined(__ZEPHYR__) && defined(CONFIG_SOF_FULL_ZEPHYR_APPLICATION)
#include <zephyr/syscalls/sof_shell_syscall.h>
#endif

#endif /* __SOF_SOF_SHELL_SYSCALL_H__ */
