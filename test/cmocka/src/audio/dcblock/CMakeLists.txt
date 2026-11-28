# SPDX-License-Identifier: BSD-3-Clause

cmocka_test(dcblock_math_test
	dcblock_math_test.c
	${PROJECT_SOURCE_DIR}/src/audio/dcblock/dcblock_generic.c
	${PROJECT_SOURCE_DIR}/src/math/numbers.c
)

target_include_directories(dcblock_math_test PRIVATE ${PROJECT_SOURCE_DIR}/src/audio)
target_include_directories(dcblock_math_test PRIVATE ${PROJECT_SOURCE_DIR}/src/audio/dcblock)
