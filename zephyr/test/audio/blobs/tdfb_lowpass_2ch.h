/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2026 Intel Corporation.
 *
 * TDFB lowpass FIR config for 2 channels
 * 4-tap averaging FIR filter [0.25, 0.25, 0.25, 0.25]
 * This exercises the actual beamformer FIR processing path.
 */

#ifndef ZTEST_BLOB_TDFB_LOWPASS_BLOB_H
#define ZTEST_BLOB_TDFB_LOWPASS_BLOB_H

#include <stdint.h>

static const uint32_t tdfb_lowpass_blob[34] = {
	0x34464f53, 0x00000000, 0x00000088, 0x0301d001,
	0x00000000, 0x00000000, 0x00000000, 0x00000000,
	0x00000068, 0x00020002, 0x00000001, 0x00000001,
	0x001e0000, 0x00000000, 0x00000000, 0x00000004,
	0x00000000, 0x00000000, 0x00000000, 0x00000000,
	0x20002000, 0x20002000, 0x00000004, 0x00000000,
	0x00000000, 0x00000000, 0x00000000, 0x20002000,
	0x20002000, 0x00010000, 0x00020001, 0x00000000,
	0x00000000, 0x00000000
};

#endif /* ZTEST_BLOB_TDFB_LOWPASS_BLOB_H */
