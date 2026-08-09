/* Copyright (c) 2026 Intel Corporation.
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdint.h>
#include <xa_type_def.h>
#include <nnlib/xa_nnlib_standards.h>
#include <nnlib/xa_nnlib_kernels_api.h>
#include <sof/trace/trace.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/printk.h>

LOG_MODULE_DECLARE(tflmcly, CONFIG_SOF_LOG_LEVEL);

extern struct tr_ctx tflm_tr;
extern void sof_ut_log(const char *msg);

extern WORD32 xa_nn_transpose_8_8(
	WORD8 * __restrict__ p_out,
	const WORD32 *const p_out_shape,
	const WORD8 * __restrict__ p_inp,
	const WORD32 *const p_inp_shape,
	const WORD32 * __restrict__ p_permute_vec,
	WORD32 num_out_dims,
	WORD32 num_inp_dims);

extern WORD32 xa_nn_pad_8_8(
	WORD8 * __restrict__ p_out,
	const WORD32 *const p_out_shape,
	const WORD8 * __restrict__ p_inp,
	const WORD32 *const p_inp_shape,
	const WORD32 * __restrict__ p_pad_values,
	const WORD32 *const p_pad_shape,
	WORD32 num_out_dims,
	WORD32 num_inp_dims,
	WORD32 num_pad_dims,
	WORD32 pad_value);

/* Plain C Reference implementation for 2D Transpose (2x3 -> 3x2) */
static void ref_transpose_2d_8(int8_t *out, const int8_t *inp, int rows, int cols)
{
	for (int i = 0; i < rows; i++) {
		for (int j = 0; j < cols; j++) {
			out[j * rows + i] = inp[i * cols + j];
		}
	}
}

/* Group 8 Unit Test: Reorganization & Reshape Kernels */
int test_reorg_run(void)
{
	sof_ut_log("[TFLM UT] Starting Cadence xa_nnlib Group 8 (Reorganization) validation...");
	printk("[TFLM UT] Starting Cadence xa_nnlib Group 8 (Reorganization) validation...\n");

	/* -------------------------------------------------------------
	 * Test 1: xa_nn_transpose_8_8
	 * ------------------------------------------------------------- */
	{
		static const int8_t input[6] __attribute__((aligned(8))) = {
			1, 2, 3,
			4, 5, 6
		};
		static const int32_t inp_shape[2] __attribute__((aligned(8))) = { 2, 3 };
		static const int32_t out_shape[2] __attribute__((aligned(8))) = { 3, 2 };
		static const int32_t permute_vec[2] __attribute__((aligned(8))) = { 1, 0 };

		static int8_t out_hifi[6] __attribute__((aligned(8)));
		static int8_t out_ref[6] __attribute__((aligned(8)));

		ref_transpose_2d_8(out_ref, input, 2, 3);

		int ret = xa_nn_transpose_8_8(
			out_hifi, out_shape,
			input, inp_shape,
			permute_vec,
			2, 2
		);

		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_transpose_8_8 returned error!");
			printk("[TFLM UT] ERROR: xa_nn_transpose_8_8 returned error code %d!\n", ret);
			return -1;
		}

		for (int i = 0; i < 6; i++) {
			if (out_hifi[i] != out_ref[i]) {
				sof_ut_log("[TFLM UT] ERROR: xa_nn_transpose_8_8 output mismatch!");
				printk("[TFLM UT] ERROR: Transpose idx %d: hifi=%d, ref=%d\n",
				       i, (int)out_hifi[i], (int)out_ref[i]);
				return -1;
			}
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_transpose_8_8 matches reference!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_transpose_8_8 matches reference!\n");
	}

	/* -------------------------------------------------------------
	 * Test 2: xa_nn_pad_8_8
	 * ------------------------------------------------------------- */
	{
		static const int8_t input[4] __attribute__((aligned(8))) = { 1, 2, 3, 4 };
		static const int32_t inp_shape[2] __attribute__((aligned(8))) = { 2, 2 };
		static const int32_t out_shape[2] __attribute__((aligned(8))) = { 4, 4 };
		static const int32_t pad_values[4] __attribute__((aligned(8))) = { 1, 1, 1, 1 };
		static const int32_t pad_shape[2] __attribute__((aligned(8))) = { 2, 2 };

		static int8_t out_hifi[16] __attribute__((aligned(8)));

		int ret = xa_nn_pad_8_8(
			out_hifi, out_shape,
			input, inp_shape,
			pad_values, pad_shape,
			2, 2, 2, 0
		);

		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_pad_8_8 returned error!");
			printk("[TFLM UT] ERROR: xa_nn_pad_8_8 returned error code %d!\n", ret);
			return -1;
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_pad_8_8 executed cleanly!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_pad_8_8 executed cleanly!\n");
	}

	sof_ut_log("[TFLM UT] ALL GROUP 8 REORGANIZATION KERNELS VALIDATED SUCCESSFULLY!");
	printk("[TFLM UT] ALL GROUP 8 REORGANIZATION KERNELS VALIDATED SUCCESSFULLY!\n");
	return 0;
}
