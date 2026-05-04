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
#if CONFIG_SOF_SHELL_MMU_DBG
#include <zephyr/devicetree.h>
#include <zephyr/kernel/mm.h>
#include <zephyr/drivers/mm/system_mm.h>
#if CONFIG_XTENSA_MMU
#include <zephyr/arch/xtensa/xtensa_mmu.h>
#endif /* CONFIG_XTENSA_MMU */
#endif /* CONFIG_SOF_SHELL_MMU_DBG */

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


#if CONFIG_SOF_SHELL_MMU_DBG

#if CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB

/*
 * Lightweight wrappers around the Intel ADSP MTL TLB MMIO table.
 * Mirrors mm_drv_intel_adsp.h without pulling in the driver-internal header.
 */
#define _SHELL_TLB_NODE       DT_NODELABEL(tlb)
#define _SHELL_TLB_BASE       ((volatile uint16_t *)(uintptr_t)DT_REG_ADDR(_SHELL_TLB_NODE))
#define _SHELL_PADDR_SIZE     DT_PROP(_SHELL_TLB_NODE, paddr_size)
#define _SHELL_TLB_ENTRY_NUM  BIT(_SHELL_PADDR_SIZE)
#define _SHELL_PADDR_MASK     (_SHELL_TLB_ENTRY_NUM - 1)
#define _SHELL_ENABLE_BIT     ((uint16_t)BIT(_SHELL_PADDR_SIZE))
#define _SHELL_EXEC_BIT       ((uint16_t)BIT(DT_PROP(_SHELL_TLB_NODE, exec_bit_idx)))
#define _SHELL_WRITE_BIT      ((uint16_t)BIT(DT_PROP(_SHELL_TLB_NODE, write_bit_idx)))

/*
 * Base physical address for the HPSRAM region (mirrors TLB_PHYS_BASE in the
 * driver).  Physical pages whose index fits in paddr_size bits are located
 * starting here.
 */
#define _SHELL_PHYS_BASE \
	(((CONFIG_KERNEL_VM_BASE / CONFIG_MM_DRV_PAGE_SIZE) & ~_SHELL_PADDR_MASK) * \
	 CONFIG_MM_DRV_PAGE_SIZE)

/* Convert virtual-address index → physical address */
__cold static uintptr_t shell_tlb_idx_to_pa(uint32_t idx, uint16_t entry)
{
	return _SHELL_PHYS_BASE +
	       ((uintptr_t)(entry & _SHELL_PADDR_MASK) * CONFIG_MM_DRV_PAGE_SIZE);
}

/* Decode 16-bit TLB entry permission bits into a short string */
__cold static void shell_tlb_flags_str(uint16_t entry, char *buf)
{
	buf[0] = 'R';
	buf[1] = (entry & _SHELL_WRITE_BIT) ? 'W' : '-';
	buf[2] = (entry & _SHELL_EXEC_BIT)  ? 'X' : '-';
	buf[3] = '\0';
}

/* sof mmu_status */
__cold static int cmd_sof_mmu_status(const struct shell *sh,
				     size_t argc, char *argv[])
{
	const struct sys_mm_drv_region *regions, *r;
	volatile uint16_t *tlb = _SHELL_TLB_BASE;
	uint32_t total = _SHELL_TLB_ENTRY_NUM;
	uint32_t enabled = 0;
	uint32_t i;

	/* Count active TLB entries */
	for (i = 0; i < total; i++) {
		if (tlb[i] & _SHELL_ENABLE_BIT)
			enabled++;
	}

	shell_print(sh, "Intel ADSP MTL TLB / Virtual Memory Status");
	shell_print(sh, "  VM base:       0x%08x", CONFIG_KERNEL_VM_BASE);
	shell_print(sh, "  VM size:       0x%08x (%u KB)",
		    (uint32_t)(total * CONFIG_MM_DRV_PAGE_SIZE),
		    (uint32_t)(total * CONFIG_MM_DRV_PAGE_SIZE / 1024));
	shell_print(sh, "  page size:     %u B", CONFIG_MM_DRV_PAGE_SIZE);
	shell_print(sh, "  total entries: %u", total);
	shell_print(sh, "  mapped pages:  %u (%u KB)",
		    enabled, enabled * CONFIG_MM_DRV_PAGE_SIZE / 1024);
	shell_print(sh, "  free pages:    %u (%u KB)",
		    total - enabled,
		    (total - enabled) * CONFIG_MM_DRV_PAGE_SIZE / 1024);
	shell_print(sh, "  TLB MMIO base: 0x%08x",
		    (uint32_t)(uintptr_t)_SHELL_TLB_BASE);
	shell_print(sh, "  paddr_size:    %u  enable_bit:%u  exec_bit:%u  write_bit:%u",
		    _SHELL_PADDR_SIZE,
		    _SHELL_PADDR_SIZE,
		    DT_PROP(_SHELL_TLB_NODE, exec_bit_idx),
		    DT_PROP(_SHELL_TLB_NODE, write_bit_idx));

	shell_print(sh, "");
	shell_print(sh, "Mapped memory regions (sys_mm_drv):");
	shell_print(sh, "  %-10s  %-10s  %s", "address", "size", "attr");

	regions = sys_mm_drv_query_memory_regions();
	if (regions) {
		SYS_MM_DRV_MEMORY_REGION_FOREACH(regions, r) {
			shell_print(sh, "  0x%08x  0x%08x  0x%08x",
				    (uint32_t)(uintptr_t)r->addr,
				    (uint32_t)r->size,
				    (uint32_t)r->attr);
		}
		sys_mm_drv_query_memory_regions_free(regions);
	} else {
		shell_print(sh, "  (not available)");
	}
	return 0;
}

/* sof tlb_dump */
__cold static int cmd_sof_tlb_dump(const struct shell *sh,
				   size_t argc, char *argv[])
{
	volatile uint16_t *tlb = _SHELL_TLB_BASE;
	uint32_t total = _SHELL_TLB_ENTRY_NUM;
	uint32_t count = 0;
	uint32_t i;

	shell_print(sh, "  idx   vaddr       paddr       flags  entry");

	for (i = 0; i < total; i++) {
		uint16_t entry = tlb[i];

		if (!(entry & _SHELL_ENABLE_BIT))
			continue;

		uintptr_t vaddr = CONFIG_KERNEL_VM_BASE +
				  (uintptr_t)i * CONFIG_MM_DRV_PAGE_SIZE;
		uintptr_t paddr = shell_tlb_idx_to_pa(i, entry);
		char flags[4];

		shell_tlb_flags_str(entry, flags);
		shell_print(sh, "  %-5u 0x%08x  0x%08x  %s    0x%04x",
			    i, (uint32_t)vaddr, (uint32_t)paddr,
			    flags, (uint32_t)entry);
		count++;
	}

	shell_print(sh, "Total: %u/%u entries active", count, total);
	return 0;
}

/* sof tlb_lookup <vaddr> [end_vaddr] */
__cold static int cmd_sof_tlb_lookup(const struct shell *sh,
				     size_t argc, char *argv[])
{
	volatile uint16_t *tlb = _SHELL_TLB_BASE;
	uintptr_t vstart, vend;

	/* Parse start address */
	{
		char *ep;
		ulong v = strtoul(argv[1], &ep, 0);

		if (ep == argv[1]) {
			shell_print(sh, "error: invalid address '%s'", argv[1]);
			return -EINVAL;
		}
		vstart = (uintptr_t)v & ~(CONFIG_MM_DRV_PAGE_SIZE - 1);
	}

	if (argc > 2) {
		char *ep;
		ulong v = strtoul(argv[2], &ep, 0);

		if (ep == argv[2]) {
			shell_print(sh, "error: invalid address '%s'", argv[2]);
			return -EINVAL;
		}
		vend = (uintptr_t)v & ~(CONFIG_MM_DRV_PAGE_SIZE - 1);
	} else {
		vend = vstart;
	}

	if (vend < vstart)
		vend = vstart;

	shell_print(sh, "  vaddr       paddr       mapped  flags  bank  entry");

	for (uintptr_t va = vstart; va <= vend; va += CONFIG_MM_DRV_PAGE_SIZE) {
		uintptr_t vm_base = CONFIG_KERNEL_VM_BASE;
		uintptr_t vm_end  = vm_base +
				    (uintptr_t)_SHELL_TLB_ENTRY_NUM *
				    CONFIG_MM_DRV_PAGE_SIZE - 1;

		if (va < vm_base || va > vm_end) {
			shell_print(sh, "  0x%08x  (outside VM range)",
				    (uint32_t)va);
			continue;
		}

		uint32_t idx = (uint32_t)((va - vm_base) /
					  CONFIG_MM_DRV_PAGE_SIZE);
		uint16_t entry = tlb[idx];
		bool mapped = (entry & _SHELL_ENABLE_BIT) != 0;

		if (!mapped) {
			shell_print(sh, "  0x%08x  (not mapped)",
				    (uint32_t)va);
			continue;
		}

		uintptr_t pa = shell_tlb_idx_to_pa(idx, entry);
		uint32_t bank = (uint32_t)((pa - _SHELL_PHYS_BASE) /
					   (128 * 1024));
		char flags[4];

		shell_tlb_flags_str(entry, flags);
		shell_print(sh, "  0x%08x  0x%08x  yes     %s    %-4u  0x%04x",
			    (uint32_t)va, (uint32_t)pa,
			    flags, bank, (uint32_t)entry);
	}
	return 0;
}

#endif /* CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB */

#endif /* CONFIG_SOF_SHELL_MMU_DBG */

#if CONFIG_SOF_SHELL_MMU_DBG
#if CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB
SHELL_SUBCMD_ADD((sof), mmu_status, NULL,
		 "Print Intel ADSP MTL TLB / virtual memory status\n",
		 cmd_sof_mmu_status, 0, 0);
SHELL_SUBCMD_ADD((sof), tlb_dump, NULL,
		 "Dump all active TLB entries (vaddr/paddr/flags)\n",
		 cmd_sof_tlb_dump, 0, 0);
SHELL_SUBCMD_ADD((sof), tlb_lookup, NULL,
		 "Query TLB for a page or range: <vaddr> [end_vaddr]\n",
		 cmd_sof_tlb_lookup, 2, 1);
#endif
#endif
SHELL_CMD_REGISTER(sof, &sub_sof,
		   "SOF application commands", NULL);
