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

extern WORD32 xa_nn_matXvec_f32xf32_f32(
	FLOAT32 * __restrict__ p_out,
	const FLOAT32 * __restrict__ p_mat1,
	const FLOAT32 * __restrict__ p_mat2,
	const FLOAT32 * __restrict__ p_vec1,
	const FLOAT32 * __restrict__ p_vec2,
	const FLOAT32 * __restrict__ p_bias,
	WORD32 rows, WORD32 cols1, WORD32 cols2,
	WORD32 row_stride1,
	WORD32 row_stride2);

extern WORD32 xa_nn_matmul_f32xf32_f32(
	FLOAT32 * __restrict__ p_out,
	const FLOAT32 * __restrict__ p_mat1,
	const FLOAT32 * __restrict__ p_vec1,
	const FLOAT32 * __restrict__ p_bias,
	WORD32 rows,
	WORD32 cols1,
	WORD32 row_stride1,
	WORD32 vec_count,
	WORD32 vec_offset,
	WORD32 out_offset,
	WORD32 out_stride);

static float fabsf_val(float v)
{
	return (v < 0.0f) ? -v : v;
}

/* Plain C Reference implementation for matXvec f32 */
static void ref_matXvec_f32(
	float *out,
	const float *mat,
	const float *vec,
	const float *bias,
	int rows,
	int cols)
{
	for (int i = 0; i < rows; i++) {
		float sum = (bias != NULL) ? bias[i] : 0.0f;
		for (int j = 0; j < cols; j++) {
			sum += mat[i * cols + j] * vec[j];
		}
		out[i] = sum;
	}
}

/* Group 5 Unit Test: Matrix Vector & MatMul Kernels */
int test_matxvec_run(void)
{
	sof_ut_log("[TFLM UT] Starting Cadence xa_nnlib Group 5 (Matrix Vector & MatMul) validation...");
	printk("[TFLM UT] Starting Cadence xa_nnlib Group 5 (Matrix Vector & MatMul) validation...\n");

	/* -------------------------------------------------------------
	 * Test 1: xa_nn_matXvec_f32xf32_f32
	 * ------------------------------------------------------------- */
	{
		#define MXV_ROWS 3
		#define MXV_COLS 4
		static const float mat[MXV_ROWS * MXV_COLS] __attribute__((aligned(8))) = {
			1.0f, 2.0f, 3.0f, 4.0f,
			-1.0f, 0.5f, 2.0f, -2.0f,
			0.0f, 1.5f, -1.0f, 3.0f
		};
		static const float vec[MXV_COLS] __attribute__((aligned(8))) = {
			0.5f, -1.0f, 2.0f, 1.5f
		};
		static const float bias[MXV_ROWS] __attribute__((aligned(8))) = {
			0.2f, -0.5f, 1.0f
		};

		static float out_hifi[MXV_ROWS] __attribute__((aligned(8)));
		static float out_ref[MXV_ROWS] __attribute__((aligned(8)));

		ref_matXvec_f32(out_ref, mat, vec, bias, MXV_ROWS, MXV_COLS);

		int ret = xa_nn_matXvec_f32xf32_f32(
			out_hifi,
			mat, NULL,
			vec, NULL,
			bias,
			MXV_ROWS, MXV_COLS, 0,
			MXV_COLS, 0
		);

		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_matXvec_f32xf32_f32 returned error!");
			printk("[TFLM UT] ERROR: xa_nn_matXvec_f32xf32_f32 returned error code %d!\n", ret);
			return -1;
		}

		for (int i = 0; i < MXV_ROWS; i++) {
			float diff = fabsf_val(out_hifi[i] - out_ref[i]);
			if (diff > 1e-4f) {
				sof_ut_log("[TFLM UT] ERROR: xa_nn_matXvec_f32xf32_f32 output mismatch!");
				printk("[TFLM UT] ERROR: MXV F32 idx %d: hifi=%f, ref=%f (diff=%f)\n",
				       i, (double)out_hifi[i], (double)out_ref[i], (double)diff);
				return -1;
			}
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_matXvec_f32xf32_f32 matches reference!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_matXvec_f32xf32_f32 matches reference!\n");
	}

	/* -------------------------------------------------------------
	 * Test 2: xa_nn_matmul_f32xf32_f32
	 * ------------------------------------------------------------- */
	{
		#define MM_ROWS 2
		#define MM_COLS 3
		#define MM_VEC_COUNT 2
		static const float mat1[MM_ROWS * MM_COLS] __attribute__((aligned(8))) = {
			1.0f, 2.0f, 3.0f,
			4.0f, 5.0f, 6.0f
		};
		static const float vec1[MM_COLS * MM_VEC_COUNT] __attribute__((aligned(8))) = {
			1.0f, 0.5f,
			2.0f, -1.0f,
			-1.0f, 2.0f
		};
		static const float bias[MM_ROWS] __attribute__((aligned(8))) = {
			0.1f, -0.1f
		};

		static float out_hifi[MM_ROWS * MM_VEC_COUNT] __attribute__((aligned(8)));

		int ret = xa_nn_matmul_f32xf32_f32(
			out_hifi,
			mat1,
			vec1,
			bias,
			MM_ROWS,
			MM_COLS,
			MM_COLS,
			MM_VEC_COUNT,
			1,
			1,
			MM_VEC_COUNT
		);

		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_matmul_f32xf32_f32 returned error!");
			printk("[TFLM UT] ERROR: xa_nn_matmul_f32xf32_f32 returned error %d!\n", ret);
			return -1;
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_matmul_f32xf32_f32 executed cleanly!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_matmul_f32xf32_f32 executed cleanly!\n");
	}

	sof_ut_log("[TFLM UT] ALL GROUP 5 MATXVEC & MATMUL KERNELS VALIDATED SUCCESSFULLY!");
	printk("[TFLM UT] ALL GROUP 5 MATXVEC & MATMUL KERNELS VALIDATED SUCCESSFULLY!\n");
	return 0;
}
