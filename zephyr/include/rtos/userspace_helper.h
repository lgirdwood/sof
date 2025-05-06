/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2023 Intel Corporation. All rights reserved.
 */

/**
 * \file ace/lib/user.h
 * \brief Userspace support functions.
 */
#ifndef __SOF_LIB_USERSPACE_HELPER_H__
#define __SOF_LIB_USERSPACE_HELPER_H__

#include <zephyr/sys/sys_heap.h>
#include <zephyr/sys/sem.h>
#include <zephyr/app_memory/app_memdomain.h>

#ifdef CONFIG_USERSPACE

#define APP_TASK_DATA	K_APP_DMEM(common_partition)

/* Processing module structure fields needed for user mode */
struct processing_module_user {
	struct k_mem_domain *comp_dom;	/* Module specific memory domain        */
	uint32_t security_domain_id;	/* Determine security domain id number  */
	uint32_t module_slot_id;	/* Module slot number                   */
	void *wrk_ctx;			/* Work queue context                   */
};

/**
 * Initialize private processing module heap.
 * @param N/A.
 * @return pointer to the sys_heap structure.
 *
 * @note
 * Function used only when CONFIG_USERSPACE is set.
 * The private heap is used only for non-privileged modules
 * for all processing module allocations that should be isolated.
 * The heap helps to accumulate all dynamic allocations in single
 * memory region which is then added to modules memory domain.
 */
struct sys_heap * drv_heap_init(void);

void *drv_heap_alloc(struct sys_heap *drv_heap, size_t bytes);

/**
 * Free private processing module heap.
 * @param sys_heap pointer to the sys_heap structure.
 *
 * @note
 * Function used only when CONFIG_USERSPACE is set.
 * Frees private module heap.
 */
void drv_heap_remove(struct sys_heap *drv_heap);

/**
 * Add memory region to non-privileged module memory domain.
 * @param domain - pointer to the modules memory domain.
 * @param addr   - pointer to memory region start
 * @param size   - size of the memory region
 * @param attr   - memory region access attributes
 *
 * @return 0 for success, error otherwise.
 *
 * @note
 * Function used only when CONFIG_USERSPACE is set.
 * Function adds page aligned region to the memory domain.
 * Caller should take care to not expose other data than these
 * intended to be shared with the module.
 */
int user_add_memory(struct k_mem_domain *domain,
		    uintptr_t addr,
		    size_t size,
		    uint32_t attr);

/**
 * Remove memory region from non-privileged module memory domain.
 * @param domain - pointer to the modules memory domain.
 * @param addr   - pointer to memory region start
 * @param size   - size of the memory region
 *
 * @return 0 for success, error otherwise.
 *
 * @note
 * Function used only when CONFIG_USERSPACE is set.
 * Function removes previously added page aligned region
 * from the memory domain.
 */
int user_remove_memory(struct k_mem_domain *domain,
		       uintptr_t addr,
		       size_t size);



int user_memory_init_shd(k_tid_t thread_id,
			 uintptr_t module);

/**
 * Add DP scheduler created thread to module memory domain.
 * @param thread_id - id of thread to be added to memory domain.
 * @param module    - processing module strucutre
 *
 * @return 0 for success, error otherwise.
 *
 * @note
 * Function used only when CONFIG_USERSPACE is set.
 */
int user_memory_init_shd(k_tid_t thread_id,
			 uintptr_t module);
/**
 * Create module memory domain, adds the domain to given thread
 * and add memory regions required.
 * @param thread_id - id of thread to be added to memory domain.
 * @param module    - processing module strucutre
 *
 * @return 0 for success, error otherwise.
 *
 * @note
 * Function used only when CONFIG_USERSPACE is set.
 */
int user_memory_init(k_tid_t thread_id,
		      uintptr_t module);

/* Folowing functions are added to workarround zephyr limitations */
/* Currently Zephyr supports only static objects for mutex and
 * semaphores. Therefore we need to pre-allocate such resources
 * and manage usage in runtime. */

/**
 * Acquire module slot for newly created module.
 * Set semaphore pointer to relevant static semaphore.
 * @param module    - processing module strucutre
 * @sem             - pointer to semaphore structure used by dp_scheduler
 *
 * @return 0 for success, error otherwise.
 *
 * @note
 * Function used only when CONFIG_USERSPACE is set.
 */
int user_sem_acquire(uintptr_t module, struct sys_sem **sem);

/**
 * Free module slot associated with the module.
 * Set semaphore pointer to relevant static semaphore.
 * @param module    - processing module strucutre
 *
 * @note
 * Function used only when CONFIG_USERSPACE is set.
 */
void user_sem_release(uintptr_t module);

/**
 * Initial module_slot_mask with available number
 * of slots (depends on CONFIG_UM_MODULES_MAX_NUM).
 * @note
 * Function used only when CONFIG_USERSPACE is set.
 */
void user_sheduler_dp_init(void);

#endif

/**
 * Allocates thread stack memory.
 * @param stack_size Required stack size.
 * @param options Stack configuration options
 *        K_USER - when creating user thread
 *        0      - when creating kernel thread
 * @return pointer to the stack or NULL if not created.
 *
 * When CONFIG_USERSPACE not set function calls rballoc_align(),
 * otherwise it uses k_thread_stack_alloc() routine.
 *
 */
void *user_stack_allocate(size_t stack_size, uint32_t options);

/**
 * Free thread stack memory.
 * @param p_stack Pointer to the stack.
 *
 * @return 0 for success, error otherwise.
 *
 * @note
 * When CONFIG_USERSPACE not set function calls rfree(),
 * otherwise it uses k_thread_stack_free() routine.
 *
 */
int user_stack_free(void *p_stack);

/**
 * Allocates memory block from private module sys_heap if exists,
 * otherwise call rballoc_align().
 * @param sys_heap - pointer to the sys_heap structure
 * @param flags    - Flags, see SOF_MEM_FLAG_...
 * @param caps     - Capabilities, see SOF_MEM_CAPS_...
 * @param bytes     - Size in bytes.
 * @param alignment - Alignment in bytes.
 * @return Pointer to the allocated memory or NULL if failed.
 *
 * @note When CONFIG_USERSPACE not set function calls rballoc_align()
 */
void *drv_heap_aligned_alloc(struct sys_heap *drv_heap, uint32_t flags,
			     uint32_t caps, size_t bytes, int32_t align);

/**
 * Allocates memory block block from private module sys_heap if exists,
 * otherwise call rmalloc.
 * @param sys_heap - pointer to the sys_heap structure
 * @param zone     - Zone to allocate memory from, see enum mem_zone.
 * @param flags    - Flags, see SOF_MEM_FLAG_...
 * @param caps     - Capabilities, see SOF_MEM_CAPS_...
 * @param bytes    - Size in bytes.
 * @return         - Pointer to the allocated memory or NULL if failed.
 *
 *  * @note When CONFIG_USERSPACE not set function calls rmalloc()
 */
void *drv_heap_rmalloc(struct sys_heap *drv_heap, enum mem_zone zone,
		       uint32_t flags, uint32_t caps, size_t bytes);

/**
 * Similar to user_rmalloc(), guarantees that returned block is zeroed.
 *
 * @note When CONFIG_USERSPACE not set function calls rzalloc()
 */
void *drv_heap_rzalloc(struct sys_heap *drv_heap, enum mem_zone zone,
		       uint32_t flags, uint32_t caps, size_t bytes);

/**
 * Frees the memory block from private module sys_heap if exists.
 * otherwise call rfree.
 * @param ptr Pointer to the memory block.
 *
 * @note User should take care to not free memory allocated from sys_heap
 *       with drv_heap set to NULL. It will cause exception.
 *
 *       When CONFIG_USERSPACE not set function calls rfree()
 */
void drv_heap_free(struct sys_heap *drv_heap, void *mem);

#endif /* __SOF_LIB_USERSPACE_HELPER_H__ */
