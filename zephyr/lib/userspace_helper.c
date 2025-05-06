// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2022 Intel Corporation. All rights reserved.
//
// Author: Jaroslaw Stelter <jaroslaw.stelter@intel.com>

/**
 * \file
 * \brief Zephyr userspace helper functions
 * \authors Jaroslaw Stelter <jaroslaw.stelter@intel.com>
 */

#include <sof/audio/component.h>
#include <sof/platform.h>

#include <rtos/task.h>
#include <rtos/userspace_helper.h>
#include <stdint.h>
#include <sof/trace/trace.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/mutex.h>
#include <zephyr/sys/sem.h>

#include <zephyr/kernel/thread.h>
#include <sof/audio/module_adapter/module/generic.h>

/* Zephyr includes */
#include <version.h>
#include <zephyr/kernel.h>

#if CONFIG_USERSPACE

#define DRV_HEAP_SIZE  CONFIG_MM_DRV_PAGE_SIZE * 5

K_APPMEM_PARTITION_DEFINE(user_partition);
K_APPMEM_PARTITION_DEFINE(task_partition);
K_APPMEM_PARTITION_DEFINE(shd_partition);
K_APPMEM_PARTITION_DEFINE(common_partition);

#define MODULE_SLOT_MASK	((1<<CONFIG_UM_MODULES_MAX_NUM) - 1)
static APP_TASK_DATA uint32_t module_slot_mask;
static APP_TASK_DATA struct sys_sem proc_sem1[CONFIG_UM_MODULES_MAX_NUM];

void user_sheduler_dp_init(void)
{
	if (cpu_get_id() == 0)
		module_slot_mask = MODULE_SLOT_MASK;
}

static inline int modules_aquire_slot(void)
{
	unsigned int key = irq_lock();
	unsigned int slot = find_lsb_set(module_slot_mask);

	if (slot == 0) {
		irq_unlock(key);
		return -ENOMEM;
	}

	slot = slot -1;
	module_slot_mask = module_slot_mask & (~(1<<slot));
	irq_unlock(key);
	return slot;
}

static inline void modules_release_slot(unsigned int slot)
{
	unsigned int key = irq_lock();
	module_slot_mask = module_slot_mask | (1<<slot);
	irq_unlock(key);
}

int user_sem_acquire(uintptr_t module, struct sys_sem **sem)
{
	struct processing_module *mod = (struct processing_module *)module;

	mod->dev_user.module_slot_id = modules_aquire_slot();

	if (mod->dev_user.module_slot_id < 0)
		return -ENOMEM;

	*sem = &proc_sem1[mod->dev_user.module_slot_id];
	return 0;
}

void user_sem_release(uintptr_t module)
{
	struct processing_module *mod = (struct processing_module *)module;

	if (mod->dev_user.module_slot_id)
		modules_release_slot(mod->dev_user.module_slot_id);
}

struct sys_heap * drv_heap_init(void)
{
	struct sys_heap *drv_heap = rballoc(0, SOF_MEM_CAPS_RAM,
					       sizeof(struct sys_heap));
	if (!drv_heap)
		return NULL;

	void *mem = rballoc_align(0, SOF_MEM_CAPS_RAM,
				  DRV_HEAP_SIZE, CONFIG_MM_DRV_PAGE_SIZE);
	if (!mem) {
		rfree(drv_heap);
		return NULL;
	}

	sys_heap_init(drv_heap, mem, DRV_HEAP_SIZE);
	drv_heap->init_mem = mem;
	drv_heap->init_bytes = DRV_HEAP_SIZE;

	return drv_heap;
}

void *drv_heap_aligned_alloc(struct sys_heap *drv_heap, uint32_t flags,
			     uint32_t caps, size_t bytes, int32_t align)
{
	if (drv_heap)
		return sys_heap_aligned_alloc(drv_heap, align, bytes);
	else
		return rballoc_align(flags, caps, bytes, align);
}

void *drv_heap_rmalloc(struct sys_heap *drv_heap, enum mem_zone zone,
		       uint32_t flags, uint32_t caps, size_t bytes)
{
	if (drv_heap)
		return sys_heap_alloc(drv_heap, bytes);
	else
		return rmalloc(zone, flags, caps, bytes);
}

void *drv_heap_rzalloc(struct sys_heap *drv_heap, enum mem_zone zone,
		       uint32_t flags, uint32_t caps, size_t bytes)
{
	if (drv_heap) {
		void *mem = sys_heap_alloc(drv_heap, bytes);
		if (mem)
			memset(mem, 0, bytes);
		return mem;
	} else
		return rzalloc(zone, flags, caps, bytes);
}

void drv_heap_free(struct sys_heap *drv_heap, void *mem)
{
	if (drv_heap)
		sys_heap_free(drv_heap, mem);
	else
		rfree(mem);
}

void drv_heap_remove(struct sys_heap *drv_heap)
{
	rfree(drv_heap->init_mem);
}

void *user_stack_allocate(size_t stack_size, uint32_t options)
{
	static k_thread_stack_t *stack;

	if (CONFIG_DYNAMIC_THREAD_STACK_SIZE < stack_size)
		return NULL;
	stack = (__sparse_force void __sparse_cache *)k_thread_stack_alloc(stack_size,
				     IS_ENABLED(CONFIG_USERSPACE) ? (options & K_USER) : 0);

	return stack;
}

int user_stack_free(void *p_stack)
{
	return k_thread_stack_free((__sparse_force void *)p_stack);
}

int user_add_memory(struct k_mem_domain *domain,
		uintptr_t addr, size_t size, uint32_t attr)
{
	uintptr_t addr_aligned;
	size_t size_aligned;
	int ret = 0;

	/* Define parameters for user_partition */
	struct k_mem_partition *dp_parts[]  = { &user_partition };
	k_mem_region_align(&addr_aligned, &size_aligned, (uintptr_t)addr,
			   size, HOST_PAGE_SIZE);
	dp_parts[0]->start = addr_aligned;
	dp_parts[0]->size = size_aligned;
	dp_parts[0]->attr = attr;

	ret = k_mem_domain_add_partition(domain, &user_partition);
	/* -EINVAL means that given page is already in the domain */
	/* Not an error case for us. */
	if (ret == -EINVAL) {
		return 0;
	}

	return ret;
}

int user_remove_memory(struct k_mem_domain *domain, uintptr_t addr, size_t size)
{
	uintptr_t addr_aligned;
	size_t size_aligned;
	int ret = 0;

	/* Define parameters for user_partition */
	struct k_mem_partition *dp_parts[]  = { &user_partition };
	k_mem_region_align(&addr_aligned, &size_aligned, (uintptr_t)addr,
			   size, HOST_PAGE_SIZE);
	dp_parts[0]->start = addr_aligned;
	dp_parts[0]->size = size_aligned;
	dp_parts[0]->attr = K_MEM_PARTITION_P_RW_U_RW;

	ret = k_mem_domain_remove_partition(domain, &user_partition);
	/* -ENOENT means that given partition is already removed */
	/* Not an error case for us. */
	if (ret == -ENOENT)
		return 0;

	return ret;
}

int user_memory_init_shd(k_tid_t thread_id,
			 uintptr_t module)
{
	struct processing_module *mod = (struct processing_module *)module;
	struct k_mem_domain *comp_dom = mod->dev_user.comp_dom;

	int ret = k_mem_domain_add_partition(comp_dom, &common_partition);

	if (ret < 0)
		return ret;

	return k_mem_domain_add_thread(comp_dom, thread_id);
}

int user_memory_init(k_tid_t thread_id,
		     uintptr_t module)
{
	uintptr_t addr_aligned;
	size_t size_aligned;
	int ret = 0;
	struct processing_module *mod = (struct processing_module *)module;
	struct comp_dev *comp = mod->dev;
	struct k_mem_domain *comp_dom = NULL;
	struct module_data *md = &mod->priv;
	void *shd_addr = (void *)get_shd_heap_start();

	/* Allocate memory domain struct */
	comp_dom = sys_heap_alloc(comp->drv->drv_heap, sizeof(struct k_mem_domain));
	if (!comp_dom)
		return -ENOMEM;

	/* Add shared heap uncached and cached space to memory partitions */
	struct k_mem_partition *dp_parts[] = { &shd_partition, &user_partition, &task_partition };
	k_mem_region_align(&addr_aligned, &size_aligned, get_shd_heap_start(),
			   get_shd_heap_size(), CONFIG_MM_DRV_PAGE_SIZE);
	dp_parts[0]->start = addr_aligned;
	dp_parts[0]->size = size_aligned;
	dp_parts[0]->attr = K_MEM_PARTITION_P_RW_U_RW;

	k_mem_region_align(&addr_aligned, &size_aligned, (uintptr_t)sys_cache_cached_ptr_get(shd_addr),
			   get_shd_heap_size(), CONFIG_MM_DRV_PAGE_SIZE);
	dp_parts[1]->start = addr_aligned;
	dp_parts[1]->size = size_aligned;
	dp_parts[1]->attr = K_MEM_PARTITION_P_RW_U_RW;

	/* Add module private heap to memory partitions */
	k_mem_region_align(&addr_aligned, &size_aligned, (uintptr_t)comp->drv->drv_heap->init_mem,
			   DRV_HEAP_SIZE, CONFIG_MM_DRV_PAGE_SIZE);
	dp_parts[2]->start = addr_aligned;
	dp_parts[2]->size = size_aligned;
	dp_parts[2]->attr = K_MEM_PARTITION_P_RW_U_RW;

	ret = k_mem_domain_init(comp_dom , ARRAY_SIZE(dp_parts), dp_parts);

	if (ret != 0) {
		return ret;
	}

	ret = k_mem_domain_add_thread(comp_dom, thread_id);
	if (ret != 0) {
		return ret;
	}

	/* Store memory domain in struct processing_module */
	mod->dev_user.comp_dom = comp_dom;

	return ret;
}

#else /* CONFIG_USERSPACE */

void *user_stack_allocate(size_t stack_size, uint32_t options)
{
	/* allocate stack - must be aligned and cached so a separate alloc */
	stack_size = Z_KERNEL_STACK_SIZE_ADJUST(stack_size);
	void *p_stack = (__sparse_force void __sparse_cache *)
		rballoc_align(0, SOF_MEM_CAPS_RAM, stack_size, Z_KERNEL_STACK_OBJ_ALIGN);

	return p_stack;
}

int user_stack_free(void *p_stack)
{
	rfree((__sparse_force void *)p_stack);
	return 0;
}

void *drv_heap_rmalloc(struct sys_heap *drv_heap, enum mem_zone zone,
		   uint32_t flags, uint32_t caps, size_t bytes)
{
	return rmalloc(zone, flags, caps, bytes);
}


void *drv_heap_aligned_alloc(struct sys_heap *drv_heap, uint32_t flags,
			 uint32_t caps, size_t bytes, int32_t align)
{
	return rballoc_align(flags, caps, bytes, align);
}


void *drv_heap_rzalloc(struct sys_heap *drv_heap, enum mem_zone zone,
		   uint32_t flags, uint32_t caps, size_t bytes)
{
	return rzalloc(zone, flags, caps, bytes);
}

void drv_heap_free(struct sys_heap *drv_heap, void *mem)
{
	rfree(mem);
}

#endif /* CONFIG_USERSPACE */
