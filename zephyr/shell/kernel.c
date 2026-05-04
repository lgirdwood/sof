// SPDX-License-Identifier: BSD-3-Clause
/*
 * Copyright(c) 2024 Intel Corporation.
 *
 * Author: Kai Vehmanen <kai.vehmanen@linux.intel.com>
 */

/*
 * SOF shell - RTOS / firmware infrastructure commands.
 *
 * This file hosts the root "sof" shell command and all commands that deal
 * with RTOS and low level firmware facilities (scheduling, memory, cores,
 * IPC transport, logging, debug windows, library/llext management, ...).
 * Audio domain commands (pipelines, modules, buffers, DAI/DMA, kcontrols)
 * live in shell/user.c and are attached to the same "sof" root via the
 * shell iterable-section subcommand mechanism.
 */

#include <rtos/sof.h> /* sof_get() */
#include <sof/schedule/ll_schedule_domain.h>
#include <sof/lib/cpu.h>
#include <sof/lib/memory.h>
#include <rtos/clk.h>
#include <rtos/alloc.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/shell/shell.h>
#if CONFIG_SYS_HEAP_RUNTIME_STATS
#include <zephyr/sys/sys_heap.h>
#endif

#include <stdlib.h>

/*
 * Root "sof" command set. The set is created here and shared with shell/user.c
 * through the shell iterable-section mechanism so both translation units can
 * register top level subcommands under "sof" using SHELL_SUBCMD_ADD((sof), ...).
 * The command itself is registered at the bottom of this file.
 */
SHELL_SUBCMD_SET_CREATE(sub_sof, (sof));

#define SOF_TEST_INJECT_SCHED_GAP_USEC 1500

__cold static int cmd_sof_test_inject_sched_gap(const struct shell *sh,
						size_t argc, char *argv[])
{
	uint32_t block_time = SOF_TEST_INJECT_SCHED_GAP_USEC;
	char *endptr = NULL;

#ifndef CONFIG_CROSS_CORE_STREAM
	shell_fprintf(sh, SHELL_NORMAL, "Domain blocking not supported, not reliable on SMP\n");
#endif

	domain_block(sof_get()->platform_timer_domain);

	if (argc > 1) {
		block_time = strtol(argv[1], &endptr, 0);
		if (endptr == argv[1])
			return -EINVAL;
	}

	k_busy_wait(block_time);

	domain_unblock(sof_get()->platform_timer_domain);

	return 0;
}

SHELL_SUBCMD_ADD((sof), test_inject_sched_gap, NULL,
		 "Inject a gap to audio scheduling\n",
		 cmd_sof_test_inject_sched_gap, 0, 0);

#if CONFIG_SOF_SHELL_CORE_STATUS
__cold static int cmd_sof_core_status(const struct shell *sh,
				      size_t argc, char *argv[])
{
	int i;

	shell_print(sh, "%-6s %-8s %s", "core", "enabled", "current");

	for (i = 0; i < CONFIG_CORE_COUNT; i++) {
		shell_print(sh, "%-6d %-8s %s",
			    i,
			    cpu_is_core_enabled(i) ? "yes" : "no",
			    (i == cpu_get_id()) ? "<--" : "");
	}

	return 0;
}

SHELL_SUBCMD_ADD((sof), core_status, NULL,
		 "Print enabled/active state of each DSP core\n",
		 cmd_sof_core_status, 0, 0);
#endif /* CONFIG_SOF_SHELL_CORE_STATUS */

#if CONFIG_SOF_SHELL_SRAM_STATUS
__cold static int cmd_sof_sram_status(const struct shell *sh,
				      size_t argc, char *argv[])
{
	struct k_heap *h = sof_sys_heap_get();
	struct sys_memory_stats stats;

	if (!h) {
		shell_print(sh, "Heap not available");
		return 0;
	}

	sys_heap_runtime_stats_get(&h->heap, &stats);

	shell_print(sh, "HPSRAM heap (sof_heap):");
	shell_print(sh, "  allocated:    %zu bytes", stats.allocated_bytes);
	shell_print(sh, "  free:         %zu bytes", stats.free_bytes);
	shell_print(sh, "  max allocated:%zu bytes", stats.max_allocated_bytes);
	shell_print(sh, "  total:        %zu bytes",
		    stats.allocated_bytes + stats.free_bytes);

	return 0;
}

SHELL_SUBCMD_ADD((sof), sram_status, NULL,
		 "Print HPSRAM heap usage statistics\n",
		 cmd_sof_sram_status, 0, 0);
#endif /* CONFIG_SOF_SHELL_SRAM_STATUS */

#if CONFIG_SOF_SHELL_CLOCK_STATUS
__cold static int cmd_sof_clock_status(const struct shell *sh,
				       size_t argc, char *argv[])
{
	struct clock_info *clocks = clocks_get();
	int i;

	if (!clocks) {
		shell_print(sh, "Clock info not available");
		return 0;
	}

	shell_print(sh, "%-6s %-12s %s", "clock", "freq_hz", "freq_mhz");

	for (i = 0; i < NUM_CLOCKS; i++) {
		uint32_t freq = clocks[i].freqs[clocks[i].current_freq_idx].freq;

		shell_print(sh, "%-6d %-12u %.1f",
			    i, freq, (double)freq / 1000000.0);
	}

	return 0;
}

SHELL_SUBCMD_ADD((sof), clock_status, NULL,
		 "Print current clock frequency for each DSP core\n",
		 cmd_sof_clock_status, 0, 0);
#endif /* CONFIG_SOF_SHELL_CLOCK_STATUS */

SHELL_CMD_REGISTER(sof, &sub_sof,
		   "SOF application commands", NULL);
