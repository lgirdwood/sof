// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2026 Intel Corporation. All rights reserved.

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

float fmaxf(float a, float b)
{
	return (a > b) ? a : b;
}

float fminf(float a, float b)
{
	return (a < b) ? a : b;
}

float roundf(float x)
{
	return (x >= 0.0f) ? (float)(int32_t)(x + 0.5f) : (float)(int32_t)(x - 0.5f);
}

double round(double x)
{
	return (x >= 0.0) ? (double)(int64_t)(x + 0.5) : (double)(int64_t)(x - 0.5);
}

double floor(double x)
{
	int64_t i = (int64_t)x;
	return (x < (double)i) ? (double)(i - 1) : (double)i;
}

float expf(float x)
{
	float sum = 1.0f;
	float term = 1.0f;
	for (int i = 1; i <= 12; i++) {
		term *= x / (float)i;
		sum += term;
	}
	return sum;
}

float logf(float x)
{
	if (x <= 0.0f)
		return 0.0f;
	float y = (x - 1.0f) / (x + 1.0f);
	float y2 = y * y;
	float sum = y;
	float term = y;
	for (int i = 3; i <= 11; i += 2) {
		term *= y2;
		sum += term / (float)i;
	}
	return 2.0f * sum;
}

double frexp(double x, int *exp)
{
	if (x == 0.0) {
		if (exp) *exp = 0;
		return 0.0;
	}
	int e = 0;
	if (x < 0.0) {
		double ret = -frexp(-x, exp);
		return ret;
	}
	while (x >= 1.0) {
		x /= 2.0;
		e++;
	}
	while (x < 0.5) {
		x *= 2.0;
		e--;
	}
	if (exp)
		*exp = e;
	return x;
}

#ifdef __cplusplus
}
#endif
