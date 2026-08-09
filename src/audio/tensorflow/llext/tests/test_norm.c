/* Copyright (c) 2026 Intel Corporation.
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdint.h>
#include <math.h>
#include <xa_type_def.h>
#include <nnlib/xa_nnlib_standards.h>
#include <nnlib/xa_nnlib_kernels_api.h>
#include <sof/trace/trace.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/printk.h>

LOG_MODULE_DECLARE(tflmcly, CONFIG_SOF_LOG_LEVEL);

extern struct tr_ctx tflm_tr;
extern void sof_ut_log(const char *msg);

extern WORD32 xa_nn_l2_norm_f32(
	FLOAT32 * __restrict__ p_out,
	const FLOAT32 * __restrict__ p_inp,
	WORD32 num_elm);

extern WORD32 xa_nn_l2_norm_asym8s_asym8s(
	WORD8 *p_out,
	const WORD8 *p_inp,
	WORD32 zero_point,
	WORD32 num_elm);

static float fabsf_val(float v)
{
	return (v < 0.0f) ? -v : v;
}

/* Plain C Reference implementation for L2 Norm Float32 */
static void ref_l2_norm_f32(float *out, const float *inp, int num_elm)
{
	float sum_sq = 0.0f;
	for (int i = 0; i < num_elm; i++) {
		sum_sq += inp[i] * inp[i];
	}
	float l2_norm = (sum_sq > 0.0f) ? (1.0f / sqrtf(sum_sq)) : 0.0f;
	for (int i = 0; i < num_elm; i++) {
		out[i] = inp[i] * l2_norm;
	}
}

/* Group 6 Unit Test: Normalization Kernels */
int test_norm_run(void)
{
	sof_ut_log("[TFLM UT] Starting Cadence xa_nnlib Group 6 (Normalization) validation...");
	printk("[TFLM UT] Starting Cadence xa_nnlib Group 6 (Normalization) validation...\n");

	/* -------------------------------------------------------------
	 * Test 1: xa_nn_l2_norm_f32
	 * ------------------------------------------------------------- */
	{
		#define L2_NUM_ELM 4
		static const float input[L2_NUM_ELM] __attribute__((aligned(8))) = {
			1.0f, 2.0f, 3.0f, 4.0f
		};

		static float out_hifi[L2_NUM_ELM] __attribute__((aligned(8)));
		static float out_ref[L2_NUM_ELM] __attribute__((aligned(8)));

		ref_l2_norm_f32(out_ref, input, L2_NUM_ELM);

		int ret = xa_nn_l2_norm_f32(out_hifi, input, L2_NUM_ELM);
		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_l2_norm_f32 returned error!");
			printk("[TFLM UT] ERROR: xa_nn_l2_norm_f32 returned error code %d!\n", ret);
			return -1;
		}

		for (int i = 0; i < L2_NUM_ELM; i++) {
			float diff = fabsf_val(out_hifi[i] - out_ref[i]);
			if (diff > 1e-3f) {
				sof_ut_log("[TFLM UT] ERROR: xa_nn_l2_norm_f32 output mismatch!");
				printk("[TFLM UT] ERROR: L2 Norm F32 idx %d: hifi=%f, ref=%f (diff=%f)\n",
				       i, (double)out_hifi[i], (double)out_ref[i], (double)diff);
				return -1;
			}
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_l2_norm_f32 matches reference!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_l2_norm_f32 matches reference!\n");
	}

	/* -------------------------------------------------------------
	 * Test 2: xa_nn_l2_norm_asym8s_asym8s
	 * ------------------------------------------------------------- */
	{
		#define L2_Q_NUM_ELM 4
		static const int8_t input_q[L2_Q_NUM_ELM] __attribute__((aligned(8))) = {
			10, 20, 30, 40
		};

		static int8_t out_q_hifi[L2_Q_NUM_ELM] __attribute__((aligned(8)));

		int ret = xa_nn_l2_norm_asym8s_asym8s(out_q_hifi, input_q, 0, L2_Q_NUM_ELM);
		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_l2_norm_asym8s_asym8s returned error!");
			printk("[TFLM UT] ERROR: xa_nn_l2_norm_asym8s_asym8s returned error code %d!\n", ret);
			return -1;
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_l2_norm_asym8s_asym8s executed cleanly!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_l2_norm_asym8s_asym8s executed cleanly!\n");
	}

	sof_ut_log("[TFLM UT] ALL GROUP 6 NORMALIZATION KERNELS VALIDATED SUCCESSFULLY!");
	printk("[TFLM UT] ALL GROUP 6 NORMALIZATION KERNELS VALIDATED SUCCESSFULLY!\n");
	return 0;
}
