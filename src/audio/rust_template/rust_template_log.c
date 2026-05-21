// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2026 Intel Corporation.
//
// Zephyr LOG shims for the Rust rust_template module.
//
// Zephyr's LOG_*/SOF comp_* are macros bound at build time to the
// per-module string table set up by LOG_MODULE_REGISTER, so they
// can't be called from Rust directly. These tiny wrappers route a
// runtime `const char *` through `LOG_INF("%s", msg)` etc. so log
// messages from the Rust crate show up under the "rust_template"
// tag like every other SOF component.
//
// Compiled into both the llext build (rust_template/llext) and the
// firmware ZTest (sof/zephyr/test) so the Rust crate's
// `extern "C" fn sof_rust_template_log_*` references resolve in
// either link context.
//
// The Zephyr build sets CONFIG_LOG_MODE_IMMEDIATE=y, so the message
// pointer does not need to outlive the call.

#include <zephyr/logging/log.h>
#include <zephyr/kernel.h>
#include <rtos/sof.h>

LOG_MODULE_REGISTER(rust_template, CONFIG_SOF_LOG_LEVEL);

void sof_rust_template_log_err(const char *msg)  { LOG_ERR("%s", msg); }
void sof_rust_template_log_warn(const char *msg) { LOG_WRN("%s", msg); }
void sof_rust_template_log_info(const char *msg) { LOG_INF("%s", msg); }
void sof_rust_template_log_dbg(const char *msg)  { LOG_DBG("%s", msg); }

/*
 * Rust #[panic_handler] entry point. The Rust runtime is required to
 * be -> ! (never returns); we log the formatted panic message via
 * the rust_template log module and then hand the CPU to Zephyr's
 * fatal-error path via k_panic(), which is also noreturn.
 *
 * `msg` is a NUL-terminated buffer built on the Rust side that
 * already contains "rust_template panic: <reason> at <file>:<line>";
 * we don't try to reformat it here.
 */
__attribute__((noreturn))
void sof_rust_template_panic(const char *msg)
{
	LOG_ERR("%s", msg);
	k_panic();
	__builtin_unreachable();
}
