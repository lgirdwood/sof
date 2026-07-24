/* Copyright (c) 2026 Intel Corporation.
 * SPDX-License-Identifier: Apache-2.0
 */

#include <xa_type_def.h>
#include <nnlib/xa_nnlib_standards.h>
#include <nnlib/xa_nnlib_kernels_api.h>

extern int printk(const char *fmt, ...);

/* Group 1 Unit Test: Activations & Softmax */
int test_activations_run(void)
{
	int status = 0;
	float input_f32[4] = { -1.0f, 0.0f, 1.0f, 2.0f };
	float output_f32[4] = { 0 };

	/* Test xa_nn_vec_activation_min_max_f32_f32 (ReLU: min=0.0f, max=FLT_MAX) */
	status = xa_nn_vec_activation_min_max_f32_f32(output_f32, input_f32, 0.0f, 1e38f, 4);
	if (status != 0) {
		printk("xa_nn_vec_activation_min_max_f32_f32 failed: %d\n", status);
		return status;
	}

	printk("xa_nn_vec_activation_min_max_f32_f32 passed: [%d, %d, %d, %d]\n",
	       (int)output_f32[0], (int)output_f32[1], (int)output_f32[2], (int)output_f32[3]);

	return 0;
}
