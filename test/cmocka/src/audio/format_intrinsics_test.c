/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2025 Intel Corporation. All rights reserved.
 *
 * Unit tests for audio format macros and saturation helpers.
 */

#include <stdbool.h>
#include <stdio.h>
#include <stdint.h>
#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <string.h>
#include <math.h>

#include <sof/audio/format.h>
#include <limits.h>

#define FLOAT_EPS 1.0e-6f
#define DOUBLE_EPS 1.0e-12

static void test_format_constants(void **state)
{
	(void)state;

	assert_int_equal(INT24_MAXVALUE, 8388607);
	assert_int_equal(INT24_MINVALUE, -8388608);

	assert_int_equal(ONE_Q2_30, 1073741824);
	assert_int_equal(ONE_Q1_31, 2147483647);
	assert_int_equal(MINUS_3DB_Q1_31, 1520301996);
	assert_int_equal(MINUS_6DB_Q1_31, 1076291389);
	assert_int_equal(MINUS_10DB_Q1_31, 679093957);
	assert_int_equal(MINUS_20DB_Q1_31, 214748365);
	assert_int_equal(MINUS_30DB_Q1_31, 67909396);
	assert_int_equal(MINUS_40DB_Q1_31, 21474836);
	assert_int_equal(MINUS_50DB_Q1_31, 6790940);
	assert_int_equal(MINUS_60DB_Q1_31, 2147484);
	assert_int_equal(MINUS_70DB_Q1_31, 679094);
	assert_int_equal(MINUS_80DB_Q1_31, 214748);
	assert_int_equal(MINUS_90DB_Q1_31, 67909);
}

static void test_q_shift_bits_macros(void **state)
{
	(void)state;

	assert_int_equal(Q_SHIFT_BITS_64(10, 10, 10), 10);
	assert_int_equal(Q_SHIFT_BITS_64(10, 2, 20), INT64_MIN);
	assert_int_equal(Q_SHIFT_BITS_64(60, 10, 0), INT64_MAX);

	assert_int_equal(Q_SHIFT_BITS_32(10, 10, 10), 10);
	assert_int_equal(Q_SHIFT_BITS_32(10, 2, 20), INT32_MIN);
	assert_int_equal(Q_SHIFT_BITS_32(30, 10, 0), INT32_MAX);
}

static void test_q_convert_macros(void **state)
{
	(void)state;

	int32_t q15 = Q_CONVERT_FLOAT(0.5, 15);
	float f = Q_CONVERT_QTOF(q15, 15);
	double d = Q_CONVERT_QTOD(q15, 15);

	assert_int_equal(q15, 16384);
	assert_true(fabsf(f - 0.5f) < FLOAT_EPS);
	assert_true(fabs(d - 0.5) < DOUBLE_EPS);
}

static void test_q_shift_macros(void **state)
{
	(void)state;

	assert_int_equal(Q_SHIFT(1024, 10, 8), 256);
	assert_int_equal(Q_SHIFT_RND(15, 4, 1), 2);
	assert_int_equal(Q_SHIFT_LEFT(16, 4, 8), 256);
}

static void test_q_mult_macros(void **state)
{
	(void)state;

	assert_int_equal(Q_MULTS_16X16(120, 130, 4, 4, 4), 975);
	assert_int_equal(Q_MULTSR_16X16(120, 130, 4, 4, 4), 975);

	assert_int_equal(Q_MULTS_32X32((int64_t)50000, 30000, 8, 8, 8), 5859375);
	assert_int_equal(Q_MULTSR_32X32((int64_t)50000, 30000, 8, 8, 8), 5859375);
}

static void test_sat_macros(void **state)
{
	(void)state;

	assert_int_equal(SATP_INT32((int64_t)INT32_MAX + 10), INT32_MAX);
	assert_int_equal(SATP_INT32(123), 123);

	assert_int_equal(SATM_INT32((int64_t)INT32_MIN - 10), INT32_MIN);
	assert_int_equal(SATM_INT32(-123), -123);
}

/* Test sat_int8 generic implementation */
static void test_sat_int8_max(void **state)
{
	int8_t result = sat_int8(INT8_MAX);
	assert_int_equal(result, INT8_MAX);
}

static void test_sat_int8_min(void **state)
{
	int8_t result = sat_int8(INT8_MIN);
	assert_int_equal(result, INT8_MIN);
}

static void test_sat_int8_overflow_positive(void **state)
{
	int8_t result = sat_int8(0x200);
	assert_int_equal(result, INT8_MAX);
}

static void test_sat_int8_underflow_negative(void **state)
{
	int8_t result = sat_int8(-0x200);
	assert_int_equal(result, INT8_MIN);
}

static void test_sat_int8_normal_positive(void **state)
{
	int8_t result = sat_int8(50);
	assert_int_equal(result, 50);
}

static void test_sat_int8_normal_negative(void **state)
{
	int8_t result = sat_int8(-50);
	assert_int_equal(result, -50);
}

static void test_sat_int8_zero(void **state)
{
	int8_t result = sat_int8(0);
	assert_int_equal(result, 0);
}

/* Test sat_int16 generic implementation */
static void test_sat_int16_max(void **state)
{
	int16_t result = sat_int16(INT16_MAX);
	assert_int_equal(result, INT16_MAX);
}

static void test_sat_int16_min(void **state)
{
	int16_t result = sat_int16(INT16_MIN);
	assert_int_equal(result, INT16_MIN);
}

static void test_sat_int16_overflow_positive(void **state)
{
	int16_t result = sat_int16(0x10000);
	assert_int_equal(result, INT16_MAX);
}

static void test_sat_int16_underflow_negative(void **state)
{
	int16_t result = sat_int16(-0x10000);
	assert_int_equal(result, INT16_MIN);
}

static void test_sat_int16_normal_positive(void **state)
{
	int16_t result = sat_int16(5000);
	assert_int_equal(result, 5000);
}

static void test_sat_int16_normal_negative(void **state)
{
	int16_t result = sat_int16(-5000);
	assert_int_equal(result, -5000);
}

static void test_sat_int16_zero(void **state)
{
	int16_t result = sat_int16(0);
	assert_int_equal(result, 0);
}

/* Test sat_int24 generic implementation */
static void test_sat_int24_max(void **state)
{
	int32_t result = sat_int24(INT24_MAXVALUE);
	assert_int_equal(result, INT24_MAXVALUE);
}

static void test_sat_int24_min(void **state)
{
	int32_t result = sat_int24(INT24_MINVALUE);
	assert_int_equal(result, INT24_MINVALUE);
}

static void test_sat_int24_overflow_positive(void **state)
{
	int32_t result = sat_int24(0x1000000);
	assert_int_equal(result, INT24_MAXVALUE);
}

static void test_sat_int24_underflow_negative(void **state)
{
	int32_t result = sat_int24(-0x1000000);
	assert_int_equal(result, INT24_MINVALUE);
}

static void test_sat_int24_normal_positive(void **state)
{
	int32_t result = sat_int24(500000);
	assert_int_equal(result, 500000);
}

static void test_sat_int24_normal_negative(void **state)
{
	int32_t result = sat_int24(-500000);
	assert_int_equal(result, -500000);
}

static void test_sat_int24_zero(void **state)
{
	int32_t result = sat_int24(0);
	assert_int_equal(result, 0);
}

/* Test sat_int32 generic implementation */
static void test_sat_int32_max(void **state)
{
	int32_t result = sat_int32((int64_t)INT32_MAX);
	assert_int_equal(result, INT32_MAX);
}

static void test_sat_int32_min(void **state)
{
	int32_t result = sat_int32((int64_t)INT32_MIN);
	assert_int_equal(result, INT32_MIN);
}

static void test_sat_int32_overflow_positive(void **state)
{
	int32_t result = sat_int32(0x100000000LL);
	assert_int_equal(result, INT32_MAX);
}

static void test_sat_int32_underflow_negative(void **state)
{
	int32_t result = sat_int32(-0x100000000LL);
	assert_int_equal(result, INT32_MIN);
}

static void test_sat_int32_normal_positive(void **state)
{
	int32_t result = sat_int32((int64_t)1000000);
	assert_int_equal(result, 1000000);
}

static void test_sat_int32_normal_negative(void **state)
{
	int32_t result = sat_int32((int64_t)-1000000);
	assert_int_equal(result, -1000000);
}

static void test_sat_int32_zero(void **state)
{
	int32_t result = sat_int32((int64_t)0);
	assert_int_equal(result, 0);
}

static void test_sat_int32_large_positive(void **state)
{
	int32_t result = sat_int32((int64_t)0x7FFFFFFFLL);
	assert_int_equal(result, 0x7FFFFFFF);
}

static void test_sat_int32_large_negative(void **state)
{
	int32_t result = sat_int32((int64_t)-0x80000000LL);
	assert_int_equal(result, -0x80000000);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_format_constants),
		cmocka_unit_test(test_q_shift_bits_macros),
		cmocka_unit_test(test_q_convert_macros),
		cmocka_unit_test(test_q_shift_macros),
		cmocka_unit_test(test_q_mult_macros),
		cmocka_unit_test(test_sat_macros),

		/* sat_int8 tests */
		cmocka_unit_test(test_sat_int8_max),
		cmocka_unit_test(test_sat_int8_min),
		cmocka_unit_test(test_sat_int8_overflow_positive),
		cmocka_unit_test(test_sat_int8_underflow_negative),
		cmocka_unit_test(test_sat_int8_normal_positive),
		cmocka_unit_test(test_sat_int8_normal_negative),
		cmocka_unit_test(test_sat_int8_zero),
		
		/* sat_int16 tests */
		cmocka_unit_test(test_sat_int16_max),
		cmocka_unit_test(test_sat_int16_min),
		cmocka_unit_test(test_sat_int16_overflow_positive),
		cmocka_unit_test(test_sat_int16_underflow_negative),
		cmocka_unit_test(test_sat_int16_normal_positive),
		cmocka_unit_test(test_sat_int16_normal_negative),
		cmocka_unit_test(test_sat_int16_zero),
		
		/* sat_int24 tests */
		cmocka_unit_test(test_sat_int24_max),
		cmocka_unit_test(test_sat_int24_min),
		cmocka_unit_test(test_sat_int24_overflow_positive),
		cmocka_unit_test(test_sat_int24_underflow_negative),
		cmocka_unit_test(test_sat_int24_normal_positive),
		cmocka_unit_test(test_sat_int24_normal_negative),
		cmocka_unit_test(test_sat_int24_zero),
		
		/* sat_int32 tests */
		cmocka_unit_test(test_sat_int32_max),
		cmocka_unit_test(test_sat_int32_min),
		cmocka_unit_test(test_sat_int32_overflow_positive),
		cmocka_unit_test(test_sat_int32_underflow_negative),
		cmocka_unit_test(test_sat_int32_normal_positive),
		cmocka_unit_test(test_sat_int32_normal_negative),
		cmocka_unit_test(test_sat_int32_zero),
		cmocka_unit_test(test_sat_int32_large_positive),
		cmocka_unit_test(test_sat_int32_large_negative),
	};

	cmocka_set_message_output(CM_OUTPUT_TAP);

	return cmocka_run_group_tests(tests, NULL, NULL);
}
