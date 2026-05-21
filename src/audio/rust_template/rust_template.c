// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2026 Intel Corporation.
//
// SOF audio module that delegates the entire module_interface to a Rust
// implementation built on the sof_module crate. The Rust side defines the
// `rust_template_interface` symbol via sof_module's `define_module!()`
// macro; this file only carries the SOF metadata (UUID, log module,
// llext manifest) the firmware loader expects.

#include <sof/audio/module_adapter/module/generic.h>
#include <module/module/api_ver.h>
#include <module/module/llext.h>
#include <rimage/sof/user/manifest.h>
#include <rtos/init.h>

SOF_DEFINE_REG_UUID(rust_template);

LOG_MODULE_DECLARE(rust_template, CONFIG_SOF_LOG_LEVEL);

/* Provided by the Rust crate via sof_module::define_module!(). */
extern const struct module_interface rust_template_interface;

static const struct sof_man_module_manifest mod_manifest __section(".module") __used =
	SOF_LLEXT_MODULE_MANIFEST("RUSTTPL", &rust_template_interface, 1,
				  SOF_REG_UUID(rust_template), 40);

SOF_LLEXT_BUILDINFO;
