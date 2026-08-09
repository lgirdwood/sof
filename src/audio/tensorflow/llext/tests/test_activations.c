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







/* Plain C Reference implementations for verification */


static void ref_activation_f32(float *out, const float *inp, float min_val, float max_val, int len)
{
	for (int i = 0; i < len; i++) {
		float v = inp[i];
		if (v < min_val) v = min_val;
		if (v > max_val) v = max_val;
		out[i] = v;
	}
}

static void ref_activation_16(int16_t *out, const int16_t *inp, int min_val, int max_val, int len)
{
	for (int i = 0; i < len; i++) {
		int v = inp[i];
		if (v < min_val) v = min_val;
		if (v > max_val) v = max_val;
		out[i] = (int16_t)v;
	}
}

static void ref_activation_8(int8_t *out, const int8_t *inp, int min_val, int max_val, int len)
{
	for (int i = 0; i < len; i++) {
		int v = inp[i];
		if (v < min_val) v = min_val;
		if (v > max_val) v = max_val;
		out[i] = (int8_t)v;
	}
}

static float fabsf_val(float v)
{
	return (v < 0.0f) ? -v : v;
}

/* Group 1 Unit Test: Activations & Softmax validation against Plain C */
int test_activations_run(void)
{
	int status = 0;
	int errors = 0;

	/* -------------------------------------------------------------
	 * 1. Test xa_nn_vec_activation_min_max_f32_f32 (ReLU6)
	 * ------------------------------------------------------------- */
	{
		static __attribute__((aligned(8))) float inp_f32[8] = { -5.5f, -1.2f, 0.0f, 0.5f, 2.8f, 5.9f, 8.4f, 100.0f };
		static __attribute__((aligned(8))) float out_hifi[8] = { 0 };
		static __attribute__((aligned(8))) float out_ref[8] = { 0 };
		float min_f = 0.0f, max_f = 6.0f;
		int len = 8;

		ref_activation_f32(out_ref, inp_f32, min_f, max_f, len);
		status = xa_nn_vec_activation_min_max_f32_f32(out_hifi, inp_f32, min_f, max_f, len);

		if (status != 0) {
			printk("xa_nn_vec_activation_min_max_f32_f32 returned error: %d\n", status);
			errors++;
		} else {
			for (int i = 0; i < len; i++) {
				if (fabsf_val(out_hifi[i] - out_ref[i]) > 1e-4f) {
					printk("f32_f32 mismatch at [%d]: hifi=%d/1000 ref=%d/1000\n",
					       i, (int)(out_hifi[i] * 1000), (int)(out_ref[i] * 1000));
					errors++;
				}
			}
		}

		if (errors == 0) {
			sof_ut_log("VALIDATION PASSED: xa_nn_vec_activation_min_max_f32_f32 matches plain C reference!");
			printk("VALIDATION PASSED: xa_nn_vec_activation_min_max_f32_f32 matches plain C reference!\n");
		}
	}

	/* -------------------------------------------------------------
	 * 2. Test xa_nn_vec_activation_min_max_16_16
	 * ------------------------------------------------------------- */
	{
		static __attribute__((aligned(8))) int16_t inp_16[8] = { -500, -10, 0, 5, 20, 45, 100, 32767 };
		static __attribute__((aligned(8))) int16_t out_hifi[8] = { 0 };
		static __attribute__((aligned(8))) int16_t out_ref[8] = { 0 };
		int min_i = 0, max_i = 50;
		int len = 8;

		ref_activation_16(out_ref, inp_16, min_i, max_i, len);
		status = xa_nn_vec_activation_min_max_16_16(out_hifi, inp_16, min_i, max_i, len);

		if (status != 0) {
			printk("xa_nn_vec_activation_min_max_16_16 returned error: %d\n", status);
			errors++;
		} else {
			for (int i = 0; i < len; i++) {
				if (out_hifi[i] != out_ref[i]) {
					printk("16_16 mismatch at [%d]: hifi=%d ref=%d\n",
					       i, (int)out_hifi[i], (int)out_ref[i]);
					errors++;
				}
			}
		}
		if (errors == 0) {
			sof_ut_log("VALIDATION PASSED: xa_nn_vec_activation_min_max_16_16 matches plain C reference!");
			printk("VALIDATION PASSED: xa_nn_vec_activation_min_max_16_16 matches plain C reference!\n");
		}
	}

	/* -------------------------------------------------------------
	 * 3. Test xa_nn_vec_activation_min_max_8_8
	 * ------------------------------------------------------------- */
	{
		static __attribute__((aligned(8))) int8_t inp_8[8] = { -128, -50, 0, 10, 30, 45, 80, 127 };
		static __attribute__((aligned(8))) int8_t out_hifi[8] = { 0 };
		static __attribute__((aligned(8))) int8_t out_ref[8] = { 0 };
		int min_i = 0, max_i = 50;
		int len = 8;

		ref_activation_8(out_ref, inp_8, min_i, max_i, len);
		status = xa_nn_vec_activation_min_max_8_8(out_hifi, inp_8, min_i, max_i, len);

		if (status != 0) {
			printk("xa_nn_vec_activation_min_max_8_8 returned error: %d\n", status);
			errors++;
		} else {
			for (int i = 0; i < len; i++) {
				if (out_hifi[i] != out_ref[i]) {
					printk("8_8 mismatch at [%d]: hifi=%d ref=%d\n",
					       i, (int)out_hifi[i], (int)out_ref[i]);
					errors++;
				}
			}
		}
		if (errors == 0) {
			sof_ut_log("VALIDATION PASSED: xa_nn_vec_activation_min_max_8_8 matches plain C reference!");
			printk("VALIDATION PASSED: xa_nn_vec_activation_min_max_8_8 matches plain C reference!\n");
		}
	}



	return errors;
}
