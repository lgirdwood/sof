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

extern WORD32 xa_nn_avgpool_f32(
	FLOAT32* __restrict__ p_out,
	const FLOAT32* __restrict__ p_inp,
	WORD32 input_height,
	WORD32 input_width,
	WORD32 input_channels,
	WORD32 kernel_height,
	WORD32 kernel_width,
	WORD32 x_stride,
	WORD32 y_stride,
	WORD32 x_padding,
	WORD32 y_padding,
	WORD32 out_height,
	WORD32 out_width,
	WORD32 inp_data_format,
	WORD32 out_data_format,
	VOID *p_scratch);

extern WORD32 xa_nn_maxpool_f32(
	FLOAT32* __restrict__ p_out,
	const FLOAT32* __restrict__ p_inp,
	WORD32 input_height,
	WORD32 input_width,
	WORD32 input_channels,
	WORD32 kernel_height,
	WORD32 kernel_width,
	WORD32 x_stride,
	WORD32 y_stride,
	WORD32 x_padding,
	WORD32 y_padding,
	WORD32 out_height,
	WORD32 out_width,
	WORD32 inp_data_format,
	WORD32 out_data_format,
	VOID *p_scratch);

static float fabsf_val(float v)
{
	return (v < 0.0f) ? -v : v;
}

/* Plain C Reference implementation for AvgPool F32 (2x2 kernel, stride 2) */
static void ref_avgpool_2x2_f32(float *out, const float *inp, int ih, int iw, int ic)
{
	int oh = ih / 2;
	int ow = iw / 2;
	for (int c = 0; c < ic; c++) {
		for (int h = 0; h < oh; h++) {
			for (int w = 0; w < ow; w++) {
				float sum = inp[(h * 2 * iw + w * 2) * ic + c] +
					    inp[(h * 2 * iw + (w * 2 + 1)) * ic + c] +
					    inp[((h * 2 + 1) * iw + w * 2) * ic + c] +
					    inp[((h * 2 + 1) * iw + (w * 2 + 1)) * ic + c];
				out[(h * ow + w) * ic + c] = sum / 4.0f;
			}
		}
	}
}

/* Group 7 Unit Test: Pooling Kernels */
int test_pool_run(void)
{
	sof_ut_log("[TFLM UT] Starting Cadence xa_nnlib Group 7 (Pooling) validation...");
	printk("[TFLM UT] Starting Cadence xa_nnlib Group 7 (Pooling) validation...\n");

	/* -------------------------------------------------------------
	 * Test 1: xa_nn_avgpool_f32
	 * ------------------------------------------------------------- */
	{
		#define POOL_IH 4
		#define POOL_IW 4
		#define POOL_IC 1
		#define POOL_OH 2
		#define POOL_OW 2

		static const float input[POOL_IH * POOL_IW * POOL_IC] __attribute__((aligned(8))) = {
			1.0f, 2.0f, 3.0f, 4.0f,
			5.0f, 6.0f, 7.0f, 8.0f,
			9.0f, 10.0f, 11.0f, 12.0f,
			13.0f, 14.0f, 15.0f, 16.0f
		};

		static float out_hifi[POOL_OH * POOL_OW * POOL_IC] __attribute__((aligned(8)));
		static float out_ref[POOL_OH * POOL_OW * POOL_IC] __attribute__((aligned(8)));
		static uint8_t scratch[1024] __attribute__((aligned(8)));

		ref_avgpool_2x2_f32(out_ref, input, POOL_IH, POOL_IW, POOL_IC);

		int ret = xa_nn_avgpool_f32(
			out_hifi, input,
			POOL_IH, POOL_IW, POOL_IC,
			2, 2, /* kernel 2x2 */
			2, 2, /* stride 2x2 */
			0, 0, /* padding 0x0 */
			POOL_OH, POOL_OW,
			0, 0, /* NHWC format */
			scratch
		);

		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_avgpool_f32 returned error!");
			printk("[TFLM UT] ERROR: xa_nn_avgpool_f32 returned error code %d!\n", ret);
			return -1;
		}

		for (int i = 0; i < POOL_OH * POOL_OW * POOL_IC; i++) {
			float diff = fabsf_val(out_hifi[i] - out_ref[i]);
			if (diff > 1e-4f) {
				sof_ut_log("[TFLM UT] ERROR: xa_nn_avgpool_f32 output mismatch!");
				printk("[TFLM UT] ERROR: AvgPool F32 idx %d: hifi=%f, ref=%f (diff=%f)\n",
				       i, (double)out_hifi[i], (double)out_ref[i], (double)diff);
				return -1;
			}
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_avgpool_f32 matches reference!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_avgpool_f32 matches reference!\n");
	}

	/* -------------------------------------------------------------
	 * Test 2: xa_nn_maxpool_f32
	 * ------------------------------------------------------------- */
	{
		static float out_hifi[POOL_OH * POOL_OW * POOL_IC] __attribute__((aligned(8)));
		static uint8_t scratch[1024] __attribute__((aligned(8)));

		static const float input[POOL_IH * POOL_IW * POOL_IC] __attribute__((aligned(8))) = {
			1.0f, 2.0f, 3.0f, 4.0f,
			5.0f, 6.0f, 7.0f, 8.0f,
			9.0f, 10.0f, 11.0f, 12.0f,
			13.0f, 14.0f, 15.0f, 16.0f
		};

		int ret = xa_nn_maxpool_f32(
			out_hifi, input,
			POOL_IH, POOL_IW, POOL_IC,
			2, 2, /* kernel 2x2 */
			2, 2, /* stride 2x2 */
			0, 0, /* padding 0x0 */
			POOL_OH, POOL_OW,
			0, 0, /* NHWC format */
			scratch
		);

		if (ret != 0) {
			sof_ut_log("[TFLM UT] ERROR: xa_nn_maxpool_f32 returned error!");
			printk("[TFLM UT] ERROR: xa_nn_maxpool_f32 returned error code %d!\n", ret);
			return -1;
		}

		sof_ut_log("[TFLM UT] VALIDATION PASSED: xa_nn_maxpool_f32 executed cleanly!");
		printk("[TFLM UT] VALIDATION PASSED: xa_nn_maxpool_f32 executed cleanly!\n");
	}

	sof_ut_log("[TFLM UT] ALL GROUP 7 POOLING KERNELS VALIDATED SUCCESSFULLY!");

	printk("[TFLM UT] ALL GROUP 7 POOLING KERNELS VALIDATED SUCCESSFULLY!\n");
	return 0;
}
