// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2023 Intel Corporation. All rights reserved.
//
// Author: Jaroslaw Stelter <jaroslaw.stelter@intel.com>
#include <sof/common.h>
#include <rtos/interrupt.h>
#include <sof/ipc/msg.h>
#include <rtos/alloc.h>
#include <rtos/cache.h>
#include <sof/lib/memory.h>
#include <sof/list.h>
#include <rtos/string.h>
#include <ipc/topology.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/sys/sem.h>
#include <sof/audio/module_adapter/module/modules_userspace.h>
#include <rtos/userspace_helper.h>
#include <system_agent.h>
#include <utilities/array.h>

LOG_MODULE_REGISTER(modules_user_tr, CONFIG_SOF_LOG_LEVEL);

/* 7c42ce8b-0108-43d0-9137-56d660478c55 */
// SOF_DEFINE_REG_UUID(modules_user_tr);
SOF_DEFINE_UUID("modules_user", modules_user_uuid, 0x7c42ce8b, 0x0108, 0x43d0, 0x91, 0x37, 0x56, 0xd6, 0x60, 0x47, 0x8c, 0x55);

DECLARE_TR_CTX(modules_user_tr, SOF_UUID(modules_user_uuid), LOG_LEVEL_INFO);

/* IPC works synchronously so single message queue is ok */
#define MSGQ_LEN        1

K_APPMEM_PARTITION_DEFINE(ipc_partition);
#define MAX_PARAM_SIZE	0x200

struct user_worker_data {
	struct k_msgq *ipc_in_msg_q;		/* pointer to input message queue    */
	struct k_msgq *ipc_out_msg_q;		/* pointer to output message queue   */
	struct k_work_user work_item;		/* ipc worker workitem               */
	k_tid_t ipc_worker_tid;			/* ipc worker thread ID              */
	uint8_t ipc_params[MAX_PARAM_SIZE];	/* ipc parameter buffer              */
	uint32_t module_ref_cnt;		/* module reference count            */
	void *p_worker_stack;			/* pointer to worker stack           */
};

struct user_security_domain {
	struct k_work_user_q ipc_user_work_q;
	struct k_msgq in_msgq;
	struct k_msgq out_msgq;
};

static struct user_security_domain security_domain[CONFIG_SEC_DOMAIN_MAX_NUMBER];
/*
 * There is one instance of IPC user worker per security domain.
 * Keep pointer to it here
 */
static APP_USER_DATA struct user_worker_data *worker_data[CONFIG_SEC_DOMAIN_MAX_NUMBER];

struct module_common_params {
	uint32_t cmd;
	int status;
	struct processing_module *mod;
	struct module_data *md;
};

struct module_init_params {
	struct module_common_params common;
	uint32_t module_id;
	uint32_t instance_id;
	uint32_t log_handle;
	byte_array_t mod_cfg;
};

struct module_large_cfg_set_params {
	struct module_common_params common;
	uint32_t config_id;
	enum module_cfg_fragment_position pos;
	uint32_t data_offset_size;
	const uint8_t *fragment;
	size_t fragment_size;
	uint8_t *response;
	size_t response_size;
};

struct module_large_cfg_get_params {
	struct module_common_params common;
	uint32_t config_id;
	enum module_cfg_fragment_position pos;
	uint32_t *data_offset_size;
	uint8_t *fragment;
	size_t fragment_size;
};

struct module_processing_mode_params {
	struct module_common_params common;
	enum module_processing_mode mode;
};

#define MODULE_CMD_INIT			0x1
#define MODULE_CMD_PREPARE  		0x2
#define MODULE_CMD_PROCESS  		0x3
#define MODULE_CMD_SET_PROCMOD 		0x4
#define MODULE_CMD_GET_PROCMOD 	 	0x5
#define MODULE_CMD_SET_CONF 		0x6
#define MODULE_CMD_GET_CONF 	 	0x7
#define MODULE_CMD_RESET		0x8
#define MODULE_CMD_FREE			0x9

static int modules_msgq_alloc(struct user_security_domain *security_domain,
			      struct user_worker_data *worker_data)
{
	char *buffer;

	buffer = rzalloc(SOF_MEM_ZONE_SYS_RUNTIME, 0,
			 SOF_MEM_CAPS_RAM | SOF_MEM_CAPS_MMU_SHD,
			 MAX_PARAM_SIZE * 2);
	if (!buffer)
		return -ENOMEM;

	k_msgq_init(&security_domain->in_msgq, buffer, MAX_PARAM_SIZE, 1);
	k_msgq_init(&security_domain->out_msgq, (buffer + MAX_PARAM_SIZE), MAX_PARAM_SIZE, 1);

	worker_data->ipc_in_msg_q = &security_domain->in_msgq;
	worker_data->ipc_out_msg_q = &security_domain->out_msgq;

	return 0;
}

static void modules_msgq_free(struct user_worker_data *worker_data)
{
	/* ipc_in_msg_q->buffer_start points to whole buffer allocated for message queues */
	rfree(worker_data->ipc_in_msg_q->buffer_start);
}
static int modules_user_init_worker(struct processing_module *mod,
			     k_work_user_handler_t user_handler)
{
	uint32_t sd_id = mod->dev_user.security_domain_id;
	int ret;
	comp_dbg(mod->dev, "modules_user_init_worker(): start");

	/* If worker_data not initialized this is first userspace module in security domain  */
	if (!worker_data[sd_id]) {

		worker_data[sd_id] = rzalloc(SOF_MEM_ZONE_SYS_RUNTIME, 0,
					     SOF_MEM_CAPS_RAM | SOF_MEM_CAPS_MMU_SHD,
					     sizeof(*worker_data[sd_id]));

		if (!worker_data[sd_id])
			return -ENOMEM;

		/* Store worker data in processing module structure */
		mod->dev_user.wrk_ctx = worker_data[sd_id];

		ret = modules_msgq_alloc(&security_domain[sd_id], worker_data[sd_id]);
		if (ret < 0)
			goto err_worker;

		worker_data[sd_id]->p_worker_stack = user_stack_allocate(CONFIG_SOF_STACK_SIZE, K_USER);
		if (!worker_data[sd_id]->p_worker_stack) {
			comp_err(mod->dev, "modules_user_init_worker(): stack alloc failed");
			ret = -ENOMEM;
			goto err_msgq;
		}

		/* Create User Mode work queue start before submit. */
		k_work_user_queue_start(&security_domain[sd_id].ipc_user_work_q, worker_data[0]->p_worker_stack,
					CONFIG_SOF_STACK_SIZE,
					0, NULL);

		worker_data[sd_id]->ipc_worker_tid = k_work_user_queue_thread_get(&security_domain[sd_id].ipc_user_work_q);
	}

	/* Init module memory domain. */
	ret = user_memory_init(worker_data[sd_id]->ipc_worker_tid, (uintptr_t)mod);
	if (ret < 0) {
		comp_err(mod->dev,
			"modules_user_init_worker(): failed to init memory domain: error: %d",
			ret);
		return ret;
	}

	struct k_mem_domain *domain = mod->dev_user.comp_dom;
	ret = k_mem_domain_add_partition(domain, &ipc_partition);

	if (ret < 0) {
		comp_err(mod->dev,
			"modules_user_init_worker(): failed to add ipc_partition: error: %d",
			ret);
		goto err;
	}

	worker_data[sd_id]->module_ref_cnt++;

	if (worker_data[sd_id]->module_ref_cnt == 1) {
		k_work_user_init(&worker_data[sd_id]->work_item, user_handler);

		/* Grant a thread access to a kernel object (IPC msg. data). */
		k_thread_access_grant(worker_data[sd_id]->ipc_worker_tid,
				      worker_data[sd_id]->ipc_in_msg_q,
				      worker_data[sd_id]->ipc_out_msg_q);

		/* Submit work item to the queue */
		ret = k_work_user_submit_to_queue(&security_domain[sd_id].ipc_user_work_q,
						  &worker_data[sd_id]->work_item);
		if (ret < 0)
			goto err;
	}

	return ret;

err:
	user_stack_free(worker_data[sd_id]->p_worker_stack);

err_msgq:
	modules_msgq_free(worker_data[sd_id]);

err_worker:
	rfree(worker_data[sd_id]);
	return ret;
}

static void module_user_destroy(struct processing_module *mod)
{
	uint32_t sd_id = mod->dev_user.security_domain_id;

	/* Module removed so decrement counter */
	worker_data[sd_id]->module_ref_cnt--;
	/* Free worker resources if no more active user space modules */
	if (worker_data[sd_id]->module_ref_cnt == 0) {
		modules_msgq_free(worker_data[sd_id]);
		k_msgq_cleanup(worker_data[sd_id]->ipc_in_msg_q);
		k_msgq_cleanup(worker_data[sd_id]->ipc_out_msg_q);
		k_thread_abort(worker_data[sd_id]->ipc_worker_tid);
		rfree(worker_data[sd_id]);
		worker_data[sd_id] = NULL;
	}
}

static int module_user_start(struct processing_module *mod, struct module_common_params *params)
{
	struct module_init_params *p = (struct module_init_params *)params;
	comp_dbg(mod->dev, "module_user_start(): start");

	return modules_init_helper(mod, p->module_id,
				  p->instance_id,
				  p->log_handle, &p->mod_cfg);
}

static int module_user_call(struct processing_module *mod, uint32_t cmd,
			    void *params, uint32_t params_size,
			    struct user_worker_data *wr_data)
{
	struct module_common_params *common_params = params;
	struct module_data *md = &mod->priv;
	k_tid_t worker_tid = wr_data->ipc_worker_tid;
	struct k_mem_domain *domain = mod->dev_user.comp_dom;
	int ret;

	common_params->mod = mod;
	common_params->md = md;
	common_params->cmd = cmd;

	/* Switch worker thread to module memory domain */
	ret = k_mem_domain_add_thread(domain, worker_tid);
	if (ret < 0) {
		comp_err(mod->dev,
			"module_user_call(): failed to switch memory domain: error: %d",
			ret);
		return ret;
	}

	ret = k_msgq_put(wr_data->ipc_in_msg_q, params, K_FOREVER);
	if (ret < 0) {
		comp_err(mod->dev,
			"module_user_call(): k_msgq_put(): error: %d",
			ret);
		return ret;
	}


	ret = k_msgq_get(wr_data->ipc_out_msg_q, params, K_FOREVER);
	if (ret < 0) {
		comp_err(mod->dev,
			"module_user_call(): k_msgq_get(): error: %d",
			ret);
	}
	return ret;
}

static void module_user_handler(struct k_work_user *work_item)
{
	uint32_t sec_domain = CONFIG_SEC_DOMAIN_MAX_NUMBER;

	/* Check which handler is called */
	for (int i = 0; i < CONFIG_SEC_DOMAIN_MAX_NUMBER; i++) {
		if (&worker_data[i]->work_item == work_item) {
			sec_domain = i;
			break;
		}
	}

	/* This should not happen */
	if (sec_domain == CONFIG_SEC_DOMAIN_MAX_NUMBER) {
		return;
	}

	struct module_common_params *params =
			(struct module_common_params *)worker_data[sec_domain]->ipc_params;

	while(1) {

		k_msgq_get(worker_data[sec_domain]->ipc_in_msg_q, params, K_FOREVER);

		switch(params->cmd) {
		case MODULE_CMD_INIT:
			params->status = module_user_start(params->mod, params);
			break;
		case MODULE_CMD_PREPARE:
			params->status = modules_prepare_helper(params->mod);
			break;
		case MODULE_CMD_RESET:
			params->status = modules_reset_helper(params->mod);
			break;
		case MODULE_CMD_FREE:
			params->status = modules_free_helper(params->mod);
			break;
		case MODULE_CMD_SET_CONF:
			{
				struct module_large_cfg_set_params *p =
						(struct module_large_cfg_set_params *)params;
				params->status =
					modules_set_configuration_helper(p->common.mod,
									 p->config_id,
									 p->pos,
									 p->data_offset_size,
									 p->fragment,
									 p->fragment_size,
									 p->response,
									 p->response_size);
				break;
			}

		case MODULE_CMD_GET_CONF:
			{
				struct module_large_cfg_get_params *p =
						(struct module_large_cfg_get_params *)params;
				params->status =
					modules_get_configuration_helper(p->common.mod,
									 p->config_id,
									 p->pos,
									 p->data_offset_size,
									 p->fragment,
									 p->fragment_size);
				break;
			}
		case MODULE_CMD_SET_PROCMOD:
			{
				struct module_processing_mode_params *p =
						(struct module_processing_mode_params *)params;
				params->status =
					modules_set_processing_mode_helper(p->common.mod, p->mode);
				break;
			}

		case MODULE_CMD_GET_PROCMOD:
		{
			struct module_processing_mode_params *p =
					(struct module_processing_mode_params *)params;
			p->mode = modules_get_processing_mode_helper(p->common.mod);
			break;
		}

		default:
			params->status = EINVAL;
			break;
		}

		k_msgq_put(worker_data[sec_domain]->ipc_out_msg_q, params, K_FOREVER);
		k_yield();
	}
}


int modules_user_worker(struct processing_module *mod)
{
	struct module_data *md = &mod->priv;
	int ret;

	ret = modules_user_init_worker(mod, module_user_handler);
	return ret;
}

int module_init_user(struct processing_module *mod, uint32_t module_entry_point,
		     uint32_t module_id, uint32_t instance_id, uint32_t log_handle,
		     void *mod_cfg)
{
	struct user_worker_data *wr_data = mod->dev_user.wrk_ctx;
	struct module_init_params *params =
			(struct module_init_params *)wr_data->ipc_params;
	struct module_data *md = &mod->priv;
	struct k_mem_domain *domain = mod->dev_user.comp_dom;
	int ret = 0;

	comp_dbg(mod->dev, "module_init_user(): start");
	params->module_id = module_id;
	params->instance_id = instance_id;
	params->log_handle = log_handle;
	params->mod_cfg.data = (uint8_t *)md->cfg.init_data;
	params->mod_cfg.size = md->cfg.size >> 2;

	/* Add cfg buffer to memory domain */
	ret = user_add_memory(domain, (uintptr_t)md->cfg.init_data ,
			      sizeof(struct module_config), K_MEM_PARTITION_P_RW_U_RW);
	if (ret < 0)
		return ret;

	ret = module_user_call(mod, MODULE_CMD_INIT,
			       params,
			       sizeof(struct module_init_params),
			       wr_data);
	if (ret < 0)
		return ret;

	/* Remove cfg buffer from memory domain (Is this needed?) */
	ret = user_remove_memory(domain, (uintptr_t)md->cfg.init_data ,
				 sizeof(struct module_config));
	if (ret < 0)
		return ret;
	/* Return status from module code operation. */
	return params->common.status;
}

int module_prepare_user(struct processing_module *mod)
{
	struct user_worker_data *wr_data = mod->dev_user.wrk_ctx;
	struct module_common_params *params =
			(struct module_common_params *)wr_data->ipc_params;
	int ret;

	comp_dbg(mod->dev, "module_prepare_user(): start");

	ret = module_user_call(mod, MODULE_CMD_PREPARE, params,
			       sizeof(struct module_common_params),
			       wr_data);
	if (ret < 0)
		return ret;
	/* Return status from module code operation. */
	return params->status;
}

int module_reset_user(struct processing_module *mod)
{
	struct user_worker_data *wr_data = mod->dev_user.wrk_ctx;
	struct module_common_params *params =
			(struct module_common_params *)wr_data->ipc_params;
	int ret;

	comp_dbg(mod->dev, "module_reset_user(): start");

	ret = module_user_call(mod, MODULE_CMD_RESET, params,
			       sizeof(struct module_common_params),
			       wr_data);
	if (ret < 0)
		return ret;
	/* Return status from module code operation. */
	return params->status;
}

int module_free_user(struct processing_module *mod)
{
	struct user_worker_data *wr_data = mod->dev_user.wrk_ctx;
	struct module_common_params *params =
			(struct module_common_params *)wr_data->ipc_params;
	int ret;

	comp_dbg(mod->dev, "module_free_user(): start");

	ret = module_user_call(mod, MODULE_CMD_FREE, params,
			       sizeof(struct module_common_params),
			       wr_data);
	if (ret < 0)
		return ret;

	/* Destroy workqueue if this was last active userspace module */
	module_user_destroy(mod);
	/* Return status from module code operation. */
	return params->status;
}

int module_set_configuration_user(struct processing_module *mod,
				  uint32_t config_id,
				  enum module_cfg_fragment_position pos,
				  uint32_t data_offset_size,
				  const uint8_t *fragment,
				  size_t fragment_size,
				  uint8_t *response,
				  size_t response_size)
{
	struct user_worker_data *wr_data = mod->dev_user.wrk_ctx;
	struct module_large_cfg_set_params *params =
			(struct module_large_cfg_set_params *)wr_data->ipc_params;
	struct k_mem_domain *domain = mod->dev_user.comp_dom;
	int ret;

	comp_dbg(mod->dev, "module_set_configuration_user(): start");
	params->config_id = config_id;
	params->pos = pos;
	params->data_offset_size = data_offset_size;
	params->fragment = fragment;
	params->fragment_size = fragment_size;
	params->response = response;
	params->response_size = response_size;

	/* Give read access to the fragment buffer (It should be 4kB)*/
	ret = user_add_memory(domain, (uintptr_t)fragment, fragment_size,
			      K_MEM_PARTITION_P_RO_U_RO);
	if (ret < 0) {
		comp_err(mod->dev,
			"module_set_configuration_user(): add fragment to domain: error: %d",
			ret);
		return ret;
	}
	/* Give write access to the response buffer (It should be 4kB)*/
	ret = user_add_memory(domain, (uintptr_t)response, response_size,
			      K_MEM_PARTITION_P_RW_U_RW);
	if (ret < 0) {
		comp_err(mod->dev,
			"module_set_configuration_user(): add response to domain: error: %d",
			ret);
		return ret;
	}

	ret = module_user_call(mod, MODULE_CMD_SET_CONF, params,
			       sizeof(struct module_large_cfg_set_params),
			       wr_data);

	/* Remove access to buffers */
	user_remove_memory(domain, (uintptr_t)fragment, fragment_size);
	user_remove_memory(domain, (uintptr_t)response, response_size);

	if (ret < 0)
		return ret;
	/* Return status from module code operation. */
	return params->common.status;
}

int module_get_configuration_user(struct processing_module *mod,
				  uint32_t config_id,
				  enum module_cfg_fragment_position pos,
				  uint32_t *data_offset_size,
				  uint8_t *fragment,
				  size_t fragment_size)
{
	struct user_worker_data *wr_data = mod->dev_user.wrk_ctx;
	struct module_large_cfg_get_params *params =
			(struct module_large_cfg_get_params *)wr_data->ipc_params;
	struct k_mem_domain *domain = mod->dev_user.comp_dom;
	int ret;

	comp_dbg(mod->dev, "module_get_configuration_user(): start");
	params->config_id = config_id;
	params->pos = pos;
	params->data_offset_size = data_offset_size;
	params->fragment = fragment;
	params->fragment_size = fragment_size;

	/* Give write access to the fragment buffer (It should be 4kB)*/
	ret = user_add_memory(domain, (uintptr_t)fragment, fragment_size,
			      K_MEM_PARTITION_P_RW_U_RW);
	if (ret < 0) {
		comp_err(mod->dev,
			"module_set_configuration_user(): add fragment to domain: error: %d",
			ret);
		return ret;
	}

	ret = module_user_call(mod, MODULE_CMD_GET_CONF, params,
			       sizeof(struct module_large_cfg_get_params),
			       wr_data);
	/* Remove access to the buffer */
	user_remove_memory(domain, (uintptr_t)fragment, fragment_size);

	if (ret < 0)
		return ret;
	/* Return status from module code operation. */
	return params->common.status;
}

int module_set_processing_mode_user(struct processing_module *mod,
				    enum module_processing_mode mode)
{
	struct user_worker_data *wr_data = mod->dev_user.wrk_ctx;
	struct module_processing_mode_params *params =
			(struct module_processing_mode_params *)wr_data->ipc_params;
	int ret;

	comp_dbg(mod->dev, "module_set_processing_mode_user(): start");
	params->mode = mode;

	ret = module_user_call(mod, MODULE_CMD_SET_PROCMOD, params,
			       sizeof(struct module_processing_mode_params),
			       wr_data);
	if (ret < 0)
		return ret;
	/* Return status from module code operation. */
	return params->common.status;
}

enum module_processing_mode module_get_processing_mode_user(struct processing_module *mod)
{
	struct user_worker_data *wr_data = mod->dev_user.wrk_ctx;
	struct module_processing_mode_params *params =
			(struct module_processing_mode_params *)wr_data->ipc_params;
	int ret;

	comp_dbg(mod->dev, "module_get_processing_mode_user(): start");

	ret = module_user_call(mod, MODULE_CMD_GET_PROCMOD, params,
			       sizeof(struct module_processing_mode_params),
			       wr_data);
	if (ret < 0)
		return ret;
	/* Return status from module code operation. */
	return params->mode;
}
