// SPDX-License-Identifier: BSD-3-Clause
/*
 * Copyright(c) 2020 Intel Corporation. All rights reserved.
 *
 * Author: Jaroslaw Stelter <jaroslaw.stelter@linux.intel.com>
 */

#include <sof/audio/module_adapter/module/generic.h>
#include <sof/audio/module_adapter/module/modules.h>
#include <sof/audio/module_adapter/module/modules_userspace.h>
#include <utilities/array.h>
#include <iadk_module_adapter.h>
#include <system_agent.h>
#include <sof/lib_manager.h>
#include <sof/audio/module_adapter/module/module_interface.h>
#include <sof/audio/module_adapter/library/native_system_agent.h>

/* Intel module adapter is an extension to SOF module adapter component that allows to integrate
 * modules developed under IADK (Intel Audio Development Kit) Framework. IADK modules uses uniform
 * set of interfaces and are linked into separate library. These modules are loaded in runtime
 * through library_manager and then after registration into SOF component infrastructure are
 * interfaced through module adapter API.
 *
 * There is variety of modules developed under IADK Framework by 3rd party vendors. The assumption
 * here is to integrate these modules with SOF infrastructure without modules code modifications.
 * Another assumption is that the 3rd party modules should be loaded in runtime without need
 * of rebuild the base firmware.
 * Therefore C++ function, structures and variables definition are here kept with original form from
 * IADK Framework. This provides binary compatibility for already developed 3rd party modules.
 *
 * Since IADK modules uses ProcessingModuleInterface to control/data transfer and AdspSystemService
 * to use base FW services from internal module code, there is a communication shim layer defined
 * in intel directory.
 *
 * Since ProcessingModuleInterface consists of virtual functions, there are C++ -> C iadk_wrappers
 * defined to access the interface calls from SOF code.
 *
 * There are three entities in intel module adapter package:
 *  - System Agent - A mediator to allow the custom module to interact with the base SOF FW.
 *                   It calls IADK module entry point and provides all necessary information to
 *                   connect both sides of ProcessingModuleInterface and System Service.
 *  - System Service - exposes of SOF base FW services to the module.
 *  - Processing Module Adapter - SOF base FW side of ProcessingModuleInterface API
 */

LOG_MODULE_REGISTER(sof_modules, CONFIG_SOF_LOG_LEVEL);
SOF_DEFINE_REG_UUID(modules);
DECLARE_TR_CTX(intel_codec_tr, SOF_UUID(modules_uuid), LOG_LEVEL_INFO);

int modules_init_helper(struct processing_module *mod, uint32_t module_id,
			       uint32_t instance_id, uint32_t log_handle, void *mod_cfg)
{
	struct module_data *md = &mod->priv;
	int ret = -EINVAL;
	struct comp_dev *dev = mod->dev;
	const struct comp_driver *const drv = dev->drv;
	const struct ipc4_base_module_cfg *src_cfg = &md->cfg.base_cfg;
	const struct comp_ipc_config *config = &dev->ipc_config;
	void *adapter;

	uintptr_t module_entry_point = lib_manager_allocate_module(config, src_cfg);

	if (module_entry_point == 0) {
		comp_err(dev, "modules_init(), lib_manager_allocate_module() failed!");
		return -EINVAL;
	}

	/* Call module specific init function if exists. */
	if (mod->is_native_sof) {
		((struct comp_driver *)drv)->adapter_ops = native_system_agent_start(module_entry_point,
							       module_id, instance_id, 0,
							       log_handle,
							       mod_cfg);
		if (!drv->adapter_ops)
			return ret;

		struct module_interface *mod_in =
					(struct module_interface *)drv->adapter_ops;

		/* The order of preference */
		if (mod_in->process)
			mod->proc_type = MODULE_PROCESS_TYPE_SOURCE_SINK;
		else if (mod_in->process_audio_stream)
			mod->proc_type = MODULE_PROCESS_TYPE_STREAM;
		else if (mod_in->process_raw_data)
			mod->proc_type = MODULE_PROCESS_TYPE_RAW;
		else
			return -EINVAL;

		ret = mod_in->init(mod);
	} else {
		mod->proc_type = MODULE_PROCESS_TYPE_RAW;
		// TODO: fix
		//((struct comp_driver *)drv)->adapter_ops = system_agent_start(module_entry_point, module_id,
		//			     instance_id, 0, log_handle, mod_cfg, &adapter);

		if (drv->adapter_ops)
			ret = iadk_wrapper_init((void *)drv->adapter_ops);
	}
	return ret;
}

int modules_prepare_helper(struct processing_module *mod)
{
	struct comp_dev *dev = mod->dev;
	const struct comp_driver *const drv = dev->drv;
	if (mod->is_native_sof) {
		struct module_interface *mod_in =
					(struct module_interface *)drv->adapter_ops;

			return mod_in->prepare(mod, NULL, 0, NULL, 0);
	} else {

		return iadk_wrapper_prepare(module_get_private_data(mod));
	}
}

int modules_free_helper(struct processing_module *mod)
{
	struct comp_dev *dev = mod->dev;
	const struct comp_driver *const drv = dev->drv;

	if (mod->is_native_sof) {
		struct module_interface *mod_in =
					(struct module_interface *)drv->adapter_ops;

		return mod_in->free(mod);
	} else {
		return iadk_wrapper_free(module_get_private_data(mod));
	}
}

int modules_reset_helper(struct processing_module *mod)
{
	struct comp_dev *dev = mod->dev;
	const struct comp_driver *const drv = dev->drv;
	if (mod->is_native_sof) {
		struct module_interface *mod_in =
					(struct module_interface *)drv->adapter_ops;

		return mod_in->reset(mod);
	}
	return iadk_wrapper_reset(module_get_private_data(mod));
}

int modules_set_configuration_helper(struct processing_module *mod, uint32_t config_id,
		     enum module_cfg_fragment_position pos,
		     uint32_t data_offset_size, const uint8_t *fragment,
		     size_t fragment_size, uint8_t *response,
		     size_t response_size)
{
	struct comp_dev *dev = mod->dev;
	const struct comp_driver *const drv = dev->drv;

	if (mod->is_native_sof) {
		struct module_interface *mod_in =
					(struct module_interface *)drv->adapter_ops;

		return mod_in->set_configuration(mod, config_id, pos,
						 data_offset_size,
						 fragment, fragment_size,
						 response, response_size);
	}
	return iadk_wrapper_set_configuration(module_get_private_data(mod),
					      config_id, pos, data_offset_size,
					      fragment, fragment_size, response,
					      response_size);
}

int modules_get_configuration_helper(struct processing_module *mod, uint32_t config_id,
		     enum module_cfg_fragment_position pos,
		     uint32_t *data_offset_size, uint8_t *fragment,
		     size_t fragment_size)
{
	struct comp_dev *dev = mod->dev;
	const struct comp_driver *const drv = dev->drv;

	if (mod->is_native_sof) {
		struct module_interface *mod_in =
					(struct module_interface *)drv->adapter_ops;

		return mod_in->get_configuration(mod, config_id,
						 data_offset_size,
						 fragment, fragment_size);
	}
	return iadk_wrapper_get_configuration(module_get_private_data(mod),
					      config_id, pos, data_offset_size,
					      fragment, fragment_size);
}

int modules_set_processing_mode_helper(struct processing_module *mod,
				       enum module_processing_mode mode)
{
	struct comp_dev *dev = mod->dev;
	const struct comp_driver *const drv = dev->drv;

	if (mod->is_native_sof) {
		struct module_interface *mod_in =
					(struct module_interface *)drv->adapter_ops;

		return mod_in->set_processing_mode(mod, mode);
	}
	return iadk_wrapper_set_processing_mode(module_get_private_data(mod), mode);
}

enum module_processing_mode modules_get_processing_mode_helper(struct processing_module *mod)
{
	struct comp_dev *dev = mod->dev;
	const struct comp_driver *const drv = dev->drv;
	if (mod->is_native_sof) {
		struct module_interface *mod_in =
					(struct module_interface *)drv->adapter_ops;

		return mod_in->get_processing_mode(mod);
	}
	return iadk_wrapper_get_processing_mode(module_get_private_data(mod));
}

/**
 * \brief modules_init.
 * \param[in] mod - processing module pointer.
 *
 * \return: zero on success
 *          error code on failure
 */
static int modules_init(struct processing_module *mod)
{
	struct module_data *md = &mod->priv;
	struct comp_dev *dev = mod->dev;
	const struct comp_driver *const drv = dev->drv;
	const struct ipc4_base_module_cfg *src_cfg = &md->cfg.base_cfg;
	const struct comp_ipc_config *config = &dev->ipc_config;
	void *adapter;
	int ret;

	const uint32_t module_id = IPC4_MOD_ID(mod->dev->ipc_config.id);
	const uint32_t instance_id = IPC4_INST_ID(mod->dev->ipc_config.id);
	const uint32_t log_handle = (uint32_t) mod->dev->drv->tctx;

	const struct sof_man_module *module_entry = lib_manager_get_man_module(module_id);

	uintptr_t module_entry_point = lib_manager_allocate_module(config, src_cfg);

	if (module_entry_point == 0) {
		comp_err(dev, "modules_init(), lib_manager_allocate_module() failed!");
		return -EINVAL;
	}

#if CONFIG_USERSPACE
	if (module_entry->type.user_mode) {
		mod->is_non_priviledged = true;
		int ret = modules_user_worker(mod);
		if (ret < 0) {
			comp_err(dev, "modules_init(), modules_user_worker() failed!");
			return -EINVAL;
		}
	}
#endif

	/* At this point module resources are allocated and it is moved to L2 memory. */
	comp_info(dev, "modules_init() start");

	byte_array_t mod_cfg = {
		.data = (uint8_t *)md->cfg.init_data,
		/* Intel modules expects DW size here */
		.size = md->cfg.size >> 2,
	};

	ret = system_agent_start(module_entry_point, module_id, instance_id, 0, log_handle,
				 &mod_cfg, &adapter);
	if (ret) {
		comp_info(dev, "System agent failed");
		return ret;
	}

	module_set_private_data(mod, adapter);

	md->mpd.in_buff_size = src_cfg->ibs;
	md->mpd.out_buff_size = src_cfg->obs;

	mod->proc_type = MODULE_PROCESS_TYPE_SOURCE_SINK;
	return iadk_wrapper_init(adapter);
}

/**
 * \brief modules_prepare.
 * \param[in] mod - processing module pointer.
 *
 * \return: zero on success
 *          error code on failure
 *
 * \note:   We use ipc4_base_module_cfg since this is only what we know about module
 *          configuration. Its internal structure is proprietary to the module implementation.
 *          There is one assumption - all IADK modules utilize IPC4 protocol.
 */
static int modules_prepare(struct processing_module *mod,
			   struct sof_source **sources, int num_of_sources,
			   struct sof_sink **sinks, int num_of_sinks)
{
	struct comp_dev *dev = mod->dev;

	comp_info(dev, "modules_prepare()");

#if CONFIG_USERSPACE
	if (mod->is_non_priviledged) {
		return module_prepare_user(mod);
	}
#endif

	return iadk_wrapper_prepare(module_get_private_data(mod));
}

static int modules_process(struct processing_module *mod,
			   struct sof_source **sources, int num_of_sources,
			   struct sof_sink **sinks, int num_of_sinks)
{
	return iadk_wrapper_process(module_get_private_data(mod), sources,
				    num_of_sources, sinks, num_of_sinks);
}

/**
 * \brief modules_free.
 * \param[in] mod - processing module pointer.
 *
 * \return: zero on success
 *          error code on failure
 */
static int modules_free(struct processing_module *mod)
{
	struct comp_dev *dev = mod->dev;
	int ret = 0;

	comp_info(dev, "modules_free()");

	if (!mod->is_non_priviledged)
		ret = iadk_wrapper_free(module_get_private_data(mod));
#if CONFIG_USERSPACE
	else
		ret = module_free_user(mod);
#endif
	if (ret)
		comp_err(dev, "modules_free(): iadk_wrapper_free failed with error: %d", ret);

	return ret;
}

/*
 * \brief modules_set_configuration - Common method to assemble large configuration message
 * \param[in] mod - struct processing_module pointer
 * \param[in] config_id - Configuration ID
 * \param[in] pos - position of the fragment in the large message
 * \param[in] data_offset_size: size of the whole configuration if it is the first fragment or the
 *	      only fragment. Otherwise, it is the offset of the fragment in the whole
 *	      configuration.
 * \param[in] fragment: configuration fragment buffer
 * \param[in] fragment_size: size of @fragment
 * \params[in] response: optional response buffer to fill
 * \params[in] response_size: size of @response
 *
 * \return: 0 upon success or error upon failure
 */
static int modules_set_configuration(struct processing_module *mod, uint32_t config_id,
				     enum module_cfg_fragment_position pos,
				     uint32_t data_offset_size, const uint8_t *fragment,
				     size_t fragment_size, uint8_t *response,
				     size_t response_size)
{
	#if CONFIG_USERSPACE
	if (mod->is_non_priviledged)
		return module_set_configuration_user(mod,
						     config_id, pos,
						     data_offset_size,
						     fragment,
						     fragment_size,
						     response,
						     response_size);
#endif
	return iadk_wrapper_set_configuration(module_get_private_data(mod), config_id, pos,
					      data_offset_size, fragment, fragment_size,
					      response, response_size);
}

/*
 * \brief modules_get_configuration - Common method to retrieve module configuration
 * \param[in] mod - struct processing_module pointer
 * \param[in] config_id - Configuration ID
 * \param[in] pos - position of the fragment in the large message
 * \param[in] data_offset_size: size of the whole configuration if it is the first fragment or the
 *	      only fragment. Otherwise, it is the offset of the fragment in the whole configuration.
 * \param[in] fragment: configuration fragment buffer
 * \param[in] fragment_size: size of @fragment
 *
 * \return: 0 upon success or error upon failure
 */
static int modules_get_configuration(struct processing_module *mod, uint32_t config_id,
				     uint32_t *data_offset_size, uint8_t *fragment,
				     size_t fragment_size)
{
	#if CONFIG_USERSPACE
	if (mod->is_non_priviledged)
		return module_get_configuration_user(mod,
						     config_id,
						     MODULE_CFG_FRAGMENT_SINGLE,
						     data_offset_size,
						     fragment,
						     fragment_size);
#endif
	return iadk_wrapper_get_configuration(module_get_private_data(mod), config_id,
					      MODULE_CFG_FRAGMENT_SINGLE, data_offset_size,
					      fragment, fragment_size);
}

/**
 * \brief Sets the processing mode for the module.
 * \param[in] mod - struct processing_module pointer
 * \param[in] mode - module processing mode to be set
 *
 * \return: 0 upon success or error upon failure
 */
static int modules_set_processing_mode(struct processing_module *mod,
				       enum module_processing_mode mode)
{
	#if CONFIG_USERSPACE
	if (mod->is_non_priviledged)
		return module_set_processing_mode_user(mod, mode);
#endif
	return iadk_wrapper_set_processing_mode(module_get_private_data(mod), mode);
}

/**
 * \brief Gets the processing mode actually set for the module.
 * \param[in] mod - struct processing_module pointer
 *
 * \return: enum - module processing mode value
 */
static enum module_processing_mode modules_get_processing_mode(struct processing_module *mod)
{
	#if CONFIG_USERSPACE
	if (mod->is_non_priviledged)
		return module_get_processing_mode_user(mod);
#endif
	return iadk_wrapper_get_processing_mode(module_get_private_data(mod));
}

/**
 * \brief Upon call to this method the ADSP system requires the module to reset its
 * internal state into a well-known initial value.
 * \param[in] mod - struct processing_module pointer
 *
 * \return: 0 upon success or error upon failure
 */
static int modules_reset(struct processing_module *mod)
{
#if CONFIG_USERSPACE
	if (mod->is_non_priviledged)
		return module_reset_user(mod);
#endif
	return iadk_wrapper_reset(module_get_private_data(mod));
}
extern const struct module_interface processing_module_adapter_interface;

/* Processing Module Adapter API*/
const struct module_interface processing_module_adapter_interface = {
	.init = modules_init,
	.prepare = modules_prepare,
	.process = modules_process,
	.set_processing_mode = modules_set_processing_mode,
	.get_processing_mode = modules_get_processing_mode,
	.set_configuration = modules_set_configuration,
	.get_configuration = modules_get_configuration,
	.reset = modules_reset,
	.free = modules_free,
};
