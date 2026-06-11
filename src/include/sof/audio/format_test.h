/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2025 Intel Corporation. All rights reserved.
 */

#ifndef __SOF_AUDIO_FORMAT_TEST_H__
#define __SOF_AUDIO_FORMAT_TEST_H__

/**
 * \brief Run format intrinsic tests (saturation functions)
 *
 * This function validates the saturation functions in both generic and
 * HiFi3 implementations. It's typically called at stream stop to ensure
 * audio format handling is correct.
 */
void format_intrinsic_test_run(void);

#endif /* __SOF_AUDIO_FORMAT_TEST_H__ */
