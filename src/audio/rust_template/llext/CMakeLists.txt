# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Intel Corporation.

# sof_llext_build() comes from sof/zephyr/CMakeLists.txt, already in scope.
# sof_rust_llext_module() lives in sof/scripts/cmake/rust.cmake.
cmake_path(SET _sof_root NORMALIZE ${APPLICATION_SOURCE_DIR}/..)
include(${_sof_root}/scripts/cmake/rust.cmake)

sof_rust_llext_module(rust_template
	CRATE_DIR ${CMAKE_CURRENT_LIST_DIR}/../rust
        SOURCES   ../rust_template.c ../rust_template_log.c
)
