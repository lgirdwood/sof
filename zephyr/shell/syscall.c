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
#endif /* CONFIG_USERSPACE */
