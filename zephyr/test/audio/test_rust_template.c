// SPDX-License-Identifier: BSD-3-Clause
/*
 * Copyright(c) 2026 Intel Corporation.
 *
 * ZTest for the all-Rust SOF audio module skeleton
 * (sof/src/audio/rust_template/rust). The Rust crate exports a
 * `struct module_interface rust_template_interface` via
 * sof_module's `define_module!()` macro; this test directly invokes
 * the function pointers it carries to confirm the Rust shim is
 * correctly populated and that the `extern "C"` ABI lines up with
 * the C `struct module_interface` layout from
 * <module/module/interface.h>.
 *
 * Note: the rust_template skeleton ignores its `struct
 * processing_module *` argument, so passing NULL is safe and lets
 * the test stay self-contained (no module-adapter setup required).
 */

#include <zephyr/kernel.h>
#include <zephyr/ztest.h>

#include <module/module/interface.h>

/* Provided by the Rust staticlib built from rust_template/rust. */
extern struct module_interface rust_template_interface;
extern void rust_template_swap_lr_s16(const int16_t *src, int16_t *dst,
				      size_t n_samples);

ZTEST(sof_rust_template_suite, test_rust_template_interface_populated)
{
	/* The crate opts into init/process/reset/free via
	 * `ProcessingModule::HAS_*` and leaves everything else NULL.
	 * Confirm that pattern survived the FFI boundary unchanged.
	 */
	zassert_not_null(rust_template_interface.init,
			 "init slot must be populated");
	zassert_not_null(rust_template_interface.process,
			 "process slot must be populated");
	zassert_not_null(rust_template_interface.reset,
			 "reset slot must be populated");
	zassert_not_null(rust_template_interface.free,
			 "free slot must be populated");

	zassert_is_null(rust_template_interface.prepare,
			"prepare slot must be NULL");
	zassert_is_null(rust_template_interface.is_ready_to_process,
			"is_ready_to_process slot must be NULL");
	zassert_is_null(rust_template_interface.process_audio_stream,
			"process_audio_stream slot must be NULL");
	zassert_is_null(rust_template_interface.process_raw_data,
			"process_raw_data slot must be NULL");
	zassert_is_null(rust_template_interface.bind,
			"bind slot must be NULL");
	zassert_is_null(rust_template_interface.unbind,
			"unbind slot must be NULL");
	zassert_is_null(rust_template_interface.trigger,
			"trigger slot must be NULL");
}

ZTEST(sof_rust_template_suite, test_rust_template_init)
{
	int rc = rust_template_interface.init(NULL);

	zassert_equal(rc, 0, "rust_template init() must return 0, got %d", rc);
}

ZTEST(sof_rust_template_suite, test_rust_template_process)
{
	/* The Rust skeleton's process() is a pass-through that ignores
	 * its source/sink arrays, so passing NULL/0 is safe.
	 */
	int rc = rust_template_interface.process(NULL, NULL, 0, NULL, 0);

	zassert_equal(rc, 0,
		      "rust_template process() must return 0, got %d", rc);
}

ZTEST(sof_rust_template_suite, test_rust_template_reset)
{
	int rc = rust_template_interface.reset(NULL);

	zassert_equal(rc, 0, "rust_template reset() must return 0, got %d", rc);
}

ZTEST(sof_rust_template_suite, test_rust_template_free)
{
	int rc = rust_template_interface.free(NULL);

	zassert_equal(rc, 0, "rust_template free() must return 0, got %d", rc);
}

ZTEST(sof_rust_template_suite, test_rust_template_swap_lr_s16_basic)
{
	/* Three stereo frames: L0,R0, L1,R1, L2,R2. */
	const int16_t in[]  = { 1, 2, 3, 4, 5, 6 };
	int16_t       out[6] = { 0 };
	const int16_t expect[] = { 2, 1, 4, 3, 6, 5 };

	rust_template_swap_lr_s16(in, out, ARRAY_SIZE(in));

	for (size_t i = 0; i < ARRAY_SIZE(in); i++) {
		zassert_equal(out[i], expect[i],
			      "swap_lr_s16: out[%zu]=%d, expected %d",
			      i, out[i], expect[i]);
	}
}

ZTEST(sof_rust_template_suite, test_rust_template_swap_lr_s16_odd_tail)
{
	/* Two stereo frames + one trailing sample: trailing sample is
	 * copied through unchanged.
	 */
	const int16_t in[]  = { 10, 20, 30, 40, 50 };
	int16_t       out[5] = { 0 };
	const int16_t expect[] = { 20, 10, 40, 30, 50 };

	rust_template_swap_lr_s16(in, out, ARRAY_SIZE(in));

	for (size_t i = 0; i < ARRAY_SIZE(in); i++) {
		zassert_equal(out[i], expect[i],
			      "swap_lr_s16 (odd tail): out[%zu]=%d, expected %d",
			      i, out[i], expect[i]);
	}
}

ZTEST(sof_rust_template_suite, test_rust_template_swap_lr_s16_null_safe)
{
	int16_t out[2] = { 0xa5, 0x5a };

	/* NULL pointers and zero length must be ignored, leaving the
	 * destination untouched.
	 */
	rust_template_swap_lr_s16(NULL, out, 2);
	zassert_equal(out[0], 0xa5, "NULL src must not modify dst");
	zassert_equal(out[1], 0x5a, "NULL src must not modify dst");

	const int16_t in[2] = { 1, 2 };
	rust_template_swap_lr_s16(in, NULL, 2);
	/* Survival is the only assertion here. */

	rust_template_swap_lr_s16(in, out, 0);
	zassert_equal(out[0], 0xa5, "n_samples=0 must not modify dst");
	zassert_equal(out[1], 0x5a, "n_samples=0 must not modify dst");
}

/*
 * Larger end-to-end-style swap test: build a 64-frame stereo block
 * where the left channel carries an ascending ramp (1000, 1001, ...)
 * and the right channel carries a descending ramp distinguishable
 * from the left (-2000, -2001, ...). After the Rust helper copies
 * the whole block into the destination buffer, every output frame
 * must have the original right value in its left slot and vice
 * versa. We also guard the destination with sentinel words on
 * either side so an off-by-one write would be caught.
 */
ZTEST(sof_rust_template_suite, test_rust_template_swap_lr_s16_block_copy)
{
	enum { FRAMES = 64, N = FRAMES * 2 };
	const int16_t SENTINEL = 0x7e57;

	int16_t in[N];
	int16_t guarded[N + 2];
	int16_t *out = &guarded[1];

	for (size_t f = 0; f < FRAMES; f++) {
		in[2 * f]     = (int16_t)(1000 + f);   /* L */
		in[2 * f + 1] = (int16_t)(-2000 - f);  /* R */
	}
	for (size_t i = 0; i < ARRAY_SIZE(guarded); i++) {
		guarded[i] = SENTINEL;
	}

	rust_template_swap_lr_s16(in, out, N);

	/* Sentinels untouched: no out-of-bounds writes. */
	zassert_equal(guarded[0], SENTINEL,
		      "leading sentinel clobbered: 0x%04x", guarded[0]);
	zassert_equal(guarded[N + 1], SENTINEL,
		      "trailing sentinel clobbered: 0x%04x", guarded[N + 1]);

	/* Every frame's L/R pair must be swapped relative to the input. */
	for (size_t f = 0; f < FRAMES; f++) {
		int16_t in_l  = in[2 * f];
		int16_t in_r  = in[2 * f + 1];
		int16_t out_l = out[2 * f];
		int16_t out_r = out[2 * f + 1];

		zassert_equal(out_l, in_r,
			      "frame %zu: out L=%d, expected input R=%d",
			      f, out_l, in_r);
		zassert_equal(out_r, in_l,
			      "frame %zu: out R=%d, expected input L=%d",
			      f, out_r, in_l);
	}

	/* Sanity check: input must be unchanged (helper writes only dst). */
	for (size_t f = 0; f < FRAMES; f++) {
		zassert_equal(in[2 * f],     (int16_t)(1000 + f),
			      "input L corrupted at frame %zu", f);
		zassert_equal(in[2 * f + 1], (int16_t)(-2000 - f),
			      "input R corrupted at frame %zu", f);
	}
}

ZTEST_SUITE(sof_rust_template_suite, NULL, NULL, NULL, NULL, NULL);
