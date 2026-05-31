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
#include <sof/schedule/schedule.h>
#include <string.h>
#include <sof/ipc/common.h>
#include <sof/lib/vpage.h>
#include <sof/lib/vregion.h>
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

#if CONFIG_SOF_SHELL_LLEXT_LOAD
#include <sof/shell_llext_load.h>
#include <sof/lib_manager.h>
#include <adsp_debug_window.h>
#endif /* CONFIG_SOF_SHELL_LLEXT_LOAD */

#if (CONFIG_SOF_SHELL_LLEXT_LIST || CONFIG_SOF_SHELL_LLEXT_PURGE) && CONFIG_LLEXT
#include <zephyr/llext/llext.h>
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

#include <sof_versions.h>

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
		unsigned long v = strtoul(argv[1], &ep, 0);

		if (ep == argv[1]) {
			shell_print(sh, "error: invalid address '%s'", argv[1]);
			return -EINVAL;
		}
		vstart = (uintptr_t)v & ~(CONFIG_MM_DRV_PAGE_SIZE - 1);
	}

	if (argc > 2) {
		char *ep;
		unsigned long v = strtoul(argv[2], &ep, 0);

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

#if CONFIG_XTENSA_MMU

/*
 * Xtensa hardware MMU helpers.
 *
 * PDTLB result layout (Xtensa ISA §4.6.5):
 *   bit[4]   = HIT
 *   bits[3:0] = TLB way (valid when HIT)
 *
 * RDTLB0 result:
 *   bits[31:12] = Virtual Page Number
 *   bits[5:4]   = ring (0=kernel, 1=unused, 2=user, 3=shared)
 *   bits[3:0]   = CA (cache + access attributes)
 *
 * RDTLB1 result:
 *   bits[31:12] = Physical Page Number
 *   bits[3:0]   = CA (same as RDTLB0)
 *
 * CA bits (from <zephyr/arch/xtensa/xtensa_mmu.h>):
 *   bit 0 = XTENSA_MMU_PERM_X   (executable)
 *   bit 1 = XTENSA_MMU_PERM_W   (writable)
 *   bit 2 = XTENSA_MMU_CACHED_WB (write-back cache)
 *   bit 3 = XTENSA_MMU_CACHED_WT (write-through cache)
 *   bits 2+3 both set = illegal / not-present
 */
#define SHELL_PDTLB_HIT    0x10U  /* bit 4 of pdtlb result */
#define SHELL_PTE_RING_SHIFT 4U
#define SHELL_PTE_RING_MASK  0x30U
#define SHELL_PTE_CA_MASK    0x0FU
#define SHELL_PTE_PPN_MASK   0xFFFFF000U

static inline uint32_t shell_pdtlb(void *vaddr)
{
	uint32_t r;
	__asm__ __volatile__("pdtlb %0, %1\n\t" : "=a"(r) : "a"((uint32_t)vaddr));
	return r;
}

static inline uint32_t shell_rdtlb0(uint32_t entry)
{
	uint32_t r;
	__asm__ volatile("rdtlb0 %0, %1\n\t" : "=a"(r) : "a"(entry));
	return r;
}

static inline uint32_t shell_rdtlb1(uint32_t entry)
{
	uint32_t r;
	__asm__ volatile("rdtlb1 %0, %1\n\t" : "=a"(r) : "a"(entry));
	return r;
}

static inline uint32_t shell_rsr_rasid(void)
{
	uint32_t r;
	__asm__ volatile("rsr %0, rasid" : "=a"(r));
	return r;
}

/* Decode 4-bit CA cache field into a short string */
__cold static const char *ca_cache_str(uint32_t ca)
{
	switch (ca & (XTENSA_MMU_CACHED_WB | XTENSA_MMU_CACHED_WT)) {
	case 0:                     return "uncached";
	case XTENSA_MMU_CACHED_WB:  return "WB      ";
	case XTENSA_MMU_CACHED_WT:  return "WT      ";
	default:                    return "illegal ";
	}
}

__cold_rodata static const char * const ring_name[] = {
	"kernel", "unused", "user", "shared"
};

/* sof rasid */
__cold static int cmd_sof_rasid(const struct shell *sh,
				size_t argc, char *argv[])
{
	uint32_t rasid = shell_rsr_rasid();
	int ring;

	shell_print(sh, "RASID: 0x%08x", rasid);
	for (ring = 0; ring < 4; ring++) {
		uint8_t asid = (uint8_t)((rasid >> (ring * 8)) & 0xff);

		shell_print(sh, "  ring %d (%s):\tASID 0x%02x",
			    ring, ring_name[ring], asid);
	}
	return 0;
}

/* sof page_info <vaddr> [end_vaddr] */
__cold static int cmd_sof_page_info(const struct shell *sh,
				    size_t argc, char *argv[])
{
	uintptr_t vstart, vend;
	uint32_t rasid;

	{
		char *ep;
		unsigned long v = strtoul(argv[1], &ep, 0);

		if (ep == argv[1]) {
			shell_print(sh, "error: invalid address '%s'", argv[1]);
			return -EINVAL;
		}
		vstart = (uintptr_t)v & ~(uintptr_t)(KB(4) - 1);
	}

	if (argc > 2) {
		char *ep;
		unsigned long v = strtoul(argv[2], &ep, 0);

		if (ep == argv[2]) {
			shell_print(sh, "error: invalid address '%s'", argv[2]);
			return -EINVAL;
		}
		vend = (uintptr_t)v & ~(uintptr_t)(KB(4) - 1);
	} else {
		vend = vstart;
	}

	if (vend < vstart)
		vend = vstart;

	rasid = shell_rsr_rasid();
	shell_print(sh, "RASID: 0x%08x", rasid);
	shell_print(sh, "  %-12s  %-12s  ring  asid  perms  cache");

	for (uintptr_t va = vstart; va <= vend; va += KB(4)) {
		uint32_t probe = shell_pdtlb((void *)va);

		if (!(probe & SHELL_PDTLB_HIT)) {
			shell_print(sh,
				    "  0x%08x   (DTLB miss — not in TLB cache)",
				    (uint32_t)va);
			continue;
		}

		uint32_t pte0 = shell_rdtlb0(probe);
		uint32_t pte1 = shell_rdtlb1(probe);
		uint32_t ring = (pte0 & SHELL_PTE_RING_MASK) >> SHELL_PTE_RING_SHIFT;
		uint32_t ca   = pte0 & SHELL_PTE_CA_MASK;
		uint32_t paddr = pte1 & SHELL_PTE_PPN_MASK;
		uint8_t  asid = (uint8_t)((rasid >> (ring * 8)) & 0xff);
		char perm[4] = {
			'R',
			(ca & XTENSA_MMU_PERM_W) ? 'W' : '-',
			(ca & XTENSA_MMU_PERM_X) ? 'X' : '-',
			'\0'
		};

		shell_print(sh,
			    "  0x%08x   0x%08x   %u     0x%02x  %s    %s",
			    (uint32_t)va, paddr,
			    ring, asid,
			    perm, ca_cache_str(ca));
	}
	return 0;
}

#endif /* CONFIG_XTENSA_MMU */

#if CONFIG_XTENSA_MMU
SHELL_SUBCMD_ADD((sof), rasid, NULL,
		 "Decode RASID register: ring 0-3 to ASID mapping\n",
		 cmd_sof_rasid, 0, 0);
SHELL_SUBCMD_ADD((sof), page_info, NULL,
		 "Probe DTLB for a page or range: <vaddr> [end_vaddr]\n"
		 "Reports physical address, ring, ASID, R/W/X permissions"
		 " and cache mode for each page currently in the DTLB.\n",
		 cmd_sof_page_info, 2, 1);
#endif

#if CONFIG_SOF_SHELL_LLEXT_LOAD

/* parse_long: shared numeric argument parser for kernel shell commands. */
__cold static int parse_long(const struct shell *sh, const char *s, long *out,
			     long min_val, long max_val)
{
	char *endptr;
	long v = strtol(s, &endptr, 0);

	if (endptr == s || v < min_val || v > max_val) {
		shell_print(sh, "error: invalid value '%s' (allowed %ld..%ld)",
			    s, min_val, max_val);
		return -EINVAL;
	}
	*out = v;
	return 0;
}

#define SOF_SHELL_LLEXT_TIMEOUT_MSEC  120000U
#define SOF_SHELL_LLEXT_POLL_MSEC       500U

__cold static int cmd_sof_llext_load(const struct shell *sh,
				     size_t argc, char *argv[])
{
	const char *name = argv[1];
	uint32_t lib_id = 1;
	volatile struct sof_shell_llext_slot *slot;
	uint32_t elapsed = 0;
	uint32_t state;

	if (argc > 2) {
		long val;
		int ret = parse_long(sh, argv[2], &val, 1, LIB_MANAGER_MAX_LIBS - 1);

		if (ret)
			return ret;
		lib_id = (uint32_t)val;
	}

	/* Acquire or reuse the LLEXT_LOAD debug window slot */
#if CONFIG_INTEL_ADSP_DEBUG_SLOT_MANAGER
	{
		struct adsp_dw_desc slot_desc = { .type = ADSP_DW_SLOT_LLEXT_LOAD };
		size_t slot_size;

		slot = adsp_dw_request_slot(&slot_desc, &slot_size);
		if (!slot) {
			shell_error(sh, "Failed to acquire debug window slot");
			return -ENOMEM;
		}
	}
#else
	/* Fall back to a compile-time fixed slot index */
	slot = (volatile struct sof_shell_llext_slot *)
		ADSP_DW->slots[CONFIG_SOF_SHELL_LLEXT_LOAD_SLOT_NUM];
	ADSP_DW->descs[CONFIG_SOF_SHELL_LLEXT_LOAD_SLOT_NUM].type =
		ADSP_DW_SLOT_LLEXT_LOAD;
#endif

	state = slot->state;
	if (state != SOF_SHELL_LLEXT_IDLE) {
		shell_error(sh, "llext_load slot busy (state=%u) — try again later",
			    state);
		return -EBUSY;
	}

	/* Initialise the shared slot word-by-word (MMIO/uncached region) */
	{
		volatile uint32_t *p = (volatile uint32_t *)slot;
		size_t nwords = sizeof(struct sof_shell_llext_slot) / sizeof(uint32_t);

		for (size_t i = 0; i < nwords; i++)
			p[i] = 0;
	}
	strncpy((char *)slot->name, name, sizeof(slot->name) - 1);
	slot->lib_id = lib_id;
	slot->magic  = SOF_SHELL_LLEXT_MAGIC;
	/* Publish state last so the host only sees REQUESTING once all fields are set */
	slot->state  = SOF_SHELL_LLEXT_REQUESTING;

	shell_print(sh, "Slot ready: name=%s  lib_id=%u  timeout=%us",
		    name, lib_id, SOF_SHELL_LLEXT_TIMEOUT_MSEC / 1000);
	shell_print(sh, "On host:    dd if=<module.ri> of=/sys/kernel/debug/sof/llext_load bs=$(stat -c%%s <module.ri>) count=1");

	/* Poll waiting for the host to finish DMA + library load */
	while (elapsed < SOF_SHELL_LLEXT_TIMEOUT_MSEC) {
		k_msleep(SOF_SHELL_LLEXT_POLL_MSEC);
		elapsed += SOF_SHELL_LLEXT_POLL_MSEC;

		state = slot->state;

		if (state == SOF_SHELL_LLEXT_DMA_DONE) {
			shell_print(sh,
				    "llext_load OK: lib_id=%u  %u bytes transferred",
				    lib_id, slot->xfer_bytes);
			slot->state = SOF_SHELL_LLEXT_IDLE;
			return 0;
		}

		if (state == SOF_SHELL_LLEXT_ERROR) {
			shell_error(sh, "llext_load FAILED: result=%d", slot->result);
			slot->state = SOF_SHELL_LLEXT_IDLE;
			return (int)slot->result;
		}
	}

	shell_error(sh, "llext_load timeout after %us",
		    SOF_SHELL_LLEXT_TIMEOUT_MSEC / 1000);
	slot->state = SOF_SHELL_LLEXT_IDLE;
	return -ETIMEDOUT;
}

SHELL_SUBCMD_ADD((sof), llext_load, NULL,
		 "Load llext module from host: <name> [lib_id=1]\n"
		 "Sets up the DMA handshake slot then waits for:\n"
		 "  dd if=<module.ri> of=/sys/kernel/debug/sof/llext_load\\\n"
		 "     bs=$(stat -c%s <module.ri>) count=1\n"
		 "on the host. Prints result when DMA and IPC4 load complete.\n",
		 cmd_sof_llext_load, 2, 1);
#endif /* CONFIG_SOF_SHELL_LLEXT_LOAD */

#if CONFIG_SOF_SHELL_LLEXT_LIST

/*
 * sof llext_list
 *
 * Lists all llext libraries currently held in IMR/DRAM.  For each library the
 * DRAM base address, total storage size and per-module-file SRAM state are
 * printed.
 *
 * Example output:
 *   llext libs in IMR/DRAM:
 *   [1] base=0x89000000  size=49152 B  modules=1
 *       [1:0] TESTER   mapped=NO   use=0  dep=0
 */
__cold static int cmd_sof_llext_list(const struct shell *sh,
				     size_t argc, char *argv[])
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

#if CONFIG_LIBRARY_MANAGER
	struct ext_library *ext_lib = ext_lib_get();
	int found = 0;
	int lib_id;

	shell_print(sh, "llext libs in IMR/DRAM:");

	for (lib_id = 1; lib_id < LIB_MANAGER_MAX_LIBS; lib_id++) {
		const struct lib_manager_mod_ctx *ctx = ext_lib->desc[lib_id];
		const struct sof_man_fw_desc *desc;
		uint32_t store_bytes;

		if (!ctx || !ctx->base_addr)
			continue;

		desc = (const struct sof_man_fw_desc *)
			((const uint8_t *)ctx->base_addr + SOF_MAN_ELF_TEXT_OFFSET);
		store_bytes = desc->header.preload_page_count *
			      (uint32_t)_SHELL_MOD_PAGE_SZ;

		shell_print(sh, "[%d] base=%p  size=%u B  manifest_mods=%u  elf_files=%u",
			    lib_id, ctx->base_addr, store_bytes,
			    desc->header.num_module_entries,
			    ctx->n_mod);

#if CONFIG_LLEXT
		if (ctx->mod) {
			unsigned int i;

			for (i = 0; i < ctx->n_mod; i++) {
				const struct lib_manager_module *m = ctx->mod + i;
				int use = m->llext ? (int)m->llext->use_count : 0;
				char name[SOF_MAN_MOD_NAME_LEN + 1];
				const uint8_t *nm;

				if (m->mod_manifest) {
					nm = m->mod_manifest->module.name;
				} else {
					const struct sof_man_module *mm =
						(const struct sof_man_module *)
						((const uint8_t *)desc +
						 SOF_MAN_MODULE_OFFSET(m->start_idx));
					nm = mm->name;
				}
				memcpy(name, nm, SOF_MAN_MOD_NAME_LEN);
				name[SOF_MAN_MOD_NAME_LEN] = '\0';

				shell_print(sh,
					    "    [%d:%u] %-8s"
					    "  DRAM=yes  SRAM=%-3s"
					    "  use=%-2d  dep=%u",
					    lib_id, i, name,
					    m->mapped ? "yes" : "no",
					    use,
					    m->n_dependent);
			}
		}
#endif /* CONFIG_LLEXT */

		found++;
	}

	if (!found)
		shell_print(sh, "  (none)");
#else
	shell_print(sh, "Library manager not enabled.");
#endif /* CONFIG_LIBRARY_MANAGER */
	return 0;
}

#endif /* CONFIG_SOF_SHELL_LLEXT_LIST */

#if CONFIG_SOF_SHELL_LLEXT_PURGE

/*
 * sof llext_purge <lib_id>
 *
 * Removes a loadable llext library from IMR/DRAM storage and frees its memory.
 * All module files belonging to the library must be unloaded from SRAM first
 * (i.e., all pipeline instances using the library must be torn down).
 *
 * Example:
 *   uart:~$ sof llext_purge 1
 *   llext_purge: lib 1 freed OK
 */
__cold static int cmd_sof_llext_purge(const struct shell *sh,
				      size_t argc, char *argv[])
{
#if CONFIG_LIBRARY_MANAGER
	char *endptr = NULL;
	long lib_id;
	int ret;

	lib_id = strtol(argv[1], &endptr, 0);
	if (endptr == argv[1] || lib_id < 1 || lib_id >= LIB_MANAGER_MAX_LIBS) {
		shell_error(sh, "lib_id must be 1..%d", LIB_MANAGER_MAX_LIBS - 1);
		return -EINVAL;
	}

	ret = lib_manager_purge_library((uint32_t)lib_id);
	switch (ret) {
	case 0:
		shell_print(sh, "llext_purge: lib %ld freed OK", lib_id);
		break;
	case -ENOENT:
		shell_error(sh, "llext_purge: lib %ld not loaded", lib_id);
		break;
	case -EBUSY:
		shell_error(sh, "llext_purge: lib %ld still active in SRAM — "
			    "destroy all pipelines using it first", lib_id);
		break;
	default:
		shell_error(sh, "llext_purge: lib %ld failed: %d", lib_id, ret);
		break;
	}
	return ret;
#else
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	shell_print(sh, "Library manager not enabled.");
	return -ENOSYS;
#endif /* CONFIG_LIBRARY_MANAGER */
}

#endif /* CONFIG_SOF_SHELL_LLEXT_PURGE */

#if CONFIG_SOF_SHELL_LLEXT_LIST
SHELL_SUBCMD_ADD((sof), llext_list, NULL,
		 "List llext libraries stored in IMR/DRAM.\n"
		 "For each library shows base address, storage size and per-module\n"
		 "SRAM mapping state (yes/no), use count and dependency count.\n",
		 cmd_sof_llext_list, 0, 0);
#endif

#if CONFIG_SOF_SHELL_LLEXT_PURGE
SHELL_SUBCMD_ADD((sof), llext_purge, NULL,
		 "Purge llext library from IMR/DRAM: <lib_id>\n"
		 "Fails with -EBUSY if any module in the library is still\n"
		 "mapped in SRAM (i.e. a pipeline using it is still active).\n",
		 cmd_sof_llext_purge, 2, 0);
#endif

#if CONFIG_SOF_SHELL_CORE_POWER

/*
 * sof core_on <core_id>
 * sof core_off <core_id>
 *
 * Power a secondary DSP core on or off.  Core 0 (primary) cannot be
 * controlled via these commands.
 */
__cold static int cmd_sof_core_on(const struct shell *sh,
				  size_t argc, char *argv[])
{
	char *endptr = NULL;
	long id;
	int ret;

	id = strtol(argv[1], &endptr, 0);
	if (endptr == argv[1] || id < 1 || id >= CONFIG_CORE_COUNT) {
		shell_error(sh, "core_id must be 1..%d", CONFIG_CORE_COUNT - 1);
		return -EINVAL;
	}

	if (cpu_is_core_enabled((int)id)) {
		shell_print(sh, "core %ld already active", id);
		return 0;
	}

	ret = cpu_enable_core((int)id);
	if (ret)
		shell_error(sh, "core_on: failed to enable core %ld: %d", id, ret);
	else
		shell_print(sh, "core_on: core %ld enabled", id);

	return ret;
}

__cold static int cmd_sof_core_off(const struct shell *sh,
				   size_t argc, char *argv[])
{
	char *endptr = NULL;
	long id;

	id = strtol(argv[1], &endptr, 0);
	if (endptr == argv[1] || id < 1 || id >= CONFIG_CORE_COUNT) {
		shell_error(sh, "core_id must be 1..%d", CONFIG_CORE_COUNT - 1);
		return -EINVAL;
	}

	if (!cpu_is_core_enabled((int)id)) {
		shell_print(sh, "core %ld already inactive", id);
		return 0;
	}

	cpu_disable_core((int)id);

	if (cpu_is_core_enabled((int)id)) {
		shell_error(sh, "core_off: core %ld did not power down", id);
		return -EIO;
	}

	shell_print(sh, "core_off: core %ld disabled", id);
	return 0;
}

#endif /* CONFIG_SOF_SHELL_CORE_POWER */

#if CONFIG_SOF_SHELL_CORE_POWER
SHELL_SUBCMD_ADD((sof), core_on, NULL,
		 "Power on a secondary DSP core: <core_id>\n"
		 "core_id must be 1..CONFIG_CORE_COUNT-1 (core 0 is primary).\n",
		 cmd_sof_core_on, 2, 0);
SHELL_SUBCMD_ADD((sof), core_off, NULL,
		 "Power off a secondary DSP core: <core_id>\n"
		 "core_id must be 1..CONFIG_CORE_COUNT-1 (core 0 is primary).\n",
		 cmd_sof_core_off, 2, 0);
#endif

__cold static int cmd_sof_version(const struct shell *sh, size_t argc, char *argv[])
{
	shell_print(sh, "SOF Version: %d.%d.%d-%s (Build %d)",
		    SOF_MAJOR, SOF_MINOR, SOF_MICRO, SOF_TAG, SOF_BUILD);
	shell_print(sh, "Git Tag: %s", SOF_GIT_TAG);
	shell_print(sh, "Source Hash: 0x%08x", SOF_SRC_HASH);
	return 0;
}

SHELL_SUBCMD_ADD((sof), version, NULL,
		 "Print the current SOF software version\n",
		 cmd_sof_version, 0, 0);

#if CONFIG_SOF_VREGIONS
__cold static void vpage_alloc_print_cb(unsigned int idx, unsigned int vpage,
					unsigned int pages, void *ctx)
{
	const struct shell *sh = ctx;

	shell_fprintf(sh, SHELL_NORMAL, "    [%u] vpage %u, pages %u\n",
		      idx, vpage, pages);
}

struct vregion_print_ctx {
	const struct shell *sh;
	int count;
};

__cold static void vregion_print_cb(int idx, const struct vregion_snapshot *s,
				    void *ctx)
{
	struct vregion_print_ctx *c = ctx;

	shell_fprintf(c->sh, SHELL_NORMAL,
		      "  [%d] Base: 0x%lx, Size: %#zx bytes, Pages: %u\n",
		      idx, (unsigned long)s->base, s->size, s->pages);
	shell_fprintf(c->sh, SHELL_NORMAL,
		      "      Lifetime Used: %#zx bytes, Free Count: %d\n",
		      s->lifetime_used, s->lifetime_free_count);
	shell_fprintf(c->sh, SHELL_NORMAL, "      Use Count: %u\n",
		      s->use_count);
	c->count++;
}
#endif /* CONFIG_SOF_VREGIONS */

__cold static int cmd_sof_vpage_info(const struct shell *sh, size_t argc, char *argv[])
{
#if CONFIG_SOF_VREGIONS
	struct vpage_stats stats;

	vpage_get_stats(&stats);

	shell_fprintf(sh, SHELL_NORMAL, "Virtual Page Allocator Status:\n");
	shell_fprintf(sh, SHELL_NORMAL,
		      "  Region Base: %p, Size: %#zx bytes, Total Pages: %u\n",
		      stats.region_base, stats.region_size, stats.total_pages);
	shell_fprintf(sh, SHELL_NORMAL, "  Free Pages: %u\n", stats.free_pages);
	shell_fprintf(sh, SHELL_NORMAL, "  Allocated Elements in use: %u / %u\n",
		      stats.num_elems_in_use, stats.max_allocs);

	vpage_for_each_alloc(vpage_alloc_print_cb, (void *)sh);
#else
	shell_fprintf(sh, SHELL_NORMAL, "Virtual regions not enabled\n");
#endif
	return 0;
}

__cold static int cmd_sof_vregion_info(const struct shell *sh, size_t argc, char *argv[])
{
#if CONFIG_SOF_VREGIONS
	struct vregion_print_ctx ctx = { .sh = sh, .count = 0 };

	shell_fprintf(sh, SHELL_NORMAL, "Virtual Regions Status:\n");
	vregion_for_each(vregion_print_cb, &ctx);
	if (ctx.count == 0)
		shell_fprintf(sh, SHELL_NORMAL,
			      "  No active virtual regions found.\n");
#else
	shell_fprintf(sh, SHELL_NORMAL, "Virtual regions not enabled\n");
#endif
	return 0;
}


SHELL_SUBCMD_ADD((sof), vpage_status, NULL,
		 "Print virtual page allocator status\n",
		 cmd_sof_vpage_info, 0, 0);

SHELL_SUBCMD_ADD((sof), vregion_status, NULL,
		 "Print virtual regions status\n",
		 cmd_sof_vregion_info, 0, 0);

__cold static int cmd_sof_ipc_stats(const struct shell *sh, size_t argc, char *argv[])
{
	struct ipc_stats s;

	if (argc > 1 && !strcmp(argv[1], "reset")) {
		ipc_stats_reset();
		shell_print(sh, "ipc stats reset");
		return 0;
	}

	ipc_stats_get(&s);
	shell_print(sh, "IPC statistics:");
	shell_print(sh, "  rx_count        : %u", s.rx_count);
	shell_print(sh, "  rx_errors       : %u", s.rx_errors);
	shell_print(sh, "  tx_count        : %u", s.tx_count);
	shell_print(sh, "  tx_direct_count : %u", s.tx_direct_count);
	shell_print(sh, "  tx_errors       : %u", s.tx_errors);
	return 0;
}

__cold static int cmd_sof_ipc_last(const struct shell *sh, size_t argc, char *argv[])
{
	struct ipc_stats s;

	ipc_stats_get(&s);
	shell_print(sh, "Last IPC RX: pri=0x%08x ext=0x%08x @ %llu cycles",
		    s.last_rx_pri, s.last_rx_ext, (unsigned long long)s.last_rx_time);
	shell_print(sh, "Last IPC TX: pri=0x%08x ext=0x%08x @ %llu cycles",
		    s.last_tx_pri, s.last_tx_ext, (unsigned long long)s.last_tx_time);
	return 0;
}

SHELL_SUBCMD_ADD((sof), ipc_stats, NULL,
		 "Print IPC RX/TX counters; 'sof ipc stats reset' clears them\n",
		 cmd_sof_ipc_stats, 1, 1);

SHELL_SUBCMD_ADD((sof), ipc_last, NULL,
		 "Print the last received and sent IPC headers\n",
		 cmd_sof_ipc_last, 0, 0);

#if CONFIG_SOF_SHELL_SCHED_INFO

__cold static const char *sched_type_str(int type)
{
	switch (type) {
	case SOF_SCHEDULE_EDF:		return "edf";
	case SOF_SCHEDULE_LL_TIMER:	return "ll_timer";
	case SOF_SCHEDULE_LL_DMA:	return "ll_dma";
	case SOF_SCHEDULE_DP:		return "dp";
	case SOF_SCHEDULE_TWB:		return "twb";
	default:			return "?";
	}
}

__cold static const char *sched_state_str(enum task_state s)
{
	switch (s) {
	case SOF_TASK_STATE_INIT:	return "init";
	case SOF_TASK_STATE_QUEUED:	return "queued";
	case SOF_TASK_STATE_PENDING:	return "pending";
	case SOF_TASK_STATE_RUNNING:	return "running";
	case SOF_TASK_STATE_PREEMPTED:	return "preempt";
	case SOF_TASK_STATE_COMPLETED:	return "done";
	case SOF_TASK_STATE_FREE:	return "free";
	case SOF_TASK_STATE_CANCEL:	return "cancel";
	case SOF_TASK_STATE_RESCHEDULE:	return "resched";
	default:			return "?";
	}
}

struct sched_walk_ctx {
	const struct shell *sh;
	int sch_type;
	uint32_t total_sum;
	uint32_t total_cnt;
	uint32_t total_max;
	int task_count;
	bool show_load;
};

__cold static void sched_list_cb(struct task *task, void *_ctx)
{
	struct sched_walk_ctx *c = _ctx;
	uint32_t avg = task->cycles_cnt ? task->cycles_sum / task->cycles_cnt : 0;

	if (c->show_load) {
		shell_print(c->sh,
			    "  %-9s core %u  prio %3u  state %-7s"
			    "  count %u  avg %u  max %u  sum %u cyc",
			    sched_type_str(c->sch_type), task->core,
			    task->priority, sched_state_str(task->state),
			    task->cycles_cnt, avg, task->cycles_max,
			    task->cycles_sum);
	} else {
		shell_print(c->sh,
			    "  %-9s core %u  prio %3u  state %-7s"
			    "  flags 0x%04x  uid %p  data %p",
			    sched_type_str(c->sch_type), task->core,
			    task->priority, sched_state_str(task->state),
			    task->flags, (const void *)task->uid, task->data);
	}

	c->total_sum += task->cycles_sum;
	c->total_cnt += task->cycles_cnt;
	if (task->cycles_max > c->total_max)
		c->total_max = task->cycles_max;
	c->task_count++;
}

__cold static int sched_walk(const struct shell *sh, bool show_load)
{
	struct schedulers *schedulers = *arch_schedulers_get();
	struct sched_walk_ctx ctx = { .sh = sh, .show_load = show_load };
	struct schedule_data *sch;
	struct list_item *slist;

	if (!schedulers) {
		shell_print(sh, "No schedulers registered");
		return 0;
	}

	list_for_item(slist, &schedulers->list) {
		sch = container_of(slist, struct schedule_data, list);
		if (!sch->ops->scheduler_dump_tasks)
			continue;
		ctx.sch_type = sch->type;
		sch->ops->scheduler_dump_tasks(sch->data, sched_list_cb, &ctx);
	}

	if (!ctx.task_count)
		shell_print(sh, "  (no tasks)");

	if (show_load) {
		uint32_t avg = ctx.total_cnt ? ctx.total_sum / ctx.total_cnt : 0;

		shell_print(sh,
			    "Total: %d tasks  count %u  avg %u  peak max %u cyc",
			    ctx.task_count, ctx.total_cnt, avg, ctx.total_max);
	}

	return 0;
}

__cold static int cmd_sof_sched_tasks(const struct shell *sh,
				      size_t argc, char *argv[])
{
	shell_print(sh, "Active scheduler tasks:");
	return sched_walk(sh, false);
}

__cold static int cmd_sof_sched_load(const struct shell *sh,
				     size_t argc, char *argv[])
{
	shell_print(sh, "Scheduler task cycle counters:");
	return sched_walk(sh, true);
}

#endif /* CONFIG_SOF_SHELL_SCHED_INFO */

#if CONFIG_SOF_SHELL_SCHED_INFO
SHELL_SUBCMD_ADD((sof), sched_tasks, NULL,
		 "List all scheduler tasks (type, core, prio, state)\n",
		 cmd_sof_sched_tasks, 0, 0);
SHELL_SUBCMD_ADD((sof), sched_load, NULL,
		 "Show per-task cycle counters and totals\n",
		 cmd_sof_sched_load, 0, 0);
#endif

#if CONFIG_SOF_SHELL_LOG_INFO

#include <zephyr/logging/log_backend.h>
#include <zephyr/logging/log_ctrl.h>

__cold static int cmd_sof_log_status(const struct shell *sh,
				     size_t argc, char *argv[])
{
	int n = log_backend_count_get();
	int i;

	shell_print(sh, "Log backends: %d, sources: %u",
		    n, log_src_cnt_get(Z_LOG_LOCAL_DOMAIN_ID));
	shell_print(sh, "  idx  id  active  name");

	for (i = 0; i < n; i++) {
		const struct log_backend *be = log_backend_get(i);

		if (!be)
			continue;
		shell_print(sh, "  %3d  %3u   %-3s    %s",
			    i, log_backend_id_get(be),
			    log_backend_is_active(be) ? "yes" : "no",
			    be->name ? be->name : "?");
	}

	return 0;
}

#endif /* CONFIG_SOF_SHELL_LOG_INFO */

#if CONFIG_SOF_SHELL_MTRACE_DUMP

#include <adsp_debug_window.h>

/* must match the layout used by zephyr/subsys/logging/backends/log_backend_adsp_mtrace.c */
struct sof_shell_mtrace_slot {
	uint32_t host_ptr;
	uint32_t dsp_ptr;
	uint8_t data[ADSP_DW_SLOT_SIZE - 2 * sizeof(uint32_t)];
} __packed;

#define SOF_SHELL_MTRACE_BUF_SIZE (ADSP_DW_SLOT_SIZE - 2 * sizeof(uint32_t))
#define SOF_SHELL_MTRACE_TYPE(core) \
	(ADSP_DW_SLOT_DEBUG_LOG | ((core) & ADSP_DW_SLOT_CORE_MASK))

__cold static int cmd_sof_mtrace_dump(const struct shell *sh,
				      size_t argc, char *argv[])
{
	struct sof_shell_mtrace_slot *slot;
	uint32_t r, w, len, i;

#ifdef CONFIG_INTEL_ADSP_DEBUG_SLOT_MANAGER
	struct adsp_dw_desc desc = { .type = SOF_SHELL_MTRACE_TYPE(0) };

	slot = adsp_dw_request_slot(&desc, NULL);
#else
	slot = (struct sof_shell_mtrace_slot *)
		ADSP_DW->slots[ADSP_DW_SLOT_NUM_MTRACE];
#endif
	if (!slot) {
		shell_print(sh, "mtrace slot not available");
		return -ENODEV;
	}

	r = slot->host_ptr;
	w = slot->dsp_ptr;

	if (r == w) {
		shell_print(sh, "mtrace: empty (host_ptr=dsp_ptr=%u)", r);
		return 0;
	}

	if (w > r)
		len = w - r;
	else
		len = SOF_SHELL_MTRACE_BUF_SIZE - r + w;

	shell_print(sh,
		    "mtrace: host_ptr=%u dsp_ptr=%u unread=%u bytes (snapshot)",
		    r, w, len);

	/* print byte-by-byte without advancing host_ptr; preserves host consumer */
	for (i = 0; i < len; i++) {
		uint32_t off = (r + i) % SOF_SHELL_MTRACE_BUF_SIZE;

		shell_fprintf(sh, SHELL_NORMAL, "%c", slot->data[off]);
	}
	shell_fprintf(sh, SHELL_NORMAL, "\n");

	return 0;
}

#endif /* CONFIG_SOF_SHELL_MTRACE_DUMP */

#if CONFIG_SOF_SHELL_LOG_INFO
SHELL_SUBCMD_ADD((sof), log_status, NULL,
		 "List Zephyr log backends with state and source count\n",
		 cmd_sof_log_status, 0, 0);
#endif

#if CONFIG_SOF_SHELL_MTRACE_DUMP
SHELL_SUBCMD_ADD((sof), mtrace_dump, NULL,
		 "Snapshot the mtrace SRAM ring buffer (does not advance host_ptr)\n",
		 cmd_sof_mtrace_dump, 0, 0);
#endif

#if CONFIG_SOF_SHELL_MAILBOX_HEX || CONFIG_SOF_SHELL_DBGWIN_DUMP

__cold static void sof_shell_hex_dump(const struct shell *sh, uintptr_t base,
			       size_t off, size_t len)
{
	const uint8_t *p = (const uint8_t *)(base + off);
	size_t i, j;

	for (i = 0; i < len; i += 16) {
		size_t row = MIN((size_t)16, len - i);
		char ascii[17];

		shell_fprintf(sh, SHELL_NORMAL, "%08lx ",
			      (unsigned long)(off + i));
		for (j = 0; j < 16; j++) {
			if (j < row)
				shell_fprintf(sh, SHELL_NORMAL, " %02x", p[i + j]);
			else
				shell_fprintf(sh, SHELL_NORMAL, "   ");
			ascii[j] = (j < row && p[i + j] >= 0x20 && p[i + j] < 0x7f) ?
				   (char)p[i + j] : '.';
		}
		ascii[16] = '\0';
		shell_fprintf(sh, SHELL_NORMAL, "  %s\n", ascii);
	}
}

#endif

#if CONFIG_SOF_SHELL_MAILBOX_HEX

#include <sof/lib/mailbox.h>

struct sof_shell_mb_region {
	const char *name;
	uintptr_t base;
	size_t size;
};

__cold_rodata static const struct sof_shell_mb_region sof_shell_mb_regions[] = {
	{ "exception", MAILBOX_EXCEPTION_BASE, MAILBOX_EXCEPTION_SIZE },
	{ "dspbox",    MAILBOX_DSPBOX_BASE,    MAILBOX_DSPBOX_SIZE    },
	{ "hostbox",   MAILBOX_HOSTBOX_BASE,   MAILBOX_HOSTBOX_SIZE   },
	{ "debug",     MAILBOX_DEBUG_BASE,     MAILBOX_DEBUG_SIZE     },
};

__cold static int cmd_sof_mailbox_hex(const struct shell *sh,
				      size_t argc, char *argv[])
{
	const struct sof_shell_mb_region *r = NULL;
	size_t off = 0, len;
	char *end = NULL;
	int i;

	if (argc < 2) {
		shell_print(sh, "Mailbox regions:");
		for (i = 0; i < ARRAY_SIZE(sof_shell_mb_regions); i++)
			shell_print(sh, "  %-10s base 0x%08lx  size %zu",
				    sof_shell_mb_regions[i].name,
				    (unsigned long)sof_shell_mb_regions[i].base,
				    sof_shell_mb_regions[i].size);
		shell_print(sh, "Usage: sof mailbox hex <region> [offset] [length]");
		return 0;
	}

	for (i = 0; i < ARRAY_SIZE(sof_shell_mb_regions); i++) {
		if (!strcmp(argv[1], sof_shell_mb_regions[i].name)) {
			r = &sof_shell_mb_regions[i];
			break;
		}
	}
	if (!r) {
		shell_print(sh, "Unknown region '%s'", argv[1]);
		return -EINVAL;
	}

	if (argc > 2) {
		off = strtoul(argv[2], &end, 0);
		if (end == argv[2] || off >= r->size) {
			shell_print(sh, "Bad offset (max 0x%zx)", r->size);
			return -EINVAL;
		}
	}

	len = MIN((size_t)256, r->size - off);
	if (argc > 3) {
		len = strtoul(argv[3], &end, 0);
		if (end == argv[3])
			return -EINVAL;
		len = MIN(len, r->size - off);
	}

	shell_print(sh, "%s @ 0x%08lx + 0x%zx, %zu bytes:",
		    r->name, (unsigned long)r->base, off, len);
	sof_shell_hex_dump(sh, r->base, off, len);
	return 0;
}

#endif /* CONFIG_SOF_SHELL_MAILBOX_HEX */

#if CONFIG_SOF_SHELL_DBGWIN_DUMP

#include <adsp_memory.h>
#include <adsp_debug_window.h>

/* Mirror struct used by zephyr/soc/intel/intel_adsp/common/debug_window.c.
 * We map window 2 directly so we can read descriptors and slot data without
 * depending on slot-manager internals.
 */
struct sof_shell_dw {
	struct adsp_dw_desc descs[ADSP_DW_DESC_COUNT];
	uint8_t reserved[ADSP_DW_PAGE0_SLOT_OFFSET -
			 ADSP_DW_DESC_COUNT * sizeof(struct adsp_dw_desc)];
	uint8_t partial_page0[ADSP_DW_SLOT_SIZE - ADSP_DW_PAGE0_SLOT_OFFSET];
	uint8_t slots[ADSP_DW_SLOT_COUNT][ADSP_DW_SLOT_SIZE];
} __packed;

#define SOF_SHELL_DW_BASE \
	(DT_REG_ADDR(DT_PHANDLE(DT_NODELABEL(mem_window2), memory)) + WIN2_OFFSET)

__cold static const char *dw_type_name(uint32_t type)
{
	switch (type & ADSP_DW_SLOT_TYPE_MASK) {
	case ADSP_DW_SLOT_UNUSED & ADSP_DW_SLOT_TYPE_MASK:
		return type ? "?" : "unused";
	case ADSP_DW_SLOT_CRITICAL_LOG & ADSP_DW_SLOT_TYPE_MASK:
		return "critical_log";
	case ADSP_DW_SLOT_DEBUG_LOG & ADSP_DW_SLOT_TYPE_MASK:
		return "debug_log";
	case ADSP_DW_SLOT_GDB_STUB & ADSP_DW_SLOT_TYPE_MASK:
		return "gdb_stub";
	case ADSP_DW_SLOT_TELEMETRY & ADSP_DW_SLOT_TYPE_MASK:
		return "telemetry";
	case ADSP_DW_SLOT_TRACE & ADSP_DW_SLOT_TYPE_MASK:
		return "trace";
	case ADSP_DW_SLOT_SHELL & ADSP_DW_SLOT_TYPE_MASK:
		return "shell";
	case ADSP_DW_SLOT_DEBUG_STREAM & ADSP_DW_SLOT_TYPE_MASK:
		return "debug_stream";
	case ADSP_DW_SLOT_BROKEN & ADSP_DW_SLOT_TYPE_MASK:
		return "broken";
	default:
		return "?";
	}
}

__cold static int cmd_sof_dbgwin_dump(const struct shell *sh,
				      size_t argc, char *argv[])
{
	volatile struct sof_shell_dw *dw =
		(volatile struct sof_shell_dw *)
		sys_cache_uncached_ptr_get((__sparse_force void __sparse_cache *)
					   SOF_SHELL_DW_BASE);
	int slot, i;
	size_t len = 256;
	char *end = NULL;

	if (argc < 2) {
		shell_print(sh,
			    "ADSP debug window @ 0x%08lx (%d slots, %u bytes each)",
			    (unsigned long)SOF_SHELL_DW_BASE,
			    ADSP_DW_SLOT_COUNT, ADSP_DW_SLOT_SIZE);
		shell_print(sh, "  slot  res_id      type       vma         name");
		for (i = 0; i < ADSP_DW_SLOT_COUNT; i++) {
			shell_print(sh,
				    "  %3d   0x%08x  0x%08x 0x%08x  %s (core %u)",
				    i, dw->descs[i].resource_id, dw->descs[i].type,
				    dw->descs[i].vma, dw_type_name(dw->descs[i].type),
				    dw->descs[i].type & ADSP_DW_SLOT_CORE_MASK);
		}
		shell_print(sh, "Usage: sof dbgwin dump <slot> [length]");
		return 0;
	}

	slot = strtol(argv[1], &end, 0);
	if (end == argv[1] || slot < 0 || slot >= ADSP_DW_SLOT_COUNT) {
		shell_print(sh, "Bad slot (0..%d)", ADSP_DW_SLOT_COUNT - 1);
		return -EINVAL;
	}

	if (argc > 2) {
		len = strtoul(argv[2], &end, 0);
		if (end == argv[2])
			return -EINVAL;
	}
	len = MIN(len, (size_t)ADSP_DW_SLOT_SIZE);

	shell_print(sh, "Slot %d type=0x%08x (%s, core %u) vma=0x%08x; %zu bytes:",
		    slot, dw->descs[slot].type, dw_type_name(dw->descs[slot].type),
		    dw->descs[slot].type & ADSP_DW_SLOT_CORE_MASK,
		    dw->descs[slot].vma, len);
	sof_shell_hex_dump(sh, (uintptr_t)dw->slots[slot], 0, len);
	return 0;
}

#endif /* CONFIG_SOF_SHELL_DBGWIN_DUMP */

#if CONFIG_SOF_SHELL_MAILBOX_HEX
SHELL_SUBCMD_ADD((sof), mailbox_hex, NULL,
		 "Hex-dump a mailbox region: <region> [offset] [length]\n",
		 cmd_sof_mailbox_hex, 1, 3);
#endif

#if CONFIG_SOF_SHELL_DBGWIN_DUMP
SHELL_SUBCMD_ADD((sof), dbgwin_dump, NULL,
		 "List ADSP debug-window slots, or hex-dump one: [slot] [length]\n",
		 cmd_sof_dbgwin_dump, 1, 2);
#endif

#if CONFIG_SOF_SHELL_PERF_STATUS

#include <sof/debug/telemetry/telemetry.h>
#include <sof/debug/telemetry/performance_monitor.h>
#include <ipc4/base_fw.h>

__cold static const char *perf_state_str(enum ipc4_perf_measurements_state_set s)
{
	switch (s) {
	case IPC4_PERF_MEASUREMENTS_DISABLED:	return "disabled";
	case IPC4_PERF_MEASUREMENTS_STOPPED:	return "stopped";
	case IPC4_PERF_MEASUREMENTS_STARTED:	return "started";
	case IPC4_PERF_MEASUREMENTS_PAUSED:	return "paused";
	default:				return "?";
	}
}

__cold static int cmd_sof_perf_status(const struct shell *sh,
				      size_t argc, char *argv[])
{
	struct system_tick_info *systick;
	int core_id, ret;

	if (argc > 1) {
		if (!strcmp(argv[1], "reset")) {
			ret = reset_performance_counters();
			shell_print(sh, "perf: reset_performance_counters() = %d", ret);
			return ret;
		}
		if (!strcmp(argv[1], "start")) {
			ret = enable_performance_counters();
			if (!ret)
				perf_meas_set_state(IPC4_PERF_MEASUREMENTS_STARTED);
			shell_print(sh, "perf: enable_performance_counters() = %d", ret);
			return ret;
		}
		if (!strcmp(argv[1], "stop")) {
			perf_meas_set_state(IPC4_PERF_MEASUREMENTS_STOPPED);
			shell_print(sh, "perf: stopped");
			return 0;
		}
		if (!strcmp(argv[1], "pause")) {
			perf_meas_set_state(IPC4_PERF_MEASUREMENTS_PAUSED);
			shell_print(sh, "perf: paused");
			return 0;
		}
		shell_print(sh, "Usage: sof perf status [reset|start|stop|pause]");
		return -EINVAL;
	}

	shell_print(sh, "Performance measurements: %s",
		    perf_state_str(perf_meas_get_state()));

#ifdef CONFIG_INTEL_ADSP_DEBUG_SLOT_MANAGER
	systick = telemetry_get_systick_info_ptr();
	if (!systick) {
		shell_print(sh, "telemetry slot not allocated");
		return 0;
	}
#else
	{
		struct telemetry_wnd_data *wnd =
			(struct telemetry_wnd_data *)
			ADSP_DW->slots[SOF_DW_TELEMETRY_SLOT];
		systick = (struct system_tick_info *)wnd->system_tick_info;
	}
#endif

	shell_print(sh, "Per-core systick (count, last_us_cyc, max_us_cyc, avg_kcps, peak_kcps):");
	for (core_id = 0; core_id < CONFIG_MAX_CORE_COUNT; core_id++) {
		if (!(cpu_enabled_cores() & BIT(core_id)))
			continue;
		shell_print(sh,
			    "  core %u: count=%u last=%u max=%u avg_kcps=%u peak_kcps=%u peak4k=%u peak8k=%u",
			    core_id,
			    systick[core_id].count,
			    systick[core_id].last_time_elapsed,
			    systick[core_id].max_time_elapsed,
			    systick[core_id].avg_utilization,
			    systick[core_id].peak_utilization,
			    systick[core_id].peak_utilization_4k,
			    systick[core_id].peak_utilization_8k);
	}

	return 0;
}

#endif /* CONFIG_SOF_SHELL_PERF_STATUS */

#if CONFIG_SOF_SHELL_PERF_STATUS
SHELL_SUBCMD_ADD((sof), perf_status, NULL,
		 "Show telemetry perf state and per-core systick;"
		 " optional [reset|start|stop|pause]\n",
		 cmd_sof_perf_status, 1, 1);
#endif

/*
 * Space-separated aliases for underscore commands.
 *
 * Keep legacy underscore names for compatibility while exposing the
 * preferred tokenized form, e.g. "sof core on" for "sof core_on".
 */

SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_test_inject_sched,
	SHELL_CMD(gap, NULL,
		  "Inject a gap to audio scheduling\n",
		  cmd_sof_test_inject_sched_gap),
	SHELL_SUBCMD_SET_END
);

SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_test_inject,
	SHELL_CMD(sched, &sof_cmd_test_inject_sched,
		  "Scheduler injection commands\n", NULL),
	SHELL_SUBCMD_SET_END
);

SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_test,
	SHELL_CMD(inject, &sof_cmd_test_inject,
		  "Injection test commands\n", NULL),
	SHELL_SUBCMD_SET_END
);

#if CONFIG_SOF_SHELL_CORE_STATUS || CONFIG_SOF_SHELL_CORE_POWER
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_core,
#if CONFIG_SOF_SHELL_CORE_STATUS
	SHELL_CMD(status, NULL,
		  "Print enabled/active state of each DSP core\n",
		  cmd_sof_core_status),
#endif
#if CONFIG_SOF_SHELL_CORE_POWER
	SHELL_CMD_ARG(on, NULL,
		  "Power on a secondary DSP core: <core_id>\n"
		  "core_id must be 1..CONFIG_CORE_COUNT-1 (core 0 is primary).\n",
		  cmd_sof_core_on, 2, 0),
	SHELL_CMD_ARG(off, NULL,
		  "Power off a secondary DSP core: <core_id>\n"
		  "core_id must be 1..CONFIG_CORE_COUNT-1 (core 0 is primary).\n",
		  cmd_sof_core_off, 2, 0),
#endif
	SHELL_SUBCMD_SET_END
);
#endif

#if CONFIG_SOF_SHELL_SRAM_STATUS
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_sram,
	SHELL_CMD(status, NULL,
		  "Print HPSRAM heap usage statistics\n",
		  cmd_sof_sram_status),
	SHELL_SUBCMD_SET_END
);
#endif

#if CONFIG_SOF_SHELL_CLOCK_STATUS
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_clock,
	SHELL_CMD(status, NULL,
		  "Print current clock frequency for each DSP core\n",
		  cmd_sof_clock_status),
	SHELL_SUBCMD_SET_END
);
#endif

#if CONFIG_SOF_SHELL_MMU_DBG
#if CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_mmu,
	SHELL_CMD(status, NULL,
		  "Print Intel ADSP MTL TLB / virtual memory status\n",
		  cmd_sof_mmu_status),
	SHELL_SUBCMD_SET_END
);

SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_tlb,
	SHELL_CMD(dump, NULL,
		  "Dump all active TLB entries (vaddr/paddr/flags)\n",
		  cmd_sof_tlb_dump),
	SHELL_CMD_ARG(lookup, NULL,
		  "Query TLB for a page or range: <vaddr> [end_vaddr]\n",
		  cmd_sof_tlb_lookup, 2, 1),
	SHELL_SUBCMD_SET_END
);
#endif
#if CONFIG_XTENSA_MMU
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_page,
	SHELL_CMD_ARG(info, NULL,
		  "Probe DTLB for a page or range: <vaddr> [end_vaddr]\n"
		  "Reports physical address, ring, ASID, R/W/X permissions"
		  " and cache mode for each page currently in the DTLB.\n",
		  cmd_sof_page_info, 2, 1),
	SHELL_SUBCMD_SET_END
);
#endif
#endif

#if CONFIG_SOF_SHELL_LLEXT_LOAD || CONFIG_SOF_SHELL_LLEXT_LIST || CONFIG_SOF_SHELL_LLEXT_PURGE
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_llext,
#if CONFIG_SOF_SHELL_LLEXT_LOAD
	SHELL_CMD_ARG(load, NULL,
		  "Load llext module from host: <name> [lib_id=1]\n"
		  "Sets up the DMA handshake slot then waits for:\n"
		  "  dd if=<module.ri> of=/sys/kernel/debug/sof/llext_load\\\n"
		  "     bs=$(stat -c%s <module.ri>) count=1\n"
		  "on the host. Prints result when DMA and IPC4 load complete.\n",
		  cmd_sof_llext_load, 2, 1),
#endif
#if CONFIG_SOF_SHELL_LLEXT_LIST
	SHELL_CMD(list, NULL,
		  "List llext libraries stored in IMR/DRAM.\n"
		  "For each library shows base address, storage size and per-module\n"
		  "SRAM mapping state (yes/no), use count and dependency count.\n",
		  cmd_sof_llext_list),
#endif
#if CONFIG_SOF_SHELL_LLEXT_PURGE
	SHELL_CMD_ARG(purge, NULL,
		  "Purge llext library from IMR/DRAM: <lib_id>\n"
		  "Fails with -EBUSY if any module in the library is still\n"
		  "mapped in SRAM (i.e. a pipeline using it is still active).\n",
		  cmd_sof_llext_purge, 2, 0),
#endif
	SHELL_SUBCMD_SET_END
);
#endif

SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_vpage,
	SHELL_CMD(status, NULL,
		  "Print virtual page allocator status\n",
		  cmd_sof_vpage_info),
	SHELL_SUBCMD_SET_END
);

SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_vregion,
	SHELL_CMD(status, NULL,
		  "Print virtual regions status\n",
		  cmd_sof_vregion_info),
	SHELL_SUBCMD_SET_END
);

SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_ipc,
	SHELL_CMD_ARG(stats, NULL,
		  "Print IPC RX/TX counters; 'sof ipc stats reset' clears them\n",
		  cmd_sof_ipc_stats, 1, 1),
	SHELL_CMD(last, NULL,
		  "Print the last received and sent IPC headers\n",
		  cmd_sof_ipc_last),
	SHELL_SUBCMD_SET_END
);

#if CONFIG_SOF_SHELL_SCHED_INFO
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_sched,
	SHELL_CMD(tasks, NULL,
		  "List all scheduler tasks (type, core, prio, state)\n",
		  cmd_sof_sched_tasks),
	SHELL_CMD(load, NULL,
		  "Show per-task cycle counters and totals\n",
		  cmd_sof_sched_load),
	SHELL_SUBCMD_SET_END
);
#endif

#if CONFIG_SOF_SHELL_LOG_INFO
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_log,
	SHELL_CMD(status, NULL,
		  "List Zephyr log backends with state and source count\n",
		  cmd_sof_log_status),
	SHELL_SUBCMD_SET_END
);
#endif

#if CONFIG_SOF_SHELL_MTRACE_DUMP
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_mtrace,
	SHELL_CMD(dump, NULL,
		  "Snapshot the mtrace SRAM ring buffer (does not advance host_ptr)\n",
		  cmd_sof_mtrace_dump),
	SHELL_SUBCMD_SET_END
);
#endif

#if CONFIG_SOF_SHELL_MAILBOX_HEX
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_mailbox,
	SHELL_CMD_ARG(hex, NULL,
		  "Hex-dump a mailbox region: <region> [offset] [length]\n",
		  cmd_sof_mailbox_hex, 1, 3),
	SHELL_SUBCMD_SET_END
);
#endif

#if CONFIG_SOF_SHELL_DBGWIN_DUMP
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_dbgwin,
	SHELL_CMD_ARG(dump, NULL,
		  "List ADSP debug-window slots, or hex-dump one: [slot] [length]\n",
		  cmd_sof_dbgwin_dump, 1, 2),
	SHELL_SUBCMD_SET_END
);
#endif

#if CONFIG_SOF_SHELL_PERF_STATUS
SHELL_STATIC_SUBCMD_SET_CREATE(sof_cmd_perf,
	SHELL_CMD_ARG(status, NULL,
		  "Show telemetry perf state and per-core systick;"
		  " optional [reset|start|stop|pause]\n",
		  cmd_sof_perf_status, 1, 1),
	SHELL_SUBCMD_SET_END
);
#endif

SHELL_SUBCMD_ADD((sof), test, &sof_cmd_test,
		 "Test commands\n", NULL, 0, 0);

#if CONFIG_SOF_SHELL_CORE_STATUS || CONFIG_SOF_SHELL_CORE_POWER
SHELL_SUBCMD_ADD((sof), core, &sof_cmd_core,
		 "Core commands\n", NULL, 0, 0);
#endif

#if CONFIG_SOF_SHELL_SRAM_STATUS
SHELL_SUBCMD_ADD((sof), sram, &sof_cmd_sram,
		 "SRAM commands\n", NULL, 0, 0);
#endif

#if CONFIG_SOF_SHELL_CLOCK_STATUS
SHELL_SUBCMD_ADD((sof), clock, &sof_cmd_clock,
		 "Clock commands\n", NULL, 0, 0);
#endif

#if CONFIG_SOF_SHELL_MMU_DBG
#if CONFIG_MM_DRV_INTEL_ADSP_MTL_TLB
SHELL_SUBCMD_ADD((sof), mmu, &sof_cmd_mmu,
		 "MMU status commands\n", NULL, 0, 0);
SHELL_SUBCMD_ADD((sof), tlb, &sof_cmd_tlb,
		 "TLB commands\n", NULL, 0, 0);
#endif
#if CONFIG_XTENSA_MMU
SHELL_SUBCMD_ADD((sof), page, &sof_cmd_page,
		 "Page-table commands\n", NULL, 0, 0);
#endif
#endif

#if CONFIG_SOF_SHELL_LLEXT_LOAD || CONFIG_SOF_SHELL_LLEXT_LIST || CONFIG_SOF_SHELL_LLEXT_PURGE
SHELL_SUBCMD_ADD((sof), llext, &sof_cmd_llext,
		 "LLEXT commands\n", NULL, 0, 0);
#endif

SHELL_SUBCMD_ADD((sof), vpage, &sof_cmd_vpage,
		 "Virtual page commands\n", NULL, 0, 0);
SHELL_SUBCMD_ADD((sof), vregion, &sof_cmd_vregion,
		 "Virtual region commands\n", NULL, 0, 0);
SHELL_SUBCMD_ADD((sof), ipc, &sof_cmd_ipc,
		 "IPC commands\n", NULL, 0, 0);

#if CONFIG_SOF_SHELL_SCHED_INFO
SHELL_SUBCMD_ADD((sof), sched, &sof_cmd_sched,
		 "Scheduler commands\n", NULL, 0, 0);
#endif

#if CONFIG_SOF_SHELL_LOG_INFO
SHELL_SUBCMD_ADD((sof), log, &sof_cmd_log,
		 "Log commands\n", NULL, 0, 0);
#endif

#if CONFIG_SOF_SHELL_MTRACE_DUMP
SHELL_SUBCMD_ADD((sof), mtrace, &sof_cmd_mtrace,
		 "Mtrace commands\n", NULL, 0, 0);
#endif

#if CONFIG_SOF_SHELL_MAILBOX_HEX
SHELL_SUBCMD_ADD((sof), mailbox, &sof_cmd_mailbox,
		 "Mailbox commands\n", NULL, 0, 0);
#endif

#if CONFIG_SOF_SHELL_DBGWIN_DUMP
SHELL_SUBCMD_ADD((sof), dbgwin, &sof_cmd_dbgwin,
		 "Debug-window commands\n", NULL, 0, 0);
#endif

#if CONFIG_SOF_SHELL_PERF_STATUS
SHELL_SUBCMD_ADD((sof), perf, &sof_cmd_perf,
		 "Performance commands\n", NULL, 0, 0);
#endif
SHELL_CMD_REGISTER(sof, &sub_sof,
		   "SOF application commands", NULL);
