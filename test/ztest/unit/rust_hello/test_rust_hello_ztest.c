// SPDX-License-Identifier: BSD-3-Clause
//
// ztest verifying that Rust functions can be called from C in SOF.

#include <zephyr/ztest.h>
#include "rust_hello.h"

ZTEST(sof_rust_hello_suite, test_rust_hello_answer)
{
	zassert_equal(rust_hello_answer(), 42u,
		      "rust_hello_answer() must return 42");
}

ZTEST(sof_rust_hello_suite, test_rust_hello_add)
{
	zassert_equal(rust_hello_add(1, 2), 3u, "1 + 2 == 3");
	zassert_equal(rust_hello_add(0xFFFFFFFFu, 1u), 0xFFFFFFFFu,
		      "saturating add at u32::MAX");
}

ZTEST_SUITE(sof_rust_hello_suite, NULL, NULL, NULL, NULL, NULL);
