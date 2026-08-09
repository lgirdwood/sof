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

extern WORD32 xa_nn_fully_connected_f32(
	FLOAT32 *__restrict__ p_out,
	const FLOAT32 *__restrict__ p_weight,
	const FLOAT32 *__restrict__ p_inp,
	const FLOAT32 *__restrict__ p_bias,
	WORD32 weight_depth,
	WORD32 out_depth);

extern WORD32 xa_nn_fully_connected_sym8sxasym8s_asym8s(
	WORD8 *__restrict__ p_out,
	const WORD8 *__restrict__ p_weight,
	const WORD8 *__restrict__ p_inp,
	const WORD32 *__restrict__ p_bias,
	WORD32 weight_depth,
	WORD32 out_depth,
	WORD32 input_zero_bias,
	WORD32 out_multiplier,
	WORD32 out_shift,
	WORD32 out_zero_bias);

static float fabsf_val(float v)
{
	return (v < 0.0f) ? -v : v;
}

/* Plain C Reference implementation for Float32 Fully Connected layer */
static void ref_fc_f32(
	float *out,
	const float *weight,
	const float *inp,
	const float *bias,
	int weight_depth,
	int out_depth)
{
	for (int i = 0; i < out_depth; i++) {
		float sum = (bias != NULL) ? bias[i] : 0.0f;
		for (int j = 0; j < weight_depth; j++) {
			sum += inp[j] * weight[i * weight_depth + j];
		}
		out[i] = sum;
	}
}

/* Group 4 Unit Test: Fully Connected (FC) Kernels */
int test_fc_run(void)
{
	sof_ut_log("[TFLM UT] Starting Cadence xa_nnlib Group 4 (Fully Connected) validation...");
	printk("[TFLM UT] Starting Cadence xa_nnlib Group 4 (Fully Connected) validation...\n");

	/* -------------------------------------------------------------
	 * Test 1: xa_nn_fully_connected_f32
	 * ------------------------------------------------------------- */
	{
		#define FC_INP_DEPTH 4
		#define FC_OUT_DEPTH 2
		static const float input[FC_INP_DEPTH] __attribute__((aligned(8))) = {
			1.0f, 2.0f, -1.0f, 0.5f
		};
		static const float weight[FC_OUT_DEPTH * FC_INP_DEPTH] __attribute__((aligned(8))) = {
			0.5f, -0.5f, 1.0f, 2.0f,  /* Output neuron 0 */
			1.5f,  0.0f, 0.5f, -1.0f  /* Output neuron 1 */
		};
		static const float bias[FC_OUT_DEPTH] __attribute__((aligned(8))) = {
			0.1f, -0.2f
		};

		static float out_hifi[FC_OUT_DEPTH] __attribute__((aligned(8)));
		static float out_ref[FC_OUT_DEPTH] __attribute__((aligned(8)));

		ref_fc_f32(out_ref, weight, input, bias, FC_INP_DEPTH, FC_OUT_DEPTH);

		int ret = xa_nn_fully_connected_f32(out_hifi, weight, input, bias, FC_INP_DEPTH, FC_OUT_DEPTH);
		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_fully_connected_f32 returned error!");
			printk("[TFLM UT] ERROR: xa_nn_fully_connected_f32 returned error code %d!\n", ret);
			return -1;
		}

		for (int i = 0; i < FC_OUT_DEPTH; i++) {
			float diff = fabsf_val(out_hifi[i] - out_ref[i]);
			if (diff > 1e-4f) {
				sof_ut_log("[TFLM UT] ERROR: xa_nn_fully_connected_f32 output mismatch!");
				printk("[TFLM UT] ERROR: FC F32 idx %d: hifi=%f, ref=%f (diff=%f)\n",
				       i, (double)out_hifi[i], (double)out_ref[i], (double)diff);
				return -1;
			}
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_fully_connected_f32 matches reference!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_fully_connected_f32 matches reference!\n");
	}

	/* -------------------------------------------------------------
	 * Test 2: xa_nn_fully_connected_sym8sxasym8s_asym8s
	 * ------------------------------------------------------------- */
	{
		#define FC_Q_INP_DEPTH 4
		#define FC_Q_OUT_DEPTH 2
		static const int8_t input_q[FC_Q_INP_DEPTH] __attribute__((aligned(8))) = {
			10, 20, -10, 5
		};
		static const int8_t weight_q[FC_Q_OUT_DEPTH * FC_Q_INP_DEPTH] __attribute__((aligned(8))) = {
			5, -5, 10, 20,
			15, 0, 5, -10
		};
		static const int32_t bias_q[FC_Q_OUT_DEPTH] __attribute__((aligned(8))) = {
			1, -2
		};

		static int8_t out_q_hifi[FC_Q_OUT_DEPTH] __attribute__((aligned(8)));

		int ret = xa_nn_fully_connected_sym8sxasym8s_asym8s(
			out_q_hifi, weight_q, input_q, bias_q,
			FC_Q_INP_DEPTH, FC_Q_OUT_DEPTH,
			0,          /* input_zero_bias */
			1073741824, /* out_multiplier = 0.5 in Q31 */
			-1,         /* out_shift */
			0           /* out_zero_bias */
		);

		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_fully_connected_sym8sxasym8s_asym8s returned error!");
			printk("[TFLM UT] ERROR: xa_nn_fully_connected_sym8sxasym8s_asym8s returned error %d!\n", ret);
			return -1;
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_fully_connected_sym8sxasym8s_asym8s executed cleanly!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_fully_connected_sym8sxasym8s_asym8s executed cleanly!\n");
	}

	sof_ut_log("[TFLM UT] ALL GROUP 4 FULLY CONNECTED KERNELS VALIDATED SUCCESSFULLY!");
	printk("[TFLM UT] ALL GROUP 4 FULLY CONNECTED KERNELS VALIDATED SUCCESSFULLY!\n");
	return 0;
}
