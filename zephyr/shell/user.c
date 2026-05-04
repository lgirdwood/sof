// SPDX-License-Identifier: BSD-3-Clause
/*
 * Copyright(c) 2024 Intel Corporation.
 *
 * Author: Kai Vehmanen <kai.vehmanen@linux.intel.com>
 */

/*
 * SOF shell - audio domain commands.
 *
 * This file hosts the user/audio facing shell commands (pipelines, modules,
 * buffers, DAI/DMA, kcontrols). They attach to the root "sof" command that is
 * created in shell/kernel.c via the shell iterable-section subcommand
 * mechanism.
 */

#include <rtos/sof.h> /* sof_get() */
#include <sof/audio/module_adapter/module/generic.h>
#include <sof/audio/component.h>
#include <sof/audio/component_ext.h>
#include <sof/audio/pipeline.h>
#include <sof/ipc/topology.h>
#include <sof/lib/memory.h>
#if CONFIG_SOF_SHELL_MODULE_LIST
#include <rimage/sof/user/manifest.h>
#include <ipc4/base_fw_vendor.h>
#if CONFIG_LIBRARY_MANAGER
#include <sof/lib_manager.h>
#endif
#endif /* CONFIG_SOF_SHELL_MODULE_LIST */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/shell/shell.h>

#include <stdlib.h>

#if CONFIG_SOF_SHELL_HEAP_USAGE
__cold static int cmd_sof_module_heap_usage(const struct shell *sh,
					    size_t argc, char *argv[])
{
	struct ipc *ipc = sof_get()->ipc;
	struct list_item *clist, *_clist;
	struct ipc_comp_dev *icd;
	int count = 0;

	if (!ipc) {
		shell_print(sh, "No IPC");
		return 0;
	}

	list_for_item_safe(clist, _clist, &ipc->comp_list) {
		size_t usage, hwm;

		icd = container_of(clist, struct ipc_comp_dev, list);
		if (icd->type != COMP_TYPE_COMPONENT)
			continue;

		usage = module_adapter_heap_usage(comp_mod(icd->cd), &hwm);
		shell_print(sh, "comp id 0x%08x%9zu usage%9zu hwm\tbytes",
			    icd->id, usage, hwm);
		count++;
	}

	if (!count)
		shell_print(sh, "No components found. Start an audio stream first.");

	return 0;
}

SHELL_SUBCMD_ADD((sof), module_heap_usage, NULL,
		 "Print heap memory usage of each module\n",
		 cmd_sof_module_heap_usage, 0, 0);
#endif /* CONFIG_SOF_SHELL_HEAP_USAGE */

#if CONFIG_SOF_SHELL_PIPELINE_STATUS || CONFIG_SOF_SHELL_MODULE_STATUS

__cold_rodata static const char * const comp_state_names[] = {
	[COMP_STATE_NOT_EXIST]	= "not_exist",
	[COMP_STATE_INIT]	= "init",
	[COMP_STATE_READY]	= "ready",
	[COMP_STATE_SUSPEND]	= "suspend",
	[COMP_STATE_PREPARE]	= "prepare",
	[COMP_STATE_PAUSED]	= "paused",
	[COMP_STATE_ACTIVE]	= "active",
	[COMP_STATE_PRE_ACTIVE] = "pre_active",
};

__cold static const char *comp_state_str(uint16_t state)
{
	if (state < ARRAY_SIZE(comp_state_names) && comp_state_names[state])
		return comp_state_names[state];
	return "unknown";
}

#endif /* CONFIG_SOF_SHELL_PIPELINE_STATUS || CONFIG_SOF_SHELL_MODULE_STATUS */

#if CONFIG_SOF_SHELL_PIPELINE_STATUS
__cold static int cmd_sof_pipeline_status(const struct shell *sh,
					  size_t argc, char *argv[])
{
	struct ipc *ipc = sof_get()->ipc;
	struct list_item *clist;
	struct ipc_comp_dev *icd;
	int count = 0;

	if (!ipc) {
		shell_print(sh, "No IPC");
		return 0;
	}

	shell_print(sh, "%-8s %-5s %-8s %-10s %-10s %s",
		    "ppl_id", "core", "priority", "period_us", "status", "state");

	list_for_item(clist, &ipc->comp_list) {
		struct pipeline *p;

		icd = container_of(clist, struct ipc_comp_dev, list);
		if (icd->type != COMP_TYPE_PIPELINE)
			continue;

		p = icd->pipeline;
		shell_print(sh, "%-8u %-5u %-8u %-10u %-10u %s",
			    p->pipeline_id, p->core, p->priority,
			    p->period, p->status,
			    comp_state_str((uint16_t)p->status));
		count++;
	}

	if (!count)
		shell_print(sh, "No pipelines found.");

	return 0;
}

SHELL_SUBCMD_ADD((sof), pipeline_status, NULL,
		 "Print status of all active pipelines\n",
		 cmd_sof_pipeline_status, 0, 0);
#endif /* CONFIG_SOF_SHELL_PIPELINE_STATUS */

#if CONFIG_SOF_SHELL_MODULE_STATUS
__cold static int cmd_sof_module_status(const struct shell *sh,
					size_t argc, char *argv[])
{
	struct ipc *ipc = sof_get()->ipc;
	struct list_item *clist;
	struct ipc_comp_dev *icd;
	int count = 0;

	if (!ipc) {
		shell_print(sh, "No IPC");
		return 0;
	}

	shell_print(sh, "%-12s %-8s %-5s %s",
		    "comp_id", "ppl_id", "core", "state");

	list_for_item(clist, &ipc->comp_list) {
		icd = container_of(clist, struct ipc_comp_dev, list);
		if (icd->type != COMP_TYPE_COMPONENT)
			continue;

		shell_print(sh, "0x%-10x %-8u %-5u %s",
			    icd->id,
			    icd->cd->pipeline ? icd->cd->pipeline->pipeline_id : 0,
			    icd->core,
			    comp_state_str(icd->cd->state));
		count++;
	}

	if (!count)
		shell_print(sh, "No components found. Start an audio stream first.");

	return 0;
}

SHELL_SUBCMD_ADD((sof), module_status, NULL,
		 "Print status of all active components\n",
		 cmd_sof_module_status, 0, 0);
#endif /* CONFIG_SOF_SHELL_MODULE_STATUS */

#if CONFIG_SOF_SHELL_MODULE_LIST

/* Page size in DSP manifest entries (instance_bss_size, segment lengths) */
#ifdef CONFIG_MM_DRV_PAGE_SIZE
#define _SHELL_MOD_PAGE_SZ CONFIG_MM_DRV_PAGE_SIZE
#else
#define _SHELL_MOD_PAGE_SZ 4096
#endif

#if CONFIG_IPC4_BASE_FW_INTEL
__cold static void print_manifest_modules(const struct shell *sh,
					  const struct sof_man_fw_desc *desc,
					  int lib_id)
{
	const struct sof_man_mod_config *cfg_base;
	int i;

	if (!desc)
		return;

	cfg_base = (const struct sof_man_mod_config *)
		((const uint8_t *)desc +
		 SOF_MAN_MODULE_OFFSET(desc->header.num_module_entries));

	for (i = 0; i < (int)desc->header.num_module_entries; i++) {
		const struct sof_man_module *mod;
		const struct sof_man_mod_config *cfg = NULL;
		uint32_t text_sz, bss_sz;
		char name[SOF_MAN_MOD_NAME_LEN + 1];

		mod = (const struct sof_man_module *)
			((const uint8_t *)desc + SOF_MAN_MODULE_OFFSET(i));

		/* name is not null-terminated in the manifest */
		memcpy(name, mod->name, SOF_MAN_MOD_NAME_LEN);
		name[SOF_MAN_MOD_NAME_LEN] = '\0';

		if (mod->cfg_count > 0)
			cfg = cfg_base + mod->cfg_offset;

		text_sz = (uint32_t)mod->segment[0].flags.r.length * _SHELL_MOD_PAGE_SZ;
		bss_sz  = (uint32_t)mod->instance_bss_size * _SHELL_MOD_PAGE_SZ;

		shell_print(sh,
			    "[%d:%d] %-8s"
			    "  uuid:%08x-%04x-%04x-%02x%02x%02x%02x%02x%02x%02x%02x",
			    lib_id, i, name,
			    mod->uuid.a, mod->uuid.b, mod->uuid.c,
			    mod->uuid.d[0], mod->uuid.d[1],
			    mod->uuid.d[2], mod->uuid.d[3],
			    mod->uuid.d[4], mod->uuid.d[5],
			    mod->uuid.d[6], mod->uuid.d[7]);
		shell_print(sh,
			    "        inst_max:%-3u  bss/inst:%6u B  text:%6u B"
			    "  affinity:0x%02x",
			    mod->instance_max_count, bss_sz, text_sz,
			    mod->affinity_mask);
		if (cfg)
			shell_print(sh,
				    "        cpc:%-8u  cps:%-9u  ibs:%-6u  obs:%u",
				    cfg->cpc, cfg->cps, cfg->ibs, cfg->obs);
		else
			shell_print(sh, "        cpc:N/A");
	}
}
#endif /* CONFIG_IPC4_BASE_FW_INTEL */

__cold static int cmd_sof_module_list(const struct shell *sh,
				      size_t argc, char *argv[])
{
#if CONFIG_IPC4_BASE_FW_INTEL
	const struct sof_man_fw_desc *desc;
	int total = 0;

	shell_print(sh, "Built-in modules:");
	desc = basefw_vendor_get_manifest();
	if (desc) {
		print_manifest_modules(sh, desc, 0);
		total += (int)desc->header.num_module_entries;
	} else {
		shell_print(sh, "  (manifest not available)");
	}

#if CONFIG_LIBRARY_MANAGER
	{
		int lib_id;

		for (lib_id = 1; lib_id < LIB_MANAGER_MAX_LIBS; lib_id++) {
			desc = lib_manager_get_library_manifest(
					LIB_MANAGER_PACK_LIB_ID(lib_id));
			if (!desc)
				continue;
			shell_print(sh, "Library %d modules:", lib_id);
			print_manifest_modules(sh, desc, lib_id);
			total += (int)desc->header.num_module_entries;
		}
	}
#endif /* CONFIG_LIBRARY_MANAGER */

	if (!total)
		shell_print(sh, "No modules found.");

#else /* !CONFIG_IPC4_BASE_FW_INTEL */
	/* Generic fallback: list registered component drivers */
	struct comp_driver_list *drivers = comp_drivers_get();
	struct list_item *clist;
	struct comp_driver_info *info;
	int count = 0;

	shell_print(sh, "%-5s  %-24s  %s", "type", "name", "uuid");

	list_for_item(clist, &drivers->list) {
		const struct sof_uuid *uid;
		const char *name;

		info = container_of(clist, struct comp_driver_info, list);
		uid = info->drv->uid;
		name = (info->drv->tctx && info->drv->tctx->uuid_p)
			? info->drv->tctx->uuid_p->name : "?";

		shell_print(sh,
			    "%-5u  %-24s"
			    "  %08x-%04x-%04x-%02x%02x%02x%02x%02x%02x%02x%02x",
			    info->drv->type, name,
			    uid->a, uid->b, uid->c,
			    uid->d[0], uid->d[1], uid->d[2], uid->d[3],
			    uid->d[4], uid->d[5], uid->d[6], uid->d[7]);
		count++;
	}

	if (!count)
		shell_print(sh, "No drivers registered.");
#endif /* CONFIG_IPC4_BASE_FW_INTEL */

	return 0;
}

SHELL_SUBCMD_ADD((sof), module_list, NULL,
		 "List all available modules with name, memory, size and RTC info\n",
		 cmd_sof_module_list, 0, 0);
#endif /* CONFIG_SOF_SHELL_MODULE_LIST */
