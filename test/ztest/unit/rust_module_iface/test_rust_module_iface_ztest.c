// SPDX-License-Identifier: BSD-3-Clause
//
// Verify that a Rust `ProcessingModule` impl can be invoked via the C
// `struct module_interface` function pointers.

#include <zephyr/ztest.h>
#include "rust_module_iface_demo.h"

ZTEST(sof_rust_module_iface, test_required_slots_set)
{
	zassert_not_null(rust_demo_module_interface.init,
			 "init must always be wired up");
	zassert_not_null(rust_demo_module_interface.process,
			 "process must be wired (HAS_PROCESS=true)");
	zassert_not_null(rust_demo_module_interface.reset,        "reset");
	zassert_not_null(rust_demo_module_interface.free,         "free");
	zassert_not_null(rust_demo_module_interface.trigger,      "trigger");
	zassert_not_null(rust_demo_module_interface.set_config_param,
			 "set_config_param");

	/* Slots the demo did NOT opt into must be NULL. */
	zassert_is_null(rust_demo_module_interface.prepare,
			"prepare must be NULL when HAS_PREPARE=false");
	zassert_is_null(rust_demo_module_interface.bind,
			"bind not implemented");
	zassert_is_null(rust_demo_module_interface.unbind,
			"unbind not implemented");
	zassert_is_null(rust_demo_module_interface.process_audio_stream,
			"deprecated process_audio_stream not implemented");
}

ZTEST(sof_rust_module_iface, test_init_returns_ok)
{
	int rc = rust_demo_module_interface.init(NULL);

	zassert_equal(rc, 0, "Rust init() should return 0, got %d", rc);
}

ZTEST(sof_rust_module_iface, test_process_returns_ok)
{
	int rc = rust_demo_module_interface.process(NULL, NULL, 0, NULL, 0);

	zassert_equal(rc, 0, "Rust process() should return 0, got %d", rc);
}

ZTEST(sof_rust_module_iface, test_trigger_dispatches_arg)
{
	zassert_equal(rust_demo_module_interface.trigger(NULL, 0),    0,    "cmd=0 -> 0");
	zassert_equal(rust_demo_module_interface.trigger(NULL, 7),   -7,    "cmd=7 -> -7");
	zassert_equal(rust_demo_module_interface.trigger(NULL, 42),  -42,   "cmd=42 -> -42");
}

ZTEST(sof_rust_module_iface, test_set_config_param)
{
	zassert_equal(rust_demo_module_interface.set_config_param(NULL,
				0xCAFE0000), 0,
			"matching id is accepted");
	zassert_equal(rust_demo_module_interface.set_config_param(NULL,
				0xDEAD0000), -22,
			"non-matching id returns -EINVAL");
}

ZTEST(sof_rust_module_iface, test_default_methods_via_macro_left_null)
{
	/* `bind` / `unbind` were never opted into; their C slots stay NULL. */
	zassert_is_null(rust_demo_module_interface.bind, NULL);
	zassert_is_null(rust_demo_module_interface.unbind, NULL);
}

ZTEST_SUITE(sof_rust_module_iface, NULL, NULL, NULL, NULL, NULL);
