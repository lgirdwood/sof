/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2016 Intel Corporation. All rights reserved.
 *
 * Author: Jaroslaw Stelter <jaroslaw.stelter@intel.com>
 */

#ifndef __SOF_AUDIO_MODULES_USERSPACE_H__
#define __SOF_AUDIO_MODULES_USERSPACE_H__

#if CONFIG_USERSPACE
#include <sof/audio/component.h>
#include <sof/list.h>
#include <ipc/topology.h>
#include <kernel/abi.h>
#include <stdbool.h>

#include <zephyr/kernel.h>
#include <zephyr/app_memory/app_memdomain.h>
#include <sof/audio/module_adapter/module/generic.h>
#include <sof/audio/module_adapter/module/modules.h>

extern struct k_mem_partition ipc_partition;
#define APP_USER_DATA	K_APP_DMEM(ipc_partition)

/**
 * Creates userspace worker if called first time
 * Adds worker specific memory to processing module
 * memory domain.
 * @param mod -  pointer to processing module structure.
 * @return 0 for success, error otherwise.
 */
int modules_user_worker(struct processing_module *mod);

/**
 * Copy parameters to user worker accessible space.
 * Queue module init() operation and return its result.
 * Module init() code is performed in user workqueue.
 *
 * @param mod -  pointer to processing module structure.
 * @param module_entry_point -  module entry point address from
 *               its manifest
 * @param module_id -  module id
 * @param instance_id -  instance id
 * @param log_handle -  log handler for module
 * @param mod_cfg -  module configuration
 * @return 0 for success, error otherwise.
 */
int module_init_user(struct processing_module *mod,
		     uint32_t module_entry_point,
		     uint32_t module_id,
		     uint32_t instance_id,
		     uint32_t log_handle,
		     void *mod_cfg);

/**
 * Copy parameters to user worker accessible space.
 * Queue module prepare() operation and return its result.
 * Module prepare() code is performed in user workqueue.
 *
 * @param mod -  pointer to processing module structure.
 * @return 0 for success, error otherwise.
 */
int module_prepare_user(struct processing_module *mod);

/**
 * Copy parameters to user worker accessible space.
 * Queue module reset() operation and return its result.
 * Module reset() code is performed in user workqueue.
 *
 * @param mod -  pointer to processing module structure.
 * @return 0 for success, error otherwise.
 */
int module_reset_user(struct processing_module *mod);

/**
 * Copy parameters to user worker accessible space.
 * Queue module free() operation and return its result.
 * Module free() code is performed in user workqueue.
 *
 * @param mod -  pointer to processing module structure.
 * @return 0 for success, error otherwise.
 */
int module_free_user(struct processing_module *mod);

/**
 * Copy parameters to user worker accessible space.
 * Queue module set_configuration() operation and return
 * its result.
 * Module set_configuration() code is performed in user workqueue.
 *
 * @param[in] mod - struct processing_module pointer
 * @param[in] config_id - Configuration ID
 * @param[in] pos - position of the fragment in the large message
 * @param[in] data_offset_size: size of the whole configuration if it is the first fragment or the
 *	      only fragment. Otherwise, it is the offset of the fragment in the whole
 *	      configuration.
 * @param[in] fragment: configuration fragment buffer
 * @param[in] fragment_size: size of @fragment
 * @params[in] response: optional response buffer to fill
 * @params[in] response_size: size of @response
 *
 * @return 0 for success, error otherwise.
 */
int module_set_configuration_user(struct processing_module *mod,
				  uint32_t config_id,
				  enum module_cfg_fragment_position pos,
				  uint32_t data_offset_size,
				  const uint8_t *fragment,
				  size_t fragment_size,
				  uint8_t *response,
				  size_t response_size);

/**
 * Copy parameters to user worker accessible space.
 * Queue module get_configuration() operation and return
 * its result.
 * Module get_configuration() code is performed in user workqueue.
 *
 * @param[in] mod - struct processing_module pointer
 * @param[in] config_id - Configuration ID
 * @param[in] pos - position of the fragment in the large message
 * @param[in] data_offset_size: size of the whole configuration if it is the first fragment or the
 *	      only fragment. Otherwise, it is the offset of the fragment in the whole
 *	      configuration.
 * @param[in] fragment: configuration fragment buffer
 * @param[in] fragment_size: size of @fragment
 *
 * @return 0 for success, error otherwise.
 */
int module_get_configuration_user(struct processing_module *mod,
				  uint32_t config_id,
				  enum module_cfg_fragment_position pos,
				  uint32_t *data_offset_size,
				  uint8_t *fragment,
				  size_t fragment_size);

/**
 * Copy parameters to user worker accessible space.
 * Queue module set_processing_mode() operation and return
 * its result.
 * Module set_processing_mode() code is performed in user workqueue.
 *
 * @param mod -  pointer to processing module structure.
 * @param mode -  processing mode to be set.
 * @return 0 for success, error otherwise.
 */
int module_set_processing_mode_user(struct processing_module *mod,
				    enum module_processing_mode mode);

/**
 * Copy parameters to user worker accessible space.
 * Queue module get_processing_mode() operation and return
 * its result.
 * Module get_processing_mode() code is performed in user workqueue.
 *
 * @param mod -  pointer to processing module structure.
 * @return processing mode.
 */
enum module_processing_mode module_get_processing_mode_user(struct processing_module *mod);

#else

#define APP_USER_DATA

#endif /* CONFIG_USERSPACE */
#endif /* __SOF_AUDIO_MODULES_USERSPACE_H__ */
