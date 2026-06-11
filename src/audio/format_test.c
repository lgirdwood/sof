/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2025 Intel Corporation. All rights reserved.
 */

#include <sof/audio/format.h>

/*
 * Lightweight runtime sanity checks used by pipeline stop hook.
 * Full macro/function validation lives in cmocka unit tests.
 */
void format_intrinsic_test_run(void)
{
	volatile int32_t a = sat_int8(0x200);
	volatile int32_t b = sat_int16(-0x10000);
	volatile int32_t c = sat_int24(0x1000000);
	volatile int32_t d = sat_int32(0x100000000LL);

	(void)a;
	(void)b;
	(void)c;
	(void)d;
}
