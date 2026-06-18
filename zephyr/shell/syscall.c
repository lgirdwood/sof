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
#include <sof/sof_shell_syscall.h>

void z_impl_sof_shell_ipc_stats_get(struct ipc_stats *out)
{
	ipc_stats_get(out);
}

void z_impl_sof_shell_ipc_stats_reset(void)
{
	ipc_stats_reset();
}

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
#endif /* CONFIG_USERSPACE */
