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

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/shell/shell.h>

#include <stdlib.h>

static int cmd_sof_module_heap_usage(const struct shell *sh,
				     size_t argc, char *argv[])
{
	struct ipc *ipc = sof_get()->ipc;
	struct list_item *clist, *_clist;
	struct ipc_comp_dev *icd;

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
	}
	return 0;
}

SHELL_SUBCMD_ADD((sof), module_heap_usage, NULL,
		 "Print heap memory usage of each module\n",
		 cmd_sof_module_heap_usage, 0, 0);
