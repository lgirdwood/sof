// SPDX-License-Identifier: BSD-3-Clause
/*
 * Copyright(c) 2026 Intel Corporation.
 *
 * Out-of-line C wrappers for the SOF module-allocator helpers
 * (`mod_alloc` / `mod_alloc_align` / `mod_free`). These are
 * `static inline` in <sof/audio/module_adapter/module/generic.h>,
 * so Rust crates that bind to them via `unsafe extern "C"` would
 * see undefined references at static-link time without these
 * wrappers.
 *
 * Used by `sof_module::alloc::SofModuleAlloc`, the
 * #[global_allocator] adapter that routes Rust allocations
 * through SOF's per-module bump allocator. The Rust side is
 * responsible for keeping the current `processing_module *`
 * available to the global allocator at call time; see
 * `sof_module::alloc::CurrentModuleGuard`.
 */

#include <stdint.h>
#include <stddef.h>
#include <sof/audio/module_adapter/module/generic.h>

/*
 * Currently-active `processing_module *` for the Rust global
 * allocator. Updated on entry/exit of every callback emitted by
 * `sof_module`'s `define_module!()` macro. The Rust side accesses
 * this symbol directly as `extern static mut` so each shim needs
 * at most one literal reference, which keeps the Xtensa BFD
 * linker's per-function `.literal` pools within `l32r`'s
 * ±256 KiB range. Single-DSP-core only for now; multi-core
 * support will need a per-CPU slot keyed off `arch_proc_id()`.
 */
struct processing_module *sof_rust_current_module;

void *sof_rust_mod_alloc(struct processing_module *mod, size_t size, size_t align)
{
	if (!mod || size == 0)
		return NULL;
	if (align <= 1)
		return mod_alloc(mod, size);
	return mod_alloc_align(mod, size, align);
}

int sof_rust_mod_free(struct processing_module *mod, const void *ptr)
{
	if (!mod || !ptr)
		return 0;
	return mod_free(mod, ptr);
}
