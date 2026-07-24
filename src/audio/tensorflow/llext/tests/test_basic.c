/* Copyright (c) 2026 Intel Corporation.
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdint.h>
#include <xa_type_def.h>
#include <nnlib/xa_nnlib_standards.h>
#include <nnlib/xa_nnlib_kernels_api.h>

extern int printk(const char *fmt, ...);

static float fabsf_val(float v)
{
	return (v < 0.0f) ? -v : v;
}

/* Plain C Reference implementations for Group 2 basic math */
static void ref_elm_add_f32(float *out, const float *inp1, const float *inp2, int len)
{
	for (int i = 0; i < len; i++) {
		out[i] = inp1[i] + inp2[i];
	}
}

static void ref_elm_sub_f32(float *out, const float *inp1, const float *inp2, int len)
{
	for (int i = 0; i < len; i++) {
		out[i] = inp1[i] - inp2[i];
	}
}

static void ref_elm_mul_f32(float *out, const float *inp1, const float *inp2, int len)
{
	for (int i = 0; i < len; i++) {
		out[i] = inp1[i] * inp2[i];
	}
}

static void ref_elm_abs_f32(float *out, const float *inp, int len)
{
	for (int i = 0; i < len; i++) {
		out[i] = fabsf_val(inp[i]);
	}
}

/* Group 2 Unit Test: Basic Math & Elementwise Kernels */
int test_basic_run(void)
{
	int status = 0;
	int errors = 0;
	float inp1[8] = { 1.5f, -2.0f, 3.25f, 4.0f, -5.5f, 6.0f, 7.5f, -8.0f };
	float inp2[8] = { 0.5f,  3.0f, -1.25f, 2.0f,  1.5f, -4.0f, 0.5f, 10.0f };
	float out_hifi[8] = { 0 };
	float out_ref[8] = { 0 };
	int len = 8;

	/* 1. Test xa_nn_elm_add_f32xf32_f32 */
	ref_elm_add_f32(out_ref, inp1, inp2, len);
	status = xa_nn_elm_add_f32xf32_f32(out_hifi, inp1, inp2, len);
	if (status != 0) {
		printk("xa_nn_elm_add_f32xf32_f32 failed: %d\n", status);
		errors++;
	} else {
		for (int i = 0; i < len; i++) {
			if (fabsf_val(out_hifi[i] - out_ref[i]) > 1e-4f) {
				printk("add_f32 mismatch [%d]: hifi=%d/100 ref=%d/100\n",
				       i, (int)(out_hifi[i] * 100), (int)(out_ref[i] * 100));
				errors++;
			}
		}
	}
	if (errors == 0) {
		printk("VALIDATION PASSED: xa_nn_elm_add_f32xf32_f32 matches plain C reference!\n");
	}

	/* 2. Test xa_nn_elm_sub_f32xf32_f32 */
	ref_elm_sub_f32(out_ref, inp1, inp2, len);
	status = xa_nn_elm_sub_f32xf32_f32(out_hifi, inp1, inp2, len);
	if (status != 0) {
		printk("xa_nn_elm_sub_f32xf32_f32 failed: %d\n", status);
		errors++;
	} else {
		for (int i = 0; i < len; i++) {
			if (fabsf_val(out_hifi[i] - out_ref[i]) > 1e-4f) {
				printk("sub_f32 mismatch [%d]: hifi=%d/100 ref=%d/100\n",
				       i, (int)(out_hifi[i] * 100), (int)(out_ref[i] * 100));
				errors++;
			}
		}
	}
	if (errors == 0) {
		printk("VALIDATION PASSED: xa_nn_elm_sub_f32xf32_f32 matches plain C reference!\n");
	}

	/* 3. Test xa_nn_elm_mul_f32xf32_f32 */
	ref_elm_mul_f32(out_ref, inp1, inp2, len);
	status = xa_nn_elm_mul_f32xf32_f32(out_hifi, inp1, inp2, len);
	if (status != 0) {
		printk("xa_nn_elm_mul_f32xf32_f32 failed: %d\n", status);
		errors++;
	} else {
		for (int i = 0; i < len; i++) {
			if (fabsf_val(out_hifi[i] - out_ref[i]) > 1e-4f) {
				printk("mul_f32 mismatch [%d]: hifi=%d/100 ref=%d/100\n",
				       i, (int)(out_hifi[i] * 100), (int)(out_ref[i] * 100));
				errors++;
			}
		}
	}
	if (errors == 0) {
		printk("VALIDATION PASSED: xa_nn_elm_mul_f32xf32_f32 matches plain C reference!\n");
	}

	/* 4. Test xa_nn_elm_abs_f32_f32 */
	ref_elm_abs_f32(out_ref, inp1, len);
	status = xa_nn_elm_abs_f32_f32(out_hifi, inp1, len);
	if (status != 0) {
		printk("xa_nn_elm_abs_f32_f32 failed: %d\n", status);
		errors++;
	} else {
		for (int i = 0; i < len; i++) {
			if (fabsf_val(out_hifi[i] - out_ref[i]) > 1e-4f) {
				printk("abs_f32 mismatch [%d]: hifi=%d/100 ref=%d/100\n",
				       i, (int)(out_hifi[i] * 100), (int)(out_ref[i] * 100));
				errors++;
			}
		}
	}
	if (errors == 0) {
		printk("VALIDATION PASSED: xa_nn_elm_abs_f32_f32 matches plain C reference!\n");
	}

	return errors;
}
